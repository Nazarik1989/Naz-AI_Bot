"""Private, original music library for Naz Story-first Reel composition.

This module owns a fixed eight-track generation plan and a durable journal for
asynchronous provider tasks.  It has no scheduler or publication entry point.
Paid submissions are made only by :mod:`naz_audio_library` after an explicit
operator confirmation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Protocol, Sequence

from story_audio_evidence import beat_evidence_is_valid


LIBRARY_SCHEMA = "naz-story-audio-library-v1"
SIDECAR_SCHEMA = "naz-story-audio-track-v1"
GENERATION_STATE_SCHEMA = "naz-story-audio-generation-v1"
GENERATION_STATE_FILE = ".naz-audio-generation-v1.json"
GENERATION_LOCK_FILE = ".naz-audio-generation.lock"
MAX_INITIAL_TRACKS = 8
TERMS_REFERENCE = "https://stability.ai/license"
RETRYABLE_LOCAL_ANALYSIS_CODES = frozenset({
    "audio_analysis_tool_unavailable",
    "audio_analysis_timeout",
    "audio_analysis_process_failed",
    "audio_analysis_output_too_large",
})
REVALIDATABLE_RECEIPT_CODES = frozenset({
    "audio_result_finish_reason_invalid", "audio_result_seed_invalid",
})
AUDIO_COMPLETION_EVIDENCE = frozenset({"SUCCESS", "HTTP_200_AUDIO"})
AUDIO_RESULT_CONTRACT = "audio-result.v2"


class AudioLibraryError(RuntimeError):
    """A redacted, operator-safe library error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AudioTrackSpec:
    track_id: str
    lane: str
    bpm: int
    duration_seconds: int
    tags: tuple[str, ...]
    prompt: str
    model: str = "stable-audio-3"
    output_format: str = "mp3"
    beats_per_bar: int = 4

    @property
    def seed(self) -> int:
        raw = hashlib.sha256(f"naz-story-audio|{self.track_id}".encode("utf-8")).digest()
        return int.from_bytes(raw[:4], "big") & 0x7FFFFFFF

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


# Prompts intentionally describe only broad musical attributes.  They contain
# no artist, song-title, uploaded-audio, sample, or imitation reference.
INITIAL_TRACK_SPECS: tuple[AudioTrackSpec, ...] = (
    AudioTrackSpec(
        "naz-midnight-wave-01", "midnight_wave", 148, 64,
        ("night", "tension", "systems", "motion", "focus"),
        "Original instrumental electronic cue at 148 BPM in a minor key. "
        "Dark hardwave atmosphere, controlled sub bass, granular cold pads, "
        "crisp half-time drums, a short original motif, night-drive tension, "
        "clean downbeat opening and clean ending. No vocals, no spoken words, "
        "no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-midnight-wave-02", "midnight_wave", 152, 60,
        ("night", "energy", "city", "focus", "future"),
        "Original instrumental electronic cue at 152 BPM in a minor key. "
        "Precise hardwave pulse, deep restrained sub bass, glassy synth air, "
        "tight half-time percussion, compact original hook, nocturnal momentum, "
        "clean downbeat opening and clean ending. No vocals, no spoken words, "
        "no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-midnight-wave-03", "midnight_wave", 144, 72,
        ("night", "reflective", "systems", "emotion", "future"),
        "Original instrumental electronic cue at 144 BPM in a minor key. "
        "Spacious hardwave texture, weighted sub bass, distant granular pads, "
        "measured half-time drums, restrained original motif, reflective future "
        "tension, clean downbeat opening and clean ending. No vocals, no spoken "
        "words, no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-dark-melodic-house-01", "dark_melodic_house", 122, 64,
        ("daily", "focus", "builder", "city", "progress"),
        "Original instrumental dark melodic house cue at 122 BPM in a minor key. "
        "Restrained four-on-the-floor groove, precise low end, cold atmospheric "
        "chords, tactile percussion, concise original hook, focused forward "
        "motion, clean downbeat opening and clean ending. No vocals, no spoken "
        "words, no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-dark-melodic-house-02", "dark_melodic_house", 124, 72,
        ("systems", "focus", "builder", "prototype", "reveal"),
        "Original instrumental dark melodic house cue at 124 BPM in a minor key. "
        "Controlled club pulse, polished sub bass, evolving cold synth layers, "
        "precise mechanical percussion, compact original motif, prototype reveal "
        "energy, clean downbeat opening and clean ending. No vocals, no spoken "
        "words, no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-dark-melodic-house-03", "dark_melodic_house", 126, 60,
        ("energy", "motion", "prototype", "confident", "future"),
        "Original instrumental dark melodic house cue at 126 BPM in a minor key. "
        "Firm four-on-the-floor rhythm, dense clean low end, metallic synth "
        "accents, disciplined percussion, short original hook, confident future "
        "motion, clean downbeat opening and clean ending. No vocals, no spoken "
        "words, no samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-emotional-future-garage-01", "emotional_future_garage", 100, 72,
        ("reflective", "human", "dialogue", "emotion", "warm"),
        "Original instrumental emotional future-garage cue at 100 BPM in a minor "
        "key. Shuffled two-step drums, soft deep sub bass, airy cold pads, subtle "
        "organic texture, small original melodic phrase, human reflective tension, "
        "clean downbeat opening and clean ending. No vocals, no spoken words, no "
        "samples, no imitation of any existing recording.",
    ),
    AudioTrackSpec(
        "naz-emotional-future-garage-02", "emotional_future_garage", 108, 64,
        ("reflective", "night", "resolve", "emotion", "human"),
        "Original instrumental emotional future-garage cue at 108 BPM in a minor "
        "key. Detailed two-step rhythm, controlled sub bass, wide luminous pads, "
        "delicate granular texture, concise original motif, tension resolving into "
        "clarity, clean downbeat opening and clean ending. No vocals, no spoken "
        "words, no samples, no imitation of any existing recording.",
    ),
)


