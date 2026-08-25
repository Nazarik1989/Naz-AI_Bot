"""Closed, versioned IPC protocol for the Narrative Review Authority."""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import BinaryIO, Mapping

from narrative_normalizer_trust import canonical_payload


IPC_SCHEMA_VERSION = "narrative-review-authority-ipc-v2"
MAX_FRAME_BYTES = 1_048_576
MAX_REQUEST_ID_BYTES = 128
ROLE_NORMALIZER = "normalizer"
ROLE_REVIEWER = "reviewer"
ROLE_CONSUMER = "consumer"
ROLES = frozenset({ROLE_NORMALIZER, ROLE_REVIEWER, ROLE_CONSUMER})

OP_HEALTH = "health"
OP_REGISTER_DRAFT = "register_draft"
OP_LATEST_STATE = "latest_state"
OP_APPEND_REVIEW = "append_review"
OP_PREPARE_APPROVAL = "prepare_approval"
OP_COMMIT_APPROVAL = "commit_approval"
OP_VERIFY_READY = "verify_ready"
OPERATIONS = frozenset({
    OP_HEALTH, OP_REGISTER_DRAFT, OP_LATEST_STATE, OP_APPEND_REVIEW,
    OP_PREPARE_APPROVAL, OP_COMMIT_APPROVAL, OP_VERIFY_READY,
})

CAPABILITIES = {
    ROLE_NORMALIZER: frozenset({OP_HEALTH, OP_REGISTER_DRAFT, OP_LATEST_STATE}),
    ROLE_REVIEWER: frozenset({
        OP_HEALTH, OP_LATEST_STATE, OP_APPEND_REVIEW,
        OP_PREPARE_APPROVAL, OP_COMMIT_APPROVAL,
    }),
    ROLE_CONSUMER: frozenset({OP_HEALTH, OP_LATEST_STATE, OP_VERIFY_READY}),
}

PROTOCOL_INVALID = "review_authority_protocol_invalid"
FRAME_INVALID = "review_authority_frame_invalid"
FRAME_TOO_LARGE = "review_authority_frame_too_large"
REQUEST_CONFLICT = "review_authority_request_conflict"
ACCESS_DENIED = "review_authority_access_denied"
TRANSPORT_CLOSED = "review_authority_transport_closed"
WRITE_HALF_CLOSE_REQUIRED = "review_authority_write_half_close_required"

