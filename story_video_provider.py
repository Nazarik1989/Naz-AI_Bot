"""Replaceable asynchronous video providers for Naz Story-first production."""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


RUNWAY_API_VERSION = "2024-11-06"
DEFAULT_RUNWAY_BASE_URL = "https://api.dev.runwayml.com/v1"
TERMINAL_FAILURES = {"failed", "cancelled", "canceled"}


class ProviderError(RuntimeError):
    """Redacted provider error safe for logs and manifests."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class SceneRequest:
    scene_id: str
    prompt: str
    duration_seconds: int
    reference_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ProviderJob:
    external_job_id: str
    status: str
    output_url: str | None = None
    failure_code: str | None = None


class VideoProvider(Protocol):
    name: str
    model: str
    supports_reference: bool

    def submit(self, request: SceneRequest) -> ProviderJob: ...
    def retrieve(self, external_job_id: str) -> ProviderJob: ...
    def download(self, job: ProviderJob, destination: Path) -> None: ...
    def cancel(self, external_job_id: str) -> None: ...


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        body: bytes | None, timeout: float,
    ) -> tuple[int, Mapping[str, str], bytes]: ...


class UrllibTransport:
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        body: bytes | None, timeout: float,
    ) -> tuple[int, Mapping[str, str], bytes]:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            # Do not retain response bodies: they may echo prompts or credentials.
            return exc.code, dict(exc.headers.items()), b""
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("provider_transport_error", retryable=True) from exc


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider_response_invalid", retryable=True) from exc
    if not isinstance(value, dict):
        raise ProviderError("provider_response_invalid", retryable=True)
    return value


class RunwayVideoProvider:
    """Runway asynchronous task adapter; all I/O is injectable for tests."""

    name = "runway"
    supports_reference = True

    def __init__(
        self, *, api_key: str, model: str = "gen4.5",
        base_url: str = DEFAULT_RUNWAY_BASE_URL, timeout_seconds: float = 30,
        transport: HttpTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ProviderError("video_api_key_missing")
        self._api_key = api_key.strip()
        self.model = model.strip() or "gen4.5"
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._transport = transport or UrllibTransport()

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Runway-Version": RUNWAY_API_VERSION,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, _, raw = self._transport.request(
            method, f"{self.base_url}{path}", headers=self._headers(), body=body,
            timeout=self.timeout_seconds,
        )
        if status == 429 or status >= 500:
            raise ProviderError("provider_temporarily_unavailable", retryable=True)
        if not 200 <= status < 300:
            raise ProviderError("provider_request_rejected")
        return _json_object(raw) if raw else {}

    @staticmethod
    def _reference_data_uri(path: Path) -> str:
        import base64

        suffix = path.suffix.casefold()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix)
        if mime is None or not path.is_file():
            raise ProviderError("approved_reference_invalid")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ProviderError("approved_reference_too_large")
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

    def submit(self, request: SceneRequest) -> ProviderJob:
        payload: dict[str, Any] = {
            "model": self.model,
            "promptText": request.prompt,
            "duration": request.duration_seconds,
            "ratio": "720:1280",
        }
        endpoint = "/text_to_video"
        if request.reference_path is not None:
            payload["promptImage"] = self._reference_data_uri(request.reference_path)
            endpoint = "/image_to_video"
        result = self._call("POST", endpoint, payload)
        job_id = str(result.get("id", "")).strip()
        if not job_id:
            raise ProviderError("provider_job_id_missing", retryable=True)
        return ProviderJob(job_id, "submitted")

    def retrieve(self, external_job_id: str) -> ProviderJob:
        result = self._call("GET", f"/tasks/{external_job_id}")
        raw_status = str(result.get("status", "")).casefold()
        if raw_status in {"pending", "throttled", "running", "in_progress"}:
            return ProviderJob(external_job_id, "in_progress")
        if raw_status in {"succeeded", "completed"}:
            output = result.get("output")
            url = output[0] if isinstance(output, list) and output else result.get("outputUrl")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ProviderError("provider_output_url_missing", retryable=True)
            return ProviderJob(external_job_id, "completed", output_url=url)
        if raw_status in TERMINAL_FAILURES:
            return ProviderJob(external_job_id, "terminal_failed", failure_code="provider_terminal_failure")
        raise ProviderError("provider_status_unknown", retryable=True)

    def download(self, job: ProviderJob, destination: Path) -> None:
        if not job.output_url:
            raise ProviderError("provider_output_url_missing")
        status, headers, raw = self._transport.request(
            "GET", job.output_url, headers={}, body=None, timeout=self.timeout_seconds,
        )
        content_type = str(headers.get("Content-Type", headers.get("content-type", ""))).casefold()
        if status != 200:
            raise ProviderError("provider_download_failed", retryable=status == 429 or status >= 500)
        if "html" in content_type or len(raw) < 12 or raw[4:8] != b"ftyp":
            raise ProviderError("provider_download_not_mp4", retryable=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)

    def cancel(self, external_job_id: str) -> None:
        status, _, _ = self._transport.request(
            "DELETE", f"{self.base_url}/tasks/{external_job_id}",
            headers=self._headers(), body=None, timeout=self.timeout_seconds,
        )
        if status in {200, 202, 204, 404}:
            return
        if status == 429 or status >= 500:
            raise ProviderError("provider_cancel_uncertain", retryable=True)
        raise ProviderError("provider_request_rejected")


class FakeVideoProvider:
    """In-memory provider used by tests; it never owns a network transport."""

    name = "fake"
    model = "fake-motion-v1"
    supports_reference = True

    def __init__(self, media_source: Path | None = None) -> None:
        self.media_source = media_source
        self.submissions: list[SceneRequest] = []
        self.jobs: dict[str, ProviderJob] = {}
        self.submit_count = 0

    def submit(self, request: SceneRequest) -> ProviderJob:
        self.submit_count += 1
        job = ProviderJob(f"fake-{request.scene_id}-{self.submit_count}", "submitted")
        self.submissions.append(request)
        self.jobs[job.external_job_id] = ProviderJob(job.external_job_id, "in_progress")
        return job

    def retrieve(self, external_job_id: str) -> ProviderJob:
        return self.jobs[external_job_id]

    def complete(self, external_job_id: str) -> None:
        self.jobs[external_job_id] = ProviderJob(external_job_id, "completed", "fake://media")

    def fail(self, external_job_id: str) -> None:
        self.jobs[external_job_id] = ProviderJob(external_job_id, "terminal_failed", failure_code="provider_terminal_failure")

    def download(self, job: ProviderJob, destination: Path) -> None:
        if self.media_source is None:
            raise ProviderError("fake_media_missing")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.media_source, destination)

    def cancel(self, external_job_id: str) -> None:
        self.jobs[external_job_id] = ProviderJob(external_job_id, "terminal_failed", failure_code="provider_timeout")


def provider_from_environment(env: Mapping[str, str] | None = None) -> VideoProvider:
    values = os.environ if env is None else env
    name = values.get("NAZ_VIDEO_PROVIDER", "disabled").strip().casefold()
    if name == "runway":
        return RunwayVideoProvider(
            api_key=values.get("NAZ_VIDEO_API_KEY", ""),
            model=values.get("NAZ_VIDEO_MODEL", "gen4.5"),
            base_url=values.get("NAZ_VIDEO_BASE_URL", DEFAULT_RUNWAY_BASE_URL),
            timeout_seconds=float(values.get("NAZ_VIDEO_HTTP_TIMEOUT_SECONDS", "30")),
        )
    raise ProviderError("video_provider_disabled" if name == "disabled" else "video_provider_unknown")