class AudioProvider(Protocol):
    name: str
    model: str

    def submit(self, request: Any) -> Any: ...
    def poll(self, external_job_id: str) -> Any: ...


class AudioAnalyzer(Protocol):
    def preflight(self) -> None: ...
    def analyze(self, path: Path) -> Any: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_code(exc: BaseException) -> str:
    value = str(getattr(exc, "code", "audio_provider_error"))
    allowed = "".join(
        character for character in value
        if character.isascii() and (character.isalnum() or character == "_")
    )
    return allowed[:80] or "audio_provider_error"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def beat_grid(spec: AudioTrackSpec) -> tuple[float, ...]:
    step = 60.0 / float(spec.bpm)
    count = int(math.floor(float(spec.duration_seconds) / step))
    return tuple(round(index * step, 6) for index in range(count + 1))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LibraryLock:
    """An advisory process lock released automatically when a process exits."""

    def __init__(self, root: Path) -> None:
        self.path = root / GENERATION_LOCK_FILE
        self.fd: int | None = None

    def __enter__(self) -> "LibraryLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b" ")
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(self.fd)
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise AudioLibraryError("audio_library_locked") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            try:
                os.lseek(self.fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _state_path(root: Path) -> Path:
    return root / GENERATION_STATE_FILE


def load_generation_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    if not path.is_file():
        return {"schema": GENERATION_STATE_SCHEMA, "updated_at": _utc_now(), "jobs": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AudioLibraryError("audio_generation_state_invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != GENERATION_STATE_SCHEMA
        or not isinstance(value.get("jobs"), dict)
    ):
        raise AudioLibraryError("audio_generation_state_invalid")
    return value


def save_generation_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _atomic_json(_state_path(root), state)


def audio_path(root: Path, spec: AudioTrackSpec) -> Path:
    return root / f"{spec.track_id}.{spec.output_format}"


def sidecar_path(root: Path, spec: AudioTrackSpec) -> Path:
    path = audio_path(root, spec)
    return path.with_suffix(path.suffix + ".json")


def _valid_artifact_bytes(raw: bytes, output_format: str) -> bool:
    if len(raw) < 64:
        return False
    if output_format == "mp3":
        return raw.startswith(b"ID3") or (raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0)
    if output_format == "wav":
        return raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
    return False


def _valid_grid(raw: Any, *, duration_seconds: float) -> bool:
    if not isinstance(raw, list) or len(raw) < 2:
        return False
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(item) and 0 <= item <= duration_seconds + 0.001 for item in values)
        and all(left < right for left, right in zip(values, values[1:]))
    )


