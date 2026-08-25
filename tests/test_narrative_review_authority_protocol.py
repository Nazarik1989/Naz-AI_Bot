from __future__ import annotations

import io
import json
import struct
from collections import UserDict

import pytest

import narrative_review_authority_protocol as p


class Text(str):
    pass


class Integer(int):
    pass


class Dictionary(dict):
    pass


class Listing(list):
    pass


class FragmentedReader:
    def __init__(self, value: bytes, fragment_size: int):
        self.value = value
        self.fragment_size = fragment_size
        self.offset = 0

    def read(self, size: int) -> bytes:
        if self.offset == len(self.value):
            return b""
        count = min(size, self.fragment_size, len(self.value) - self.offset)
        result = self.value[self.offset:self.offset + count]
        self.offset += count
        return result


class TimeoutReader:
    def read(self, size: int) -> bytes:
        del size
        raise TimeoutError


PLAIN_REQUEST = {
    "schema_version": p.IPC_SCHEMA_VERSION,
    "request_id": "request-001",
    "operation": p.OP_HEALTH,
    "payload": {},
}


@pytest.mark.parametrize("operation", sorted(p.OPERATIONS))
def test_request_roundtrip_for_each_closed_operation(operation: str) -> None:
    request = p.Request("request-001", operation, {})
    assert p.request_from_payload(request.to_payload()) == request


def test_ambiguous_v1_ipc_envelope_is_rejected_by_v2_contract() -> None:
    raw = dict(PLAIN_REQUEST)
    raw["schema_version"] = "narrative-review-authority-ipc-v1"
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.request_from_payload(raw)


@pytest.mark.parametrize(
    ("role", "operation"),
    [(role, operation) for role in sorted(p.ROLES) for operation in sorted(p.OPERATIONS)],
)
def test_capability_matrix_is_exact(role: str, operation: str) -> None:
    if operation in p.CAPABILITIES[role]:
        p.require_capability(role, operation)
    else:
        with pytest.raises(p.ProtocolError, match=p.ACCESS_DENIED):
            p.require_capability(role, operation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", Text(p.IPC_SCHEMA_VERSION)),
        ("schema_version", None),
        ("schema_version", "v2"),
        ("request_id", Text("request-001")),
        ("request_id", None),
        ("request_id", 1),
        ("operation", Text(p.OP_HEALTH)),
        ("operation", None),
        ("operation", "delete"),
        ("payload", Dictionary()),
        ("payload", UserDict()),
        ("payload", []),
    ],
)
def test_request_envelope_rejects_nonexact_or_unknown_values(field: str, value: object) -> None:
    raw = dict(PLAIN_REQUEST)
    raw[field] = value
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.request_from_payload(raw)


@pytest.mark.parametrize("missing", sorted(PLAIN_REQUEST))
def test_request_rejects_each_missing_envelope_key(missing: str) -> None:
    raw = dict(PLAIN_REQUEST)
    del raw[missing]
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.request_from_payload(raw)


@pytest.mark.parametrize("extra", ["role", "path", "key", "signature", "timeout", "debug", "admin", "uid"])
def test_request_rejects_each_extra_envelope_key(extra: str) -> None:
    raw = dict(PLAIN_REQUEST)
    raw[extra] = "x"
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.request_from_payload(raw)


@pytest.mark.parametrize(
    "request_id",
    [
        "", " ", " a", "a ", "a/b", "a\\b", ".leading", "-leading", ":leading",
        "a\nb", "a\rb", "a\tb", "a b", "ü", "a?b", "a#b", "a%b", "a&b",
        "a=b", "a+b", "a,b", "a;b", "a@b", "a!b", "a" * 129, Text("valid-id"),
        0, True, None, b"id", [], {},
    ],
)
def test_request_id_closed_grammar(request_id: object) -> None:
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.Request(request_id, p.OP_HEALTH, {})


