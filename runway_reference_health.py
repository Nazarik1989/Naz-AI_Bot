"""Private, versioned health state for Runway identity-reference routes.

The registry stores digests and closed status codes only.  Reference paths,
image bytes, provider prose and prompts never cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


REGISTRY_SCHEMA = "naz-runway-reference-health-v1"
HEALTH_POLICY_VERSION = "naz-runway-reference-health-v1"
REFERENCE_PROFILE_VERSION = "naz-reference-profile.v2"
PROMPT_POLICY_VERSION = "runway-identity-prompt-v1"
HEALTH_STATES = frozenset({
    "unknown", "healthy", "degraded", "quarantined", "revalidation_required"
})
FAILURE_CATEGORIES = frozenset({
    "bad_output",
    "moderation_terminal",
    "input_safety_terminal",
    "asset_invalid",
    "provider_preprocessing_internal",
    "provider_dependency_unavailable",
    "provider_internal_unknown",
    "unknown_terminal",
})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9_]{1,63}")
_SCENE_RE = re.compile(r"[0-9]{2}_[a-z][a-z0-9_]{1,63}")


class ReferenceHealthError(RuntimeError):
    """Closed, privacy-safe reference-health failure."""


@dataclass(frozen=True, slots=True)
class ReferenceRoute:
    provider_name: str
    keyframe_model: str
    reference_profile_version: str
    prompt_policy_version: str
    reference_role: str
    reference_digest: str
    reference_set_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.provider_name) is not str
            or self.provider_name != "runway"
            or type(self.keyframe_model) is not str
            or self.keyframe_model != "gen4_image"
            or type(self.reference_profile_version) is not str
            or self.reference_profile_version != REFERENCE_PROFILE_VERSION
            or type(self.prompt_policy_version) is not str
            or self.prompt_policy_version != PROMPT_POLICY_VERSION
            or type(self.reference_role) is not str
            or not _ROLE_RE.fullmatch(self.reference_role)
            or type(self.reference_digest) is not str
            or not _DIGEST_RE.fullmatch(self.reference_digest)
            or type(self.reference_set_digest) is not str
            or not _DIGEST_RE.fullmatch(self.reference_set_digest)
        ):
            raise ReferenceHealthError("reference_health_route_invalid")

    @property
    def identity(self) -> str:
        canonical = json.dumps(
            asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def sha256_file(path: Path, *, maximum_bytes: int = 20 * 1024 * 1024) -> str:
    """Hash one regular, non-symlink file without returning its identity."""
    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ReferenceHealthError("reference_health_file_invalid")
        stat = candidate.stat()
        if stat.st_size <= 0 or stat.st_size > maximum_bytes:
            raise ReferenceHealthError("reference_health_file_invalid")
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ReferenceHealthError("reference_health_file_invalid") from exc


def reference_set_digest(rows: Iterable[tuple[str, str]]) -> str:
    normalized = list(rows)
    if not normalized or len(normalized) > 3 or any(
        type(role) is not str
        or not _ROLE_RE.fullmatch(role)
        or type(digest) is not str
        or not _DIGEST_RE.fullmatch(digest)
        for role, digest in normalized
    ):
        raise ReferenceHealthError("reference_health_set_invalid")
    canonical = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunwayFailureDecision:
    safe_provider_failure_code: str | None
    normalized_category: str
    automatic_retry: bool
    same_input_retry: bool
    delayed_retry_eligible: bool
    input_repair_required: bool
    corrected_input_required: bool


def classify_provider_failure_code(value: object) -> RunwayFailureDecision:
    """Map an allowlisted Runway failureCode to a finite retry decision."""
    code: str | None = None
    if type(value) is str:
        candidate = value.strip().upper()
        if candidate and len(candidate) <= 128 and re.fullmatch(r"[A-Z0-9_.-]+", candidate):
            code = candidate
    invalid_value = value is not None and code is None
    if invalid_value:
        category = "unknown_terminal"
    elif code == "INPUT_PREPROCESSING.SAFETY.TEXT":
        category = "input_safety_terminal"
    elif code is not None and code.startswith("SAFETY."):
        category = "moderation_terminal"
    elif code == "ASSET.INVALID" or (code or "").startswith("ASSET.INVALID."):
        category = "asset_invalid"
    elif code is not None and code.startswith("INTERNAL.BAD_OUTPUT."):
        category = "bad_output"
    elif code == "INPUT_PREPROCESSING.INTERNAL":
        category = "provider_preprocessing_internal"
    elif code == "THIRD_PARTY.UNAVAILABLE":
        category = "provider_dependency_unavailable"
    elif code is None or code == "INTERNAL":
        category = "provider_internal_unknown"
    else:
        category = "unknown_terminal"
    delayed = category in {
        "provider_preprocessing_internal", "provider_dependency_unavailable"
    }
    return RunwayFailureDecision(
        safe_provider_failure_code=code,
        normalized_category=category,
        automatic_retry=delayed,
        same_input_retry=False,
        delayed_retry_eligible=delayed,
        input_repair_required=category == "asset_invalid",
        corrected_input_required=category == "bad_output",
    )


class ReferenceHealthRegistry:
    """Atomic snapshot plus no-clobber event history owned by the worker."""

    def __init__(self, root: Path) -> None:
        candidate = Path(root).expanduser().resolve()
        if candidate == Path(candidate.anchor) or not candidate.is_absolute():
            raise ReferenceHealthError("reference_health_root_invalid")
        self.root = candidate
        self.state_path = candidate / "registry.json"
        self.events_root = candidate / "events"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.events_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.events_root, 0o700)
        except OSError as exc:
            raise ReferenceHealthError("reference_health_permissions_invalid") from exc
        if self.root.is_symlink() or self.events_root.is_symlink():
            raise ReferenceHealthError("reference_health_root_invalid")

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema": REGISTRY_SCHEMA, "records": {}, "migrations": {}}
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise ReferenceHealthError("reference_health_state_invalid")
        try:
            raw = self.state_path.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ReferenceHealthError("reference_health_state_invalid")
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReferenceHealthError("reference_health_state_invalid") from exc
        if (
            type(payload) is not dict
            or payload.get("schema") != REGISTRY_SCHEMA
            or type(payload.get("records")) is not dict
            or type(payload.get("migrations")) is not dict
        ):
            raise ReferenceHealthError("reference_health_state_invalid")
        return payload

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._load(), sort_keys=True))

    def _write(self, payload: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        self._ensure_root()
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        event_bytes = json.dumps(
            event, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        event_id = hashlib.sha256(event_bytes).hexdigest()
        event_path = self.events_root / f"{event_id}.json"
        try:
            descriptor = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(event_bytes)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            existing = event_path.read_bytes()
            if existing != event_bytes:
                raise ReferenceHealthError("reference_health_event_conflict")
        except OSError as exc:
            raise ReferenceHealthError("reference_health_write_failed") from exc
        try:
            fd, name = tempfile.mkstemp(prefix=".registry-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.state_path)
                os.chmod(self.state_path, 0o600)
            finally:
                Path(name).unlink(missing_ok=True)
        except OSError as exc:
            raise ReferenceHealthError("reference_health_write_failed") from exc

    def _record(self, route: ReferenceRoute, *, outcome: str, plan_id: str, scene_id: str) -> None:
        if outcome not in {"success"} | FAILURE_CATEGORIES:
            raise ReferenceHealthError("reference_health_outcome_invalid")
        if not re.fullmatch(r"[a-f0-9]{24}", plan_id) or not _SCENE_RE.fullmatch(scene_id):
            raise ReferenceHealthError("reference_health_binding_invalid")
        payload = self._load()
        records = payload["records"]
        previous = records.get(route.identity, {})
        successes = int(previous.get("observed_successful_task_count", 0))
        terminals = int(previous.get("observed_terminal_task_count", 0))
        consecutive = int(previous.get("consecutive_terminal_count", 0))
        if outcome == "success":
            successes += 1
            consecutive = 0
            state = (
                "degraded"
                if terminals
                else "healthy"
                if successes >= 2
                else "revalidation_required"
            )
            last_failure = previous.get("last_safe_failure_category")
        else:
            terminals += 1
            consecutive += 1
            state = (
                "degraded"
                if successes
                else "quarantined"
                if consecutive >= 2
                else "revalidation_required"
            )
            last_failure = outcome
        timestamp = datetime.now(timezone.utc).isoformat()
        records[route.identity] = {
            **asdict(route),
            "observed_successful_task_count": successes,
            "observed_terminal_task_count": terminals,
            "consecutive_terminal_count": consecutive,
            "last_safe_failure_category": last_failure,
            "health_state": state,
            "updated_at": timestamp,
        }
        event = {
            "schema": REGISTRY_SCHEMA,
            "route_identity": route.identity,
            "plan_id": plan_id,
            "scene_id": scene_id,
            "outcome": outcome,
            "successful_count": successes,
            "terminal_count": terminals,
            "consecutive_terminal_count": consecutive,
            "health_state": state,
        }
        self._write(payload, event)

    def record_success(self, route: ReferenceRoute, *, plan_id: str, scene_id: str) -> None:
        self._record(route, outcome="success", plan_id=plan_id, scene_id=scene_id)

    def record_terminal(
        self, route: ReferenceRoute, *, plan_id: str, scene_id: str, category: str
    ) -> None:
        self._record(route, outcome=category, plan_id=plan_id, scene_id=scene_id)

    def health_state(self, route: ReferenceRoute) -> str:
        record = self._load()["records"].get(route.identity)
        return str(record.get("health_state")) if isinstance(record, dict) else "unknown"

    def role_is_quarantined(self, route: ReferenceRoute) -> bool:
        record = self._load()["records"].get(route.identity)
        return isinstance(record, dict) and record.get("health_state") == "quarantined"

    def import_route_evidence(
        self,
        route: ReferenceRoute,
        *,
        successful_count: int,
        terminal_count: int,
        consecutive_terminal_count: int,
        last_failure_category: str | None,
        health_state: str,
        evidence_id: str,
    ) -> None:
        """Import one deterministic historical aggregate without transport."""
        if (
            type(successful_count) is not int
            or type(terminal_count) is not int
            or type(consecutive_terminal_count) is not int
            or min(successful_count, terminal_count, consecutive_terminal_count) < 0
            or health_state not in HEALTH_STATES
            or last_failure_category not in FAILURE_CATEGORIES | {None}
            or type(evidence_id) is not str
            or not _DIGEST_RE.fullmatch(evidence_id)
        ):
            raise ReferenceHealthError("reference_health_import_invalid")
        payload = self._load()
        timestamp = datetime.now(timezone.utc).isoformat()
        imported = {
            **asdict(route),
            "observed_successful_task_count": successful_count,
            "observed_terminal_task_count": terminal_count,
            "consecutive_terminal_count": consecutive_terminal_count,
            "last_safe_failure_category": last_failure_category,
            "health_state": health_state,
            "updated_at": timestamp,
        }
        existing = payload["records"].get(route.identity)
        if isinstance(existing, dict):
            comparable = dict(existing)
            comparable.pop("updated_at", None)
            expected = dict(imported)
            expected.pop("updated_at", None)
            if comparable != expected:
                raise ReferenceHealthError("reference_health_import_conflict")
            imported["updated_at"] = str(existing.get("updated_at", timestamp))
        payload["records"][route.identity] = imported
        self._write(payload, {
            "schema": REGISTRY_SCHEMA,
            "event": "historical_evidence_imported",
            "evidence_id": evidence_id,
            "route_identity": route.identity,
            "successful_count": successful_count,
            "terminal_count": terminal_count,
            "consecutive_terminal_count": consecutive_terminal_count,
            "last_safe_failure_category": last_failure_category,
            "health_state": health_state,
        })

    def bind_migration(
        self,
        *,
        plan_id: str,
        manifest_digest: str,
        immutable_plan_fingerprint: str,
        completed_scene_ids: tuple[str, ...],
        retry_scene_ids: tuple[str, ...],
        task_audit: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if (
            not re.fullmatch(r"[a-f0-9]{24}", plan_id)
            or not _DIGEST_RE.fullmatch(manifest_digest)
            or not _DIGEST_RE.fullmatch(immutable_plan_fingerprint)
            or not completed_scene_ids
            or not retry_scene_ids
            or set(task_audit) != {"01_hook", "02_problem", "05_conclusion"}
        ):
            raise ReferenceHealthError("reference_health_migration_invalid")
        normalized_audit: dict[str, dict[str, Any]] = {}
        for scene_id, item in task_audit.items():
            if type(item) is not dict:
                raise ReferenceHealthError("reference_health_migration_invalid")
            status = item.get("status")
            code = item.get("failure_code")
            task_digest = item.get("task_identity_digest")
            if (
                status not in {"SUCCEEDED", "FAILED"}
                or type(task_digest) is not str
                or not _DIGEST_RE.fullmatch(task_digest)
                or (scene_id == "01_hook" and (status != "SUCCEEDED" or code is not None))
                or (
                    scene_id in {"02_problem", "05_conclusion"}
                    and (
                        status != "FAILED"
                        or type(code) is not str
                        or classify_provider_failure_code(code).safe_provider_failure_code
                        != code
                    )
                )
            ):
                raise ReferenceHealthError("reference_health_migration_invalid")
            decision = classify_provider_failure_code(code)
            normalized_audit[scene_id] = {
                "status": status,
                "safe_provider_failure_code": decision.safe_provider_failure_code,
                "failure_category": decision.normalized_category,
                "task_identity_digest": task_digest,
                "automatic_retry": False,
                "same_input_retry": False,
                "corrected_input_required": decision.corrected_input_required,
                "delayed_retry_eligible": decision.delayed_retry_eligible,
            }
        payload = self._load()
        binding = {
            "policy_version": HEALTH_POLICY_VERSION,
            "manifest_digest": manifest_digest,
            "immutable_plan_fingerprint": immutable_plan_fingerprint,
            "completed_scene_ids": list(completed_scene_ids),
            "retry_scene_ids": list(retry_scene_ids),
            "task_audit": normalized_audit,
        }
        existing = payload["migrations"].get(plan_id)
        if existing is not None and existing != binding:
            raise ReferenceHealthError("reference_health_migration_conflict")
        payload["migrations"][plan_id] = binding
        self._write(payload, {"schema": REGISTRY_SCHEMA, "migration": plan_id, **binding})

    def migration(self, plan_id: str) -> dict[str, Any] | None:
        value = self._load()["migrations"].get(plan_id)
        return json.loads(json.dumps(value, sort_keys=True)) if isinstance(value, dict) else None