_REQUEST_KEYS = frozenset({"schema_version", "request_id", "operation", "payload"})
_RESPONSE_KEYS = frozenset({"schema_version", "request_id", "ok", "result", "error"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ProtocolError(ValueError):
    """Privacy-safe protocol error."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason: str = PROTOCOL_INVALID) -> None:
    raise ProtocolError(reason)


def safe_identifier(value: object) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        _fail()
    return value


def exact_json(value: object, *, depth: int = 0) -> object:
    if depth > 24:
        _fail()
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is list:
        return [exact_json(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                _fail()
            result[key] = exact_json(item, depth=depth + 1)
        return result
    _fail()


def canonical(value: object) -> bytes:
    exact_json(value)
    try:
        return canonical_payload(value)
    except Exception:
        raise ProtocolError(PROTOCOL_INVALID) from None


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    operation: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        safe_identifier(self.request_id)
        if type(self.operation) is not str or self.operation not in OPERATIONS:
            _fail()
        if type(self.payload) is not dict:
            _fail()
        exact_json(self.payload)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": IPC_SCHEMA_VERSION,
            "request_id": self.request_id,
            "operation": self.operation,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class Response:
    request_id: str
    ok: bool
    result: dict[str, object] | None
    error: str | None

    def __post_init__(self) -> None:
        safe_identifier(self.request_id)
        if type(self.ok) is not bool:
            _fail()
        if self.ok:
            if type(self.result) is not dict or self.error is not None:
                _fail()
            exact_json(self.result)
        elif self.result is not None or type(self.error) is not str or _SAFE_ID.fullmatch(self.error) is None:
            _fail()

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": IPC_SCHEMA_VERSION,
            "request_id": self.request_id,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
        }


def request_from_payload(value: object) -> Request:
    try:
        if type(value) is not dict or frozenset(value) != _REQUEST_KEYS:
            _fail()
        if type(value["schema_version"]) is not str or value["schema_version"] != IPC_SCHEMA_VERSION:
            _fail()
        return Request(value["request_id"], value["operation"], value["payload"])
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError(PROTOCOL_INVALID) from None


def response_from_payload(value: object) -> Response:
    try:
        if type(value) is not dict or frozenset(value) != _RESPONSE_KEYS:
            _fail()
        if type(value["schema_version"]) is not str or value["schema_version"] != IPC_SCHEMA_VERSION:
            _fail()
        return Response(value["request_id"], value["ok"], value["result"], value["error"])
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError(PROTOCOL_INVALID) from None


def encode_frame(value: object) -> bytes:
    body = canonical(value)
    if len(body) > MAX_FRAME_BYTES:
        _fail(FRAME_TOO_LARGE)
    return struct.pack(">I", len(body)) + body


def decode_frame_bytes(frame: object) -> object:
    if type(frame) is not bytes or len(frame) < 4:
        _fail(FRAME_INVALID)
    length = struct.unpack(">I", frame[:4])[0]
    if length > MAX_FRAME_BYTES:
        _fail(FRAME_TOO_LARGE)
    if length != len(frame) - 4:
        _fail(FRAME_INVALID)
    body = frame[4:]
    try:
        value = json.loads(body.decode("utf-8"))
        if body != canonical(value):
            _fail(FRAME_INVALID)
        return value
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError(FRAME_INVALID) from None


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            _fail(TRANSPORT_CLOSED)
        if type(chunk) is not bytes:
            _fail(FRAME_INVALID)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> object:
    header = _read_exact(stream, 4)
    length = struct.unpack(">I", header)[0]
    if length > MAX_FRAME_BYTES:
        _fail(FRAME_TOO_LARGE)
    return decode_frame_bytes(header + _read_exact(stream, length))


def read_single_frame(stream: BinaryIO) -> object:
    """Read one complete connection and accept exactly one frame.

    EOF is part of the request contract: callers must write-half-close after
    their sole frame.  Reading to EOF before parsing prevents a valid first
    frame from being dispatched while delayed trailing bytes remain in flight.
    """

    chunks: list[bytes] = []
    total = 0
    limit = MAX_FRAME_BYTES + 4
    try:
        while True:
            chunk = stream.read(min(65_536, limit + 1 - total))
            if not chunk:
                break
            if type(chunk) is not bytes:
                _fail(FRAME_INVALID)
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                _fail(FRAME_TOO_LARGE)
    except ProtocolError:
        raise
    except (TimeoutError, OSError):
        raise ProtocolError(WRITE_HALF_CLOSE_REQUIRED) from None
    return decode_frame_bytes(b"".join(chunks))


def write_frame(stream: BinaryIO, value: object) -> None:
    stream.write(encode_frame(value))
    stream.flush()


def require_capability(role: object, operation: object) -> None:
    if type(role) is not str or role not in ROLES or type(operation) is not str:
        _fail(ACCESS_DENIED)
    if operation not in CAPABILITIES[role]:
        _fail(ACCESS_DENIED)


def make_error(request_id: str, reason: str) -> Response:
    safe_identifier(request_id)
    safe_identifier(reason)
    return Response(request_id, False, None, reason)


__all__ = (
    "ACCESS_DENIED", "CAPABILITIES", "FRAME_INVALID", "FRAME_TOO_LARGE",
    "IPC_SCHEMA_VERSION", "MAX_FRAME_BYTES", "OPERATIONS", "OP_APPEND_REVIEW",
    "OP_COMMIT_APPROVAL", "OP_HEALTH", "OP_LATEST_STATE", "OP_PREPARE_APPROVAL",
    "OP_REGISTER_DRAFT", "OP_VERIFY_READY", "PROTOCOL_INVALID", "ProtocolError",
    "REQUEST_CONFLICT", "ROLE_CONSUMER", "ROLE_NORMALIZER", "ROLE_REVIEWER",
    "ROLES", "Request", "Response", "canonical", "decode_frame_bytes", "encode_frame",
    "exact_json", "make_error", "read_frame", "request_from_payload", "require_capability",
    "response_from_payload", "safe_identifier", "write_frame", "read_single_frame",
    "WRITE_HALF_CLOSE_REQUIRED",
)