@pytest.mark.parametrize(
    "payload",
    [
        {"x": Text("value")}, {"x": Integer(1)}, {"x": Dictionary()},
        {"x": Listing()}, {1: "value"}, {"x": object()}, {"x": {"y": Text("z")}},
        {"x": [Text("z")]}, {"x": UserDict()}, Dictionary(), UserDict(), [], (),
    ],
)
def test_payload_rejects_nonexact_json_types_before_serialization(payload: object) -> None:
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.Request("request-001", p.OP_HEALTH, payload)


@pytest.mark.parametrize(
    "payload",
    [{}, {"a": None}, {"a": True}, {"a": 1}, {"a": "x"}, {"a": []}, {"a": {}}, {"a": [1, "x", False]}],
)
def test_payload_accepts_plain_closed_json_types(payload: dict[str, object]) -> None:
    assert p.Request("request-001", p.OP_HEALTH, payload).payload == payload


@pytest.mark.parametrize("operation", ["delete", "replace", "truncate", "list", "sign", "get_key", "raw_path", "tcp"])
def test_arbitrary_or_dangerous_operation_is_absent(operation: str) -> None:
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.Request("request-001", operation, {})


@pytest.mark.parametrize(
    "mutation",
    [
        b"", b"\x00", b"{}", struct.pack(">I", 2) + b"{}x",
        struct.pack(">I", 3) + b"{}", struct.pack(">I", 1) + b"{",
        struct.pack(">I", 2) + b"\xff\xff",
        struct.pack(">I", 7) + b'{"a":1 }', struct.pack(">I", 13) + b'{"b":1,"a":2}',
        struct.pack(">I", 8) + b'{"a":1}\n', struct.pack(">I", p.MAX_FRAME_BYTES + 1),
    ],
)
def test_malformed_noncanonical_or_oversized_frames_fail_closed(mutation: bytes) -> None:
    expected = p.FRAME_TOO_LARGE if len(mutation) == 4 and struct.unpack(">I", mutation)[0] > p.MAX_FRAME_BYTES else p.FRAME_INVALID
    with pytest.raises(p.ProtocolError, match=expected):
        p.decode_frame_bytes(mutation)


@pytest.mark.parametrize("cut", range(0, 18))
def test_truncated_stream_fails_closed_at_every_cut(cut: int) -> None:
    frame = p.encode_frame(PLAIN_REQUEST)
    if cut >= len(frame):
        pytest.skip("cut beyond frame")
    with pytest.raises(p.ProtocolError, match=p.TRANSPORT_CLOSED):
        p.read_frame(io.BytesIO(frame[:cut]))


def test_frame_roundtrip_is_canonical_and_length_prefixed() -> None:
    frame = p.encode_frame(PLAIN_REQUEST)
    assert struct.unpack(">I", frame[:4])[0] == len(frame) - 4
    assert p.decode_frame_bytes(frame) == PLAIN_REQUEST
    assert frame[4:] == p.canonical(PLAIN_REQUEST)


def test_single_frame_reader_accepts_exact_frame_followed_by_eof() -> None:
    assert p.read_single_frame(io.BytesIO(p.encode_frame(PLAIN_REQUEST))) == PLAIN_REQUEST


@pytest.mark.parametrize("fragment_size", [1, 2, 3, 7, 31])
def test_single_frame_reader_accepts_fragmented_frame_after_complete_eof(fragment_size: int) -> None:
    stream = FragmentedReader(p.encode_frame(PLAIN_REQUEST), fragment_size)
    assert p.read_single_frame(stream) == PLAIN_REQUEST


@pytest.mark.parametrize(
    "trailing",
    [b"x", b"trailing prose", p.encode_frame(PLAIN_REQUEST)],
)
def test_single_frame_reader_rejects_every_trailing_byte_or_second_frame(trailing: bytes) -> None:
    with pytest.raises(p.ProtocolError, match=p.FRAME_INVALID):
        p.read_single_frame(io.BytesIO(p.encode_frame(PLAIN_REQUEST) + trailing))


