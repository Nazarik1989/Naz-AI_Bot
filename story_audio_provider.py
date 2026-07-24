"""Safe asynchronous adapter for Stability AI Stable Audio 3.0.

The adapter deliberately separates the paid POST from result polling.  It never
retries a submission: callers persist the returned generation id and may safely
retry only ``poll``.  HTTP I/O is injectable so the complete contract can be
tested without network or paid provider calls.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping, Protocol


DEFAULT_STABILITY_BASE_URL = "https://api.stability.ai"
STABLE_AUDIO_MODEL = "stable-audio-3"
STABILITY_USER_AGENT = "NazAudioLibrary/1.0"
TEXT_TO_AUDIO_PATH = "/v2beta/audio/stable-audio/text-to-audio"
AUDIO_RESULTS_PATH = "/v2beta/audio/results"
DEFAULT_MAX_RESPONSE_BYTES = 192 * 1024 * 1024
ABSOLUTE_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
MAX_PROMPT_CHARACTERS = 10_000
MAX_SEED = 4_294_967_294
GENERATION_ID_LENGTH = 64


class AudioProviderError(RuntimeError):
    """Provider failure whose representation is safe for logs and manifests."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


class AudioTransportError(RuntimeError):
    """Redacted low-level transport failure used by injectable transports."""

    def __init__(self, code: str = "audio_transport_error") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AudioGenerationRequest:
    prompt: str
    duration_seconds: float
    output_format: str = "mp3"
    model: str = STABLE_AUDIO_MODEL
    seed: int = 0
    steps: int = 8
    cfg_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class AudioArtifact:
    data: bytes
    content_type: str
    output_format: str
    seed: int | None
    finish_reason: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AudioProviderJob:
    external_job_id: str
    status: str
    artifact: AudioArtifact | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _declared_content_length(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _read_limited(stream: object, headers: Mapping[str, str], limit: int) -> bytes:
    declared = _declared_content_length(headers)
    if declared is not None and declared > limit:
        raise AudioTransportError("audio_response_too_large")
    raw = stream.read(limit + 1)  # type: ignore[attr-defined]
    if len(raw) > limit:
        raise AudioTransportError("audio_response_too_large")
    return raw


class UrllibTransport:
    """Stdlib-only HTTP transport with a hard response-body bound."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                response_headers = dict(response.headers.items())
                raw = _read_limited(response, response_headers, max_response_bytes)
                return HttpResponse(response.status, response_headers, raw)
        except urllib.error.HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            try:
                raw = _read_limited(exc, response_headers, max_response_bytes)
            finally:
                exc.close()
            return HttpResponse(exc.code, response_headers, raw)
        except AudioTransportError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AudioTransportError() from exc


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Parser exceptions may retain excerpts of the provider body.
        raise AudioProviderError("audio_provider_response_invalid") from None
    if not isinstance(value, dict):
        raise AudioProviderError("audio_provider_response_invalid")
    return value


def _format_number(value: float) -> str:
    return format(float(value), ".15g")


def _multipart_form(fields: tuple[tuple[str, str], ...]) -> tuple[bytes, str]:
    """Encode fixed-name text fields using a fresh non-colliding boundary."""

    encoded: list[tuple[str, bytes]] = []
    for name, value in fields:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise AudioProviderError("audio_multipart_field_invalid")
        encoded.append((name, value.encode("utf-8")))

    boundary = ""
    for _ in range(8):
        candidate = f"----NazStableAudio{secrets.token_hex(24)}"
        marker = candidate.encode("ascii")
        if all(marker not in value for _, value in encoded):
            boundary = candidate
            break
    if not boundary:
        raise AudioProviderError("audio_multipart_boundary_failed")

    chunks: list[bytes] = []
    for name, value in encoded:
        chunks.extend((
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            value,
            b"\r\n",
        ))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _validate_header_value(value: str, code: str) -> str:
    normalized = value.strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise AudioProviderError(code)
    return normalized


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AudioProviderError("audio_base_url_invalid")
    return normalized


def _validate_generation_id(value: object, *, response: bool = False) -> str:
    generation_id = value if isinstance(value, str) else ""
    # The official schema specifies length only.  URL quoting in ``poll`` keeps
    # even an unusual but schema-valid id confined to one path segment.
    if len(generation_id) != GENERATION_ID_LENGTH:
        code = "audio_submit_response_invalid" if response else "audio_job_id_invalid"
        raise AudioProviderError(code)
    return generation_id


def _validate_request(request: AudioGenerationRequest) -> None:
    if not isinstance(request.prompt, str):
        raise AudioProviderError("audio_prompt_invalid")
    if not request.prompt.strip() or len(request.prompt) > MAX_PROMPT_CHARACTERS:
        raise AudioProviderError("audio_prompt_invalid")
    if request.model != STABLE_AUDIO_MODEL:
        raise AudioProviderError("audio_model_invalid")
    if (
        isinstance(request.duration_seconds, bool)
        or not isinstance(request.duration_seconds, (int, float))
        or not math.isfinite(float(request.duration_seconds))
        or not 1 <= float(request.duration_seconds) <= 380
    ):
        raise AudioProviderError("audio_duration_invalid")
    if request.output_format not in {"mp3", "wav"}:
        raise AudioProviderError("audio_output_format_invalid")
    if isinstance(request.seed, bool) or not isinstance(request.seed, int) or not 0 <= request.seed <= MAX_SEED:
        raise AudioProviderError("audio_seed_invalid")
    if isinstance(request.steps, bool) or not isinstance(request.steps, int) or not 4 <= request.steps <= 8:
        raise AudioProviderError("audio_steps_invalid")
    if (
        isinstance(request.cfg_scale, bool)
        or not isinstance(request.cfg_scale, (int, float))
        or not math.isfinite(float(request.cfg_scale))
        or not 1 <= float(request.cfg_scale) <= 25
    ):
        raise AudioProviderError("audio_cfg_scale_invalid")


def _is_mp3(raw: bytes) -> bool:
    if len(raw) >= 10 and raw.startswith(b"ID3"):
        return True
    return len(raw) >= 4 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0


def _is_wav(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


_STATUS_CODES = {
    400: "audio_request_invalid",
    401: "audio_auth_failed",
    402: "audio_credits_insufficient",
    403: "audio_content_moderated",
    404: "audio_job_not_found",
    413: "audio_request_too_large",
    422: "audio_request_rejected",
    429: "audio_rate_limited",
}


class StableAudioProvider:
    """Stable Audio 3.0 text-to-audio client with injectable HTTP transport."""

    name = "stability"
    model = STABLE_AUDIO_MODEL
    credits_per_success = 26

    def __init__(
        self,
        *,
        api_key: str,
        model: str = STABLE_AUDIO_MODEL,
        base_url: str = DEFAULT_STABILITY_BASE_URL,
        timeout_seconds: float = 30,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: HttpTransport | None = None,
        client_id: str | None = None,
        client_version: str | None = None,
    ) -> None:
        self._api_key = _validate_header_value(api_key, "audio_api_key_missing")
        if model != STABLE_AUDIO_MODEL:
            raise AudioProviderError("audio_model_invalid")
        self.base_url = _validate_base_url(base_url)
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise AudioProviderError("audio_timeout_invalid") from exc
        if not math.isfinite(timeout) or not 1 <= timeout <= 300:
            raise AudioProviderError("audio_timeout_invalid")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 12 <= max_response_bytes <= ABSOLUTE_MAX_RESPONSE_BYTES
        ):
            raise AudioProviderError("audio_response_limit_invalid")
        self.timeout_seconds = timeout
        self.max_response_bytes = max_response_bytes
        self._transport = transport or UrllibTransport()
        self._client_id = self._optional_client_header(client_id, "audio_client_id_invalid")
        self._client_version = self._optional_client_header(client_version, "audio_client_version_invalid")

    @staticmethod
    def _optional_client_header(value: str | None, code: str) -> str | None:
        if value is None:
            return None
        normalized = _validate_header_value(value, code)
        if len(normalized) > 256:
            raise AudioProviderError(code)
        return normalized

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "audio/*",
            # Stability's edge rejects urllib's generic default user agent on
            # some hosts.  Keep this deterministic and free of host/user data.
            "User-Agent": STABILITY_USER_AGENT,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self._client_id is not None:
            headers["stability-client-id"] = self._client_id
        if self._client_version is not None:
            headers["stability-client-version"] = self._client_version
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        content_type: str | None = None,
        submission: bool,
    ) -> HttpResponse:
        try:
            response = self._transport.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(content_type=content_type),
                body=body,
                timeout=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        except AudioTransportError as exc:
            if exc.code == "audio_response_too_large":
                raise AudioProviderError(
                    "audio_response_too_large",
                    outcome_unknown=submission,
                ) from exc
            if submission:
                raise AudioProviderError(
                    "audio_submit_outcome_unknown",
                    outcome_unknown=True,
                ) from exc
            raise AudioProviderError("audio_poll_transport_error", retryable=True) from exc
        if len(response.body) > self.max_response_bytes:
            raise AudioProviderError(
                "audio_response_too_large",
                outcome_unknown=submission,
            )
        declared = _declared_content_length(response.headers)
        if declared is not None and declared > self.max_response_bytes:
            raise AudioProviderError(
                "audio_response_too_large",
                outcome_unknown=submission,
            )
        return response

    @staticmethod
    def _raise_http_error(status: int, *, submission: bool) -> None:
        code = _STATUS_CODES.get(status)
        if code is None:
            code = "audio_provider_unavailable" if status >= 500 else "audio_provider_request_failed"
        retryable = not submission and (status == 429 or status >= 500)
        raise AudioProviderError(code, retryable=retryable, status_code=status)

    def submit(self, request: AudioGenerationRequest) -> AudioProviderJob:
        """Submit exactly one paid generation request; this method never retries."""

        _validate_request(request)
        fields = (
            ("prompt", request.prompt),
            ("model", request.model),
            ("duration", _format_number(float(request.duration_seconds))),
            ("seed", str(request.seed)),
            ("steps", str(request.steps)),
            ("cfg_scale", _format_number(float(request.cfg_scale))),
            ("output_format", request.output_format),
        )
        body, content_type = _multipart_form(fields)
        response = self._request(
            "POST",
            TEXT_TO_AUDIO_PATH,
            body=body,
            content_type=content_type,
            submission=True,
        )
        if response.status != 202:
            self._raise_http_error(response.status, submission=True)
        try:
            payload = _json_object(response.body)
            generation_id = _validate_generation_id(payload.get("id"), response=True)
        except AudioProviderError as exc:
            raise AudioProviderError("audio_submit_response_invalid") from exc
        return AudioProviderJob(generation_id, "submitted")

    def poll(self, external_job_id: str) -> AudioProviderJob:
        """Poll one existing job; no generation POST can occur on this path."""

        generation_id = _validate_generation_id(external_job_id)
        encoded_generation_id = urllib.parse.quote(generation_id, safe="")
        response = self._request(
            "GET",
            f"{AUDIO_RESULTS_PATH}/{encoded_generation_id}",
            body=None,
            submission=False,
        )
        if response.status == 202:
            try:
                payload = _json_object(response.body)
                response_id = _validate_generation_id(payload.get("id"))
            except AudioProviderError as exc:
                raise AudioProviderError("audio_poll_response_invalid", retryable=True) from exc
            if response_id != generation_id or payload.get("status") != "in-progress":
                raise AudioProviderError("audio_poll_response_invalid", retryable=True)
            return AudioProviderJob(generation_id, "in_progress")
        if response.status == 200:
            return AudioProviderJob(generation_id, "completed", self._artifact(response))
        self._raise_http_error(response.status, submission=False)
        raise AssertionError("unreachable")

    @staticmethod
    def _artifact(response: HttpResponse) -> AudioArtifact:
        raw_content_type = _header(response.headers, "content-type") or ""
        content_type = raw_content_type.partition(";")[0].strip().casefold()
        if content_type in {"audio/mpeg", "audio/mp3"}:
            output_format = "mp3"
            valid = _is_mp3(response.body)
        elif content_type in {"audio/wav", "audio/x-wav"}:
            output_format = "wav"
            valid = _is_wav(response.body)
        else:
            raise AudioProviderError("audio_result_content_type_invalid")
        if not valid:
            raise AudioProviderError("audio_result_file_invalid")

        raw_finish_reason = _header(response.headers, "finish-reason")
        finish_reason = raw_finish_reason.strip() if raw_finish_reason is not None else "HTTP_200_AUDIO"
        if raw_finish_reason is not None and finish_reason != "SUCCESS":
            raise AudioProviderError("audio_result_finish_reason_invalid")
        raw_seed = _header(response.headers, "seed")
        seed: int | None = None
        if raw_seed is not None:
            try:
                seed = int(raw_seed.strip())
            except ValueError:
                raise AudioProviderError("audio_result_seed_invalid") from None
            if not 0 <= seed <= MAX_SEED:
                raise AudioProviderError("audio_result_seed_invalid")
        request_id = _header(response.headers, "x-request-id")
        return AudioArtifact(
            data=response.body,
            content_type=content_type,
            output_format=output_format,
            seed=seed,
            finish_reason=finish_reason,
            request_id=request_id,
        )


def provider_from_environment(env: Mapping[str, str] | None = None) -> StableAudioProvider:
    """Build a provider without making any API request."""

    values = os.environ if env is None else env
    name = values.get("NAZ_AUDIO_PROVIDER", "disabled").strip().casefold()
    if name == "disabled":
        raise AudioProviderError("audio_provider_disabled")
    if name != "stability":
        raise AudioProviderError("audio_provider_unknown")
    try:
        timeout = float(values.get("NAZ_AUDIO_HTTP_TIMEOUT_SECONDS", "30"))
        max_response_bytes = int(
            values.get("NAZ_AUDIO_MAX_RESPONSE_BYTES", str(DEFAULT_MAX_RESPONSE_BYTES))
        )
    except (TypeError, ValueError) as exc:
        raise AudioProviderError("audio_provider_config_invalid") from exc
    return StableAudioProvider(
        api_key=values.get("NAZ_AUDIO_API_KEY", ""),
        model=values.get("NAZ_AUDIO_MODEL", STABLE_AUDIO_MODEL),
        base_url=values.get("NAZ_AUDIO_BASE_URL", DEFAULT_STABILITY_BASE_URL),
        timeout_seconds=timeout,
        max_response_bytes=max_response_bytes,
        client_id=values.get("NAZ_AUDIO_CLIENT_ID") or None,
        client_version=values.get("NAZ_AUDIO_CLIENT_VERSION") or None,
    )