def read_valid_sidecar(root: Path, spec: AudioTrackSpec) -> dict[str, Any] | None:
    media = audio_path(root, spec)
    metadata = sidecar_path(root, spec)
    if not media.is_file() or not metadata.is_file():
        return None
    try:
        row = json.loads(metadata.read_text(encoding="utf-8"))
        rights = row["rights"]
        duration = float(row["duration_seconds"])
        generation = row["generation"]
        valid = (
            isinstance(row, dict)
            and row.get("schema") == SIDECAR_SCHEMA
            and row.get("track_id") == spec.track_id
            and row.get("lane") == spec.lane
            and 40.0 <= float(row.get("bpm")) <= 240.0
            and 12.0 <= duration <= 380.0
            and abs(duration - float(spec.duration_seconds)) <= 3.0
            and float(row.get("requested_duration_seconds")) == float(spec.duration_seconds)
            and float(row.get("requested_bpm")) == float(spec.bpm)
            and int(row.get("beats_per_bar")) == spec.beats_per_bar
            and tuple(row.get("tags", ())) == spec.tags
            and row.get("checksum") == sha256_file(media)
            and row.get("generation_prompt_sha256") == spec.prompt_sha256
            and row.get("license") == "stability-generated-output"
            and row.get("source") == "naz-private-generated-library"
            and isinstance(rights, dict)
            and rights.get("origin") == "text_to_audio"
            and rights.get("provider") == "stability-ai"
            and rights.get("model") == spec.model
            and rights.get("third_party_audio_input") is False
            and rights.get("artist_or_track_reference") is False
            and bool(str(rights.get("terms_reference", "")).strip())
            and bool(str(rights.get("generated_at", "")).strip())
            and isinstance(generation, dict)
            and generation.get("output_format") == spec.output_format
            and generation.get("finish_reason") in AUDIO_COMPLETION_EVIDENCE
            and type(generation.get("seed")) is int
            and generation.get("seed") == spec.seed
            and (
                generation.get("seed_source") in {"provider_header", "request"}
                or (
                    "seed_source" not in generation
                    and generation.get("finish_reason") == "SUCCESS"
                )
            )
            and row.get("beat_grid_source") == "actual-audio-derived-beat-track-v1"
            and row.get("beat_evidence_source") == "actual-audio-derived-onset-match-v1"
            and beat_evidence_is_valid(row.get("beat_grid", ()), row.get("beat_evidence", ()))
            and isinstance(row.get("audio_analysis"), dict)
            and row["audio_analysis"].get("source") == "actual-audio-derived-beat-track-v1"
            and bool(str(row["audio_analysis"].get("analyzer", "")).strip())
            and 0.2 <= float(row["audio_analysis"].get("confidence")) <= 1.0
            and 0.25 <= float(row["audio_analysis"].get("peak_prominence")) <= 1.0
            and 0.25 <= float(row["audio_analysis"].get("onset_alignment_fraction")) <= 1.0
            and 0.25 <= float(row["audio_analysis"].get("grid_onset_coverage")) <= 1.0
            and abs(
                float(row["audio_analysis"].get("grid_onset_coverage"))
                - sum(row["beat_evidence"]) / float(len(row["beat_evidence"]))
            ) <= 0.000001
            and len(row.get("beat_grid", ())) >= 16
            and _valid_grid(row.get("beat_grid"), duration_seconds=duration)
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return row if valid else None


def library_plan(root: Path) -> dict[str, Any]:
    rows = []
    for spec in INITIAL_TRACK_SPECS:
        rows.append({
            "track_id": spec.track_id,
            "lane": spec.lane,
            "bpm": spec.bpm,
            "duration_seconds": spec.duration_seconds,
            "status": "ready" if read_valid_sidecar(root, spec) else "missing",
        })
    return {
        "schema": LIBRARY_SCHEMA,
        "track_count": len(rows),
        "ready_count": sum(row["status"] == "ready" for row in rows),
        "tracks": rows,
        "live_api_called": False,
    }


def _provider_request(spec: AudioTrackSpec) -> Any:
    values = {
        "prompt": spec.prompt,
        "duration_seconds": spec.duration_seconds,
        "output_format": spec.output_format,
        "model": spec.model,
        "seed": spec.seed,
        "steps": 8,
        "cfg_scale": 1.0,
    }
    try:
        from story_audio_provider import AudioGenerationRequest
    except ImportError:
        return SimpleNamespace(**values)
    return AudioGenerationRequest(**values)


def _job_id(job: Any) -> str:
    value = str(getattr(job, "external_job_id", "")).strip()
    if not value:
        raise AudioLibraryError("audio_provider_job_id_missing")
    return value


def _artifact_bytes(artifact: Any) -> bytes:
    value = getattr(artifact, "data", None)
    if value is None:
        value = getattr(artifact, "bytes", None)
    if value is None:
        value = getattr(artifact, "content", None)
    if not isinstance(value, bytes):
        raise AudioLibraryError("audio_artifact_missing")
    return value


def _write_completed_artifact(
    root: Path, spec: AudioTrackSpec, artifact: Any, *, provider: AudioProvider,
    analyzer: AudioAnalyzer, requested_seed: int,
) -> dict[str, Any]:
    raw = _artifact_bytes(artifact)
    artifact_format = str(getattr(artifact, "output_format", spec.output_format)).strip().casefold()
    content_type = str(getattr(artifact, "content_type", "")).partition(";")[0].strip().casefold()
    finish_reason = str(getattr(artifact, "finish_reason", "")).strip()
    provider_seed = getattr(artifact, "seed", None)
    seed = requested_seed if provider_seed is None else provider_seed
    seed_source = "request" if provider_seed is None else "provider_header"
    expected_content_types = {"mp3": {"audio/mpeg", "audio/mp3"}, "wav": {"audio/wav", "audio/x-wav"}}
    if (
        artifact_format != spec.output_format
        or content_type not in expected_content_types.get(artifact_format, set())
        or finish_reason not in AUDIO_COMPLETION_EVIDENCE
        or not isinstance(seed, int)
        or seed != requested_seed
        or not _valid_artifact_bytes(raw, artifact_format)
    ):
        raise AudioLibraryError("audio_artifact_invalid")
    media = audio_path(root, spec)
    if media.exists() and sha256_file(media) != sha256_bytes(raw):
        raise AudioLibraryError("audio_output_conflict")
    root.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{spec.track_id}-analysis-", suffix=f".{artifact_format}", dir=root,
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            analysis = analyzer.analyze(staged)
            actual_duration = float(getattr(analysis, "duration_seconds"))
            actual_bpm = float(getattr(analysis, "bpm"))
            actual_grid = [float(value) for value in getattr(analysis, "beat_grid")]
            actual_evidence = list(getattr(analysis, "beat_evidence"))
            analysis_source = str(getattr(analysis, "source", "")).strip()
            analyzer_name = str(getattr(analysis, "analyzer", "")).strip()
            analysis_confidence = float(getattr(analysis, "confidence"))
            peak_prominence = float(getattr(analysis, "peak_prominence"))
            onset_fraction = float(getattr(analysis, "onset_alignment_fraction"))
            grid_coverage = float(getattr(analysis, "grid_onset_coverage"))
        except Exception as exc:
            code = _safe_code(exc) if getattr(exc, "code", None) else "audio_analysis_failed"
            if not code.startswith(("audio_analysis_", "audio_duration_", "audio_beat_", "audio_stream_")):
                code = "audio_analysis_failed"
            raise AudioLibraryError(code) from exc
        if (
            not math.isfinite(actual_duration)
            or not math.isfinite(actual_bpm)
            or not 12.0 <= actual_duration <= 380.0
            or abs(actual_duration - float(spec.duration_seconds)) > 3.0
            or not 40.0 <= actual_bpm <= 240.0
            or analysis_source != "actual-audio-derived-beat-track-v1"
            or not analyzer_name
            or not 0.2 <= analysis_confidence <= 1.0
            or not 0.25 <= peak_prominence <= 1.0
            or not 0.25 <= onset_fraction <= 1.0
            or not 0.25 <= grid_coverage <= 1.0
            or not beat_evidence_is_valid(actual_grid, actual_evidence)
            or abs(grid_coverage - sum(actual_evidence) / float(len(actual_evidence))) > 0.000001
            or not _valid_grid(actual_grid, duration_seconds=actual_duration)
            or len(actual_grid) < 16
        ):
            raise AudioLibraryError("audio_analysis_invalid")
    finally:
        staged.unlink(missing_ok=True)
    _atomic_bytes(media, raw)
    request_id = str(getattr(artifact, "request_id", "")).strip()
    row: dict[str, Any] = {
        "schema": SIDECAR_SCHEMA,
        "track_id": spec.track_id,
        "lane": spec.lane,
        "tags": list(spec.tags),
        "bpm": round(actual_bpm, 3),
        "requested_bpm": spec.bpm,
        "duration_seconds": round(actual_duration, 6),
        "requested_duration_seconds": spec.duration_seconds,
        "beats_per_bar": spec.beats_per_bar,
        "beat_grid": [round(value, 6) for value in actual_grid],
        "beat_grid_source": analysis_source,
        "beat_evidence": actual_evidence,
        "beat_evidence_source": "actual-audio-derived-onset-match-v1",
        "audio_analysis": {
            "source": analysis_source,
            "analyzer": analyzer_name,
            "confidence": round(analysis_confidence, 6),
            "peak_prominence": round(peak_prominence, 6),
            "onset_alignment_fraction": round(onset_fraction, 6),
            "grid_onset_coverage": round(grid_coverage, 6),
        },
        "license": "stability-generated-output",
        "source": "naz-private-generated-library",
        "checksum": sha256_bytes(raw),
        "generation_prompt_sha256": spec.prompt_sha256,
        "generation": {
            "output_format": artifact_format,
            "seed": seed,
            "seed_source": seed_source,
            "finish_reason": finish_reason,
            "request_id": request_id or None,
        },
        "rights": {
            "origin": "text_to_audio",
            "provider": "stability-ai",
            "model": str(getattr(provider, "model", spec.model)),
            "third_party_audio_input": False,
            "artist_or_track_reference": False,
            "terms_reference": TERMS_REFERENCE,
            "generated_at": _utc_now(),
            "request_id": request_id or None,
        },
    }
    _atomic_json(sidecar_path(root, spec), row)
    return row


def _poll_job(
    *, root: Path, state: dict[str, Any], spec: AudioTrackSpec,
    job: dict[str, Any], provider: AudioProvider, analyzer: AudioAnalyzer,
) -> str:
    if job.get("result_contract_version") != AUDIO_RESULT_CONTRACT:
        job["result_contract_version"] = AUDIO_RESULT_CONTRACT
        # The one-time recovery marker must be durable before the GET so a
        # crash cannot make one invalid receipt spin forever.
        save_generation_state(root, state)
    if job.get("request_fingerprint") != spec.prompt_sha256:
        job.update({"state": "blocked", "reason_code": "audio_request_fingerprint_mismatch", "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "blocked"
    requested_seed = job.get("requested_seed")
    if requested_seed is None:
        # v1 journals written before requested_seed was persisted are safe to
        # migrate only when their immutable prompt fingerprint still matches.
        requested_seed = spec.seed
        job["requested_seed"] = requested_seed
        save_generation_state(root, state)
    if type(requested_seed) is not int or requested_seed != spec.seed:
        job.update({"state": "blocked", "reason_code": "audio_requested_seed_mismatch", "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "blocked"
    raw_job_id = job.get("external_job_id")
    external_job_id = raw_job_id.strip() if isinstance(raw_job_id, str) else ""
    if not external_job_id:
        # A crash after POST and before receipt persistence is ambiguous.  Never
        # issue another paid POST automatically.
        job.update({"state": "blocked", "reason_code": "audio_submission_ambiguous", "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "blocked"
    try:
        provider_job = provider.poll(external_job_id)
    except Exception as exc:  # provider exceptions are redacted to their code
        state_name = "submitted" if bool(getattr(exc, "retryable", False)) else "failed"
        job.update({"state": state_name, "reason_code": _safe_code(exc), "updated_at": _utc_now()})
        save_generation_state(root, state)
        return state_name
    status = str(getattr(provider_job, "status", "")).strip().casefold()
    if status in {"submitted", "pending", "running", "in_progress"}:
        job.update({"state": "submitted", "reason_code": None, "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "submitted"
    if status in {"failed", "terminal_failed", "cancelled", "canceled"}:
        job.update({"state": "failed", "reason_code": "audio_provider_terminal_failure", "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "failed"
    if status != "completed":
        job.update({"state": "submitted", "reason_code": "audio_provider_status_unknown", "updated_at": _utc_now()})
        save_generation_state(root, state)
        return "submitted"
    try:
        _write_completed_artifact(
            root, spec, getattr(provider_job, "artifact", None),
            provider=provider, analyzer=analyzer, requested_seed=requested_seed,
        )
    except AudioLibraryError as exc:
        state_name = "analysis_pending" if exc.code in RETRYABLE_LOCAL_ANALYSIS_CODES else "failed"
        job.update({"state": state_name, "reason_code": exc.code, "updated_at": _utc_now()})
        save_generation_state(root, state)
        return state_name
    job.update({"state": "completed", "reason_code": None, "completed_at": _utc_now(), "updated_at": _utc_now()})
    save_generation_state(root, state)
    return "completed"


def generate_initial_library(
    *, root: Path, provider: AudioProvider, confirmed_paid_calls: int,
    max_new_tracks: int | None = None, analyzer: AudioAnalyzer | None = None,
) -> dict[str, Any]:
    """Resume jobs and submit at most the explicitly confirmed number of calls.

    A persisted ``external_job_id`` is always polled.  A ``submitting`` journal
    entry without a receipt is treated as ambiguous and never POSTed again.
    Provider failures are not retried by this operation.
    """

    if not 0 <= confirmed_paid_calls <= MAX_INITIAL_TRACKS:
        raise AudioLibraryError("audio_paid_call_confirmation_invalid")
    maximum = confirmed_paid_calls if max_new_tracks is None else max_new_tracks
    if not 0 <= maximum <= MAX_INITIAL_TRACKS or maximum > confirmed_paid_calls:
        raise AudioLibraryError("audio_generation_limit_invalid")
    if str(getattr(provider, "name", "")).casefold() != "stability" or str(
        getattr(provider, "model", "")
    ) != "stable-audio-3":
        raise AudioLibraryError("audio_provider_configuration_invalid")
    if analyzer is None:
        try:
            from story_audio_analysis import FfmpegAudioAnalyzer
        except ImportError as exc:
            raise AudioLibraryError("audio_analyzer_unavailable") from exc
        analyzer = FfmpegAudioAnalyzer()
    root = root.expanduser().resolve()
    with LibraryLock(root):
        state = load_generation_state(root)
        jobs: dict[str, Any] = state["jobs"]
        # Reconcile already complete files before touching the provider.
        for spec in INITIAL_TRACK_SPECS:
            if read_valid_sidecar(root, spec):
                current = jobs.setdefault(spec.track_id, {})
                current.update({"state": "completed", "reason_code": None, "updated_at": _utc_now()})
        save_generation_state(root, state)

        # Poll every durable receipt first.  This cannot create a paid job.
        polled_now = 0
        for spec in INITIAL_TRACK_SPECS:
            job = jobs.get(spec.track_id)
            if (
                isinstance(job, dict)
                and job.get("state") == "failed"
                and job.get("reason_code") in REVALIDATABLE_RECEIPT_CODES
                and job.get("result_contract_version") != AUDIO_RESULT_CONTRACT
                and bool(str(job.get("external_job_id", "")).strip())
            ):
                job.update({"state": "submitted", "reason_code": None, "updated_at": _utc_now()})
                save_generation_state(root, state)
            if isinstance(job, dict) and job.get("state") in {
                "submitted", "submitting", "analysis_pending",
            }:
                polled_now += 1
                _poll_job(
                    root=root, state=state, spec=spec, job=job,
                    provider=provider, analyzer=analyzer,
                )

        submitted_now = 0
        preflight_complete = False
        for spec in INITIAL_TRACK_SPECS:
            if submitted_now >= maximum or read_valid_sidecar(root, spec):
                continue
            current = jobs.get(spec.track_id)
            if isinstance(current, dict) and current.get("state") in {
                "submitting", "submitted", "analysis_pending", "completed", "failed", "blocked",
            }:
                continue
            if not preflight_complete:
                try:
                    analyzer.preflight()
                except Exception as exc:
                    code = _safe_code(exc) if getattr(exc, "code", None) else "audio_analysis_preflight_failed"
                    if code not in RETRYABLE_LOCAL_ANALYSIS_CODES:
                        code = "audio_analysis_preflight_failed"
                    raise AudioLibraryError(code) from exc
                preflight_complete = True
            job = {
                "track_id": spec.track_id,
                "state": "submitting",
                "external_job_id": None,
                "submission_attempts": 1,
                "request_fingerprint": spec.prompt_sha256,
                "requested_seed": spec.seed,
                "result_contract_version": AUDIO_RESULT_CONTRACT,
                "submitted_at": _utc_now(),
                "updated_at": _utc_now(),
                "reason_code": None,
            }
            jobs[spec.track_id] = job
            # Durable intent is written before POST.  A crash can therefore
            # lose a result, but can never silently duplicate a paid call.
            save_generation_state(root, state)
            submitted_now += 1
            try:
                provider_job = provider.submit(_provider_request(spec))
                job.update({
                    "state": "submitted", "external_job_id": _job_id(provider_job),
                    "updated_at": _utc_now(),
                })
            except Exception as exc:
                job.update({"state": "failed", "reason_code": _safe_code(exc), "updated_at": _utc_now()})
            save_generation_state(root, state)
            if job["state"] == "failed":
                # Fail fast: an account/configuration rejection is likely to
                # affect the whole batch.  A later explicit run may continue
                # with other catalog items, but this paid run stops here.
                break

        statuses = {
            spec.track_id: str(jobs.get(spec.track_id, {}).get("state", "missing"))
            for spec in INITIAL_TRACK_SPECS
        }
        failure_reason_codes = sorted({
            _safe_code(SimpleNamespace(code=row.get("reason_code")))
            for row in jobs.values()
            if isinstance(row, dict) and row.get("state") == "failed" and row.get("reason_code")
        })
        analysis_pending_reason_codes = sorted({
            _safe_code(SimpleNamespace(code=row.get("reason_code")))
            for row in jobs.values()
            if (
                isinstance(row, dict)
                and row.get("state") == "analysis_pending"
                and row.get("reason_code")
            )
        })
        return {
            "schema": LIBRARY_SCHEMA,
            "track_count": len(INITIAL_TRACK_SPECS),
            "ready_count": sum(read_valid_sidecar(root, spec) is not None for spec in INITIAL_TRACK_SPECS),
            "submitted_now": submitted_now,
            "polled_now": polled_now,
            "paid_call_cap": maximum,
            "statuses": statuses,
            "failed_count": sum(value == "failed" for value in statuses.values()),
            "failure_reason_codes": failure_reason_codes,
            "analysis_pending_count": sum(
                value == "analysis_pending" for value in statuses.values()
            ),
            "analysis_pending_reason_codes": analysis_pending_reason_codes,
            "live_api_called": submitted_now > 0 or polled_now > 0,
        }


def specs_as_safe_rows(specs: Sequence[AudioTrackSpec] = INITIAL_TRACK_SPECS) -> list[dict[str, Any]]:
    """Return plan metadata without generation prompts."""

    return [
        {
            "track_id": item.track_id, "lane": item.lane, "bpm": item.bpm,
            "duration_seconds": item.duration_seconds, "tags": list(item.tags),
        }
        for item in specs
    ]


def validate_initial_catalog() -> None:
    lanes = [spec.lane for spec in INITIAL_TRACK_SPECS]
    expected = {"midnight_wave": 3, "dark_melodic_house": 3, "emotional_future_garage": 2}
    if (
        len(INITIAL_TRACK_SPECS) != MAX_INITIAL_TRACKS
        or len({spec.track_id for spec in INITIAL_TRACK_SPECS}) != MAX_INITIAL_TRACKS
    ):
        raise AudioLibraryError("audio_catalog_invalid")
    if {lane: lanes.count(lane) for lane in expected} != expected:
        raise AudioLibraryError("audio_catalog_invalid")
    for spec in INITIAL_TRACK_SPECS:
        if not 45 <= spec.duration_seconds <= 90 or not 40 <= spec.bpm <= 240 or spec.output_format != "mp3":
            raise AudioLibraryError("audio_catalog_invalid")


validate_initial_catalog()