def test_single_frame_reader_requires_write_half_close_before_timeout() -> None:
    with pytest.raises(p.ProtocolError, match=p.WRITE_HALF_CLOSE_REQUIRED):
        p.read_single_frame(TimeoutReader())


@pytest.mark.parametrize("raw", [b"", b"\x00", b"\x00\x00\x00", p.encode_frame(PLAIN_REQUEST)[:-1]])
def test_single_frame_reader_rejects_truncated_connection(raw: bytes) -> None:
    with pytest.raises(p.ProtocolError, match=p.FRAME_INVALID):
        p.read_single_frame(io.BytesIO(raw))


def test_single_frame_reader_rejects_multiple_json_values_inside_one_payload() -> None:
    body = b"{}{}"
    with pytest.raises(p.ProtocolError, match=p.FRAME_INVALID):
        p.read_single_frame(io.BytesIO(struct.pack(">I", len(body)) + body))


@pytest.mark.parametrize("scalar", [None, [], 0, True, "request"])
def test_canonical_json_frame_with_non_envelope_value_is_rejected_at_request_boundary(scalar: object) -> None:
    decoded = p.decode_frame_bytes(p.encode_frame(scalar))
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.request_from_payload(decoded)


@pytest.mark.parametrize("size", [p.MAX_FRAME_BYTES + 1, p.MAX_FRAME_BYTES + 2, p.MAX_FRAME_BYTES + 1024])
def test_encode_rejects_oversized_frame(size: int) -> None:
    with pytest.raises(p.ProtocolError, match=p.FRAME_TOO_LARGE):
        p.encode_frame({"x": "a" * size})


PLAIN_RESPONSE = {
    "schema_version": p.IPC_SCHEMA_VERSION,
    "request_id": "request-001",
    "ok": True,
    "result": {"status": "ok"},
    "error": None,
}


@pytest.mark.parametrize("missing", sorted(PLAIN_RESPONSE))
def test_response_rejects_each_missing_key(missing: str) -> None:
    raw = dict(PLAIN_RESPONSE)
    del raw[missing]
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.response_from_payload(raw)


@pytest.mark.parametrize("extra", ["trace", "secret", "stack", "role", "key"])
def test_response_rejects_each_extra_key(extra: str) -> None:
    raw = dict(PLAIN_RESPONSE)
    raw[extra] = "x"
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.response_from_payload(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        {"ok": 1}, {"ok": Text("true")}, {"result": None}, {"result": Dictionary()},
        {"error": "unexpected"}, {"request_id": Text("request-001")},
        {"schema_version": Text(p.IPC_SCHEMA_VERSION)}, {"schema_version": "v2"},
    ],
)
def test_success_response_exact_contract(mutation: dict[str, object]) -> None:
    raw = dict(PLAIN_RESPONSE)
    raw.update(mutation)
    with pytest.raises(p.ProtocolError, match=p.PROTOCOL_INVALID):
        p.response_from_payload(raw)


@pytest.mark.parametrize("reason", [p.ACCESS_DENIED, p.REQUEST_CONFLICT, p.PROTOCOL_INVALID, p.FRAME_INVALID])
def test_error_response_roundtrip(reason: str) -> None:
    response = p.make_error("request-001", reason)
    assert p.response_from_payload(response.to_payload()) == response
    assert response.result is None and response.error == reason


@pytest.mark.parametrize("role", [None, 1, Text(p.ROLE_NORMALIZER), "admin", "root", "reviewer-admin"])
def test_role_is_exact_closed_enum(role: object) -> None:
    with pytest.raises(p.ProtocolError, match=p.ACCESS_DENIED):
        p.require_capability(role, p.OP_HEALTH)


def test_write_frame_never_emits_noncanonical_json() -> None:
    stream = io.BytesIO()
    p.write_frame(stream, {"z": 1, "a": 2})
    assert stream.getvalue()[4:] == b'{"a":2,"z":1}'
