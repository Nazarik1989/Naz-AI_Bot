from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import json
import os
import stat
import traceback
from pathlib import Path

import pytest

import narrative_normalizer_trust as trust


KEY = bytes(range(32))
OTHER_KEYS = (
    b"a" * 32,
    b"b" * 48,
    bytes(reversed(range(32))),
)
DOMAINS = (
    trust.TRUST_DOMAIN_EVIDENCE,
    trust.TRUST_DOMAIN_DRAFT_REVIEW,
    trust.TRUST_DOMAIN_CLAIM,
)
PAYLOAD = {
    "source_identity": "a" * 64,
    "ordered_ids": ["evidence-1", "evidence-2"],
    "nested": {"accepted": True, "count": 2, "note": "точный факт"},
}


def encoded_key(key: bytes = KEY) -> str:
    return base64.b64encode(key).decode("ascii")


def secure_file(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    if os.name != "nt":
        path.chmod(0o600)
    return path


@pytest.mark.parametrize("domain", DOMAINS, ids=("evidence", "draft-review", "claim"))
def test_signing_matches_independent_hmac_sha256_vector(domain):
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(domain, PAYLOAD)
    encoded = json.dumps(
        PAYLOAD,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    message = (
        b"naz-ai-bot\0narrative-normalizer\0trust-v1\0"
        + domain.encode("ascii")
        + b"\0"
        + encoded
    )
    assert receipt.payload_digest == hashlib.sha256(encoded).hexdigest()
    assert receipt.seal == hmac.new(KEY, message, hashlib.sha256).hexdigest()
    assert service.verify(domain, PAYLOAD, receipt) is True


def test_exact_known_vector_is_stable():
    receipt = trust.NarrativeTrustService(KEY).sign(
        trust.TRUST_DOMAIN_CLAIM,
        {"attempt": 7, "state": "completed"},
    )
    assert receipt.key_id == "3088aa72eda4cff0dc5e1b56"
    assert receipt.payload_digest == "81c5a89652059ab166ccfa6c4a42899391fc5a747e6e45e09fe0762490a85c12"
    assert receipt.seal == "57f1236ee2d24a552436503d0f5757b8c7ba9abb79aea6cfd64af139cbdfc58f"


def test_same_key_domain_and_payload_are_byte_deterministic():
    first = trust.receipt_to_payload(
        trust.NarrativeTrustService(KEY).sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    )
    second = trust.receipt_to_payload(
        trust.NarrativeTrustService(KEY).sign(trust.TRUST_DOMAIN_EVIDENCE, copy.deepcopy(PAYLOAD))
    )
    assert trust.canonical_payload(first) == trust.canonical_payload(second)


@pytest.mark.parametrize(
    "value",
    (
        b"",
        b"x",
        b"x" * 31,
        bytearray(b"x" * 32),
        memoryview(b"x" * 32),
        "x" * 32,
        None,
        32,
    ),
    ids=("empty", "one-byte", "31-bytes", "bytearray", "memoryview", "str", "none", "int"),
)
def test_service_rejects_non_exact_or_short_key(value):
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_INVALID) as error:
        trust.NarrativeTrustService(value)
    assert error.value.reason_code == trust.TRUST_KEY_INVALID
    assert error.value.__cause__ is None


def test_service_is_immutable_and_repr_is_redacted():
    raw = b"sensitive-trust-key-material-0001"
    service = trust.NarrativeTrustService(raw)
    rendered = f"{service!r} {service}"
    assert "<redacted>" in rendered
    assert raw.decode("ascii") not in rendered
    assert encoded_key(raw) not in rendered
    with pytest.raises(AttributeError):
        service.key_id = "0" * 24
    with pytest.raises(AttributeError):
        service.new_attribute = raw


def test_receipt_is_frozen_and_round_trips_exactly():
    receipt = trust.NarrativeTrustService(KEY).sign(trust.TRUST_DOMAIN_DRAFT_REVIEW, PAYLOAD)
    restored = trust.receipt_from_payload(trust.receipt_to_payload(receipt))
    assert restored == receipt
    with pytest.raises(dataclasses.FrozenInstanceError):
        restored.seal = "0" * 64


@pytest.mark.parametrize(
    ("left", "right"),
    (
        ({"b": 2, "a": 1}, {"a": 1, "b": 2}),
        ({"items": (1, 2, 3)}, {"items": [1, 2, 3]}),
        ({"text": "Привет"}, {"text": "Привет"}),
        ({"value": 1.25}, {"value": 1.25}),
    ),
    ids=("mapping-order", "tuple-list", "utf8", "finite-float"),
)
def test_canonical_payload_has_one_representation(left, right):
    assert trust.canonical_payload(left) == trust.canonical_payload(right)


@pytest.mark.parametrize(
    "value",
    (
        {"bad": {1, 2}},
        {1: "non-string-key"},
        {"bad": b"bytes"},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": complex(1, 2)},
        object(),
    ),
    ids=("set", "non-string-key", "bytes", "nan", "infinity", "complex", "object"),
)
def test_canonical_payload_rejects_unsupported_or_ambiguous_values(value):
    with pytest.raises(trust.TrustError, match=trust.TRUST_PAYLOAD_INVALID) as error:
        trust.canonical_payload(value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(source_identity="b" * 64),
        lambda value: value["ordered_ids"].append("evidence-3"),
        lambda value: value["ordered_ids"].reverse(),
        lambda value: value["nested"].update(accepted=False),
        lambda value: value["nested"].update(count=3),
        lambda value: value["nested"].update(note="другой факт"),
        lambda value: value.update(extra="field"),
        lambda value: value.pop("nested"),
    ),
    ids=(
        "source-identity",
        "extra-id",
        "id-order",
        "decision",
        "count",
        "text",
        "extra-field",
        "missing-field",
    ),
)
def test_payload_tampering_never_verifies(mutation):
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    changed = copy.deepcopy(PAYLOAD)
    mutation(changed)
    assert service.verify(trust.TRUST_DOMAIN_EVIDENCE, changed, receipt) is False
    with pytest.raises(trust.TrustError, match=trust.TRUST_RECEIPT_INVALID):
        service.require_valid(trust.TRUST_DOMAIN_EVIDENCE, changed, receipt)


@pytest.mark.parametrize(
    ("signed_domain", "verified_domain"),
    tuple((left, right) for left in DOMAINS for right in DOMAINS if left != right),
    ids=(
        "evidence-as-review",
        "evidence-as-claim",
        "review-as-evidence",
        "review-as-claim",
        "claim-as-evidence",
        "claim-as-review",
    ),
)
def test_domain_substitution_never_verifies(signed_domain, verified_domain):
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(signed_domain, PAYLOAD)
    assert service.verify(verified_domain, PAYLOAD, receipt) is False


def test_all_domain_seals_are_distinct():
    service = trust.NarrativeTrustService(KEY)
    seals = {service.sign(domain, PAYLOAD).seal for domain in DOMAINS}
    assert len(seals) == len(DOMAINS)


@pytest.mark.parametrize("wrong_key", OTHER_KEYS, ids=("key-a", "key-b-long", "reversed"))
def test_wrong_key_never_verifies(wrong_key):
    receipt = trust.NarrativeTrustService(KEY).sign(trust.TRUST_DOMAIN_CLAIM, PAYLOAD)
    assert trust.NarrativeTrustService(wrong_key).verify(
        trust.TRUST_DOMAIN_CLAIM,
        PAYLOAD,
        receipt,
    ) is False


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("key_id", "0" * 24),
        ("domain", trust.TRUST_DOMAIN_CLAIM),
        ("payload_digest", "0" * 64),
        ("seal", "0" * 64),
    ),
    ids=("key-id", "domain", "payload-digest", "seal"),
)
def test_well_formed_receipt_tampering_never_verifies(field, replacement):
    service = trust.NarrativeTrustService(KEY)
    payload = trust.receipt_to_payload(service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD))
    payload[field] = replacement
    changed = trust.receipt_from_payload(payload)
    assert service.verify(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD, changed) is False


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.pop("seal"),
        lambda value: value.update(extra="field"),
        lambda value: value.update(schema_version="unknown"),
        lambda value: value.update(algorithm="sha256"),
        lambda value: value.update(key_id="short"),
        lambda value: value.update(domain="unknown"),
        lambda value: value.update(payload_digest="z" * 64),
        lambda value: value.update(seal="z" * 64),
    ),
    ids=("missing", "extra", "schema", "algorithm", "key-id", "domain", "digest", "seal"),
)
def test_malformed_receipt_payload_fails_closed_without_context(mutation):
    payload = trust.receipt_to_payload(
        trust.NarrativeTrustService(KEY).sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    )
    mutation(payload)
    with pytest.raises(trust.TrustError, match=trust.TRUST_RECEIPT_INVALID) as error:
        trust.receipt_from_payload(payload)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_verify_uses_constant_time_comparison_for_every_receipt_binding(monkeypatch):
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    original = trust.hmac.compare_digest
    calls = []

    def observed(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(trust.hmac, "compare_digest", observed)
    assert service.verify(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD, receipt) is True
    assert len(calls) == 4


def test_hmac_constructor_exception_is_privacy_normalized(monkeypatch):
    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("credential=constructor-secret C:\\private\\key")

    monkeypatch.setattr(trust.hmac, "new", fail)
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_INVALID) as captured:
        trust.NarrativeTrustService(KEY)
    public = " ".join((str(captured.value), repr(captured.value), "".join(traceback.format_exception(captured.value))))
    assert "constructor-secret" not in public
    assert "C:\\private\\key" not in public
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_sign_and_verify_fail_closed_on_hmac_runtime_exception(monkeypatch):
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("token=signing-secret /private/key")

    monkeypatch.setattr(trust.hmac, "new", fail)
    with pytest.raises(trust.TrustError, match=trust.TRUST_RECEIPT_INVALID) as captured:
        service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    public = " ".join((str(captured.value), repr(captured.value), "".join(traceback.format_exception(captured.value))))
    assert "signing-secret" not in public
    assert "/private/key" not in public
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert service.verify(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD, receipt) is False


@pytest.mark.parametrize(
    "cancellation",
    (KeyboardInterrupt("cancel-hmac"), SystemExit("exit-hmac"), GeneratorExit("close-hmac")),
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_hmac_base_exceptions_are_not_normalized(monkeypatch, cancellation):
    service = trust.NarrativeTrustService(KEY)

    def cancel(*args, **kwargs):
        del args, kwargs
        raise cancellation

    monkeypatch.setattr(trust.hmac, "new", cancel)
    with pytest.raises(type(cancellation)) as captured:
        service.sign(trust.TRUST_DOMAIN_EVIDENCE, PAYLOAD)
    assert captured.value is cancellation


def test_loads_strict_base64_key_from_injected_environment():
    service = trust.load_trust_service({trust.TRUST_KEY_ENV: encoded_key()}, None)
    assert service.verify(
        trust.TRUST_DOMAIN_CLAIM,
        PAYLOAD,
        service.sign(trust.TRUST_DOMAIN_CLAIM, PAYLOAD),
    )


@pytest.mark.parametrize(
    ("env", "expected"),
    (
        ({}, trust.TRUST_KEY_MISSING),
        ({trust.TRUST_KEY_ENV: ""}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: "not-base64"}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: " x"}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: "x\n"}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: base64.b64encode(b"short").decode("ascii")}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: "ключ"}, trust.TRUST_KEY_INVALID),
        ({trust.TRUST_KEY_ENV: "A" * (trust.MAX_TRUST_KEY_FILE_BYTES + 4)}, trust.TRUST_KEY_INVALID),
    ),
    ids=("missing", "empty", "not-base64", "leading-space", "newline", "short", "non-ascii", "oversize"),
)
def test_environment_key_failures_have_only_stable_reason(env, expected):
    with pytest.raises(trust.TrustError, match=expected) as error:
        trust.load_trust_service(env, None)
    assert error.value.args == (expected,)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_environment_and_file_are_rejected_as_ambiguous_without_reading_path(tmp_path):
    sensitive = tmp_path / "secret-token-file"
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_AMBIGUOUS) as error:
        trust.load_trust_service({trust.TRUST_KEY_ENV: encoded_key()}, sensitive)
    public = " ".join((str(error.value), repr(error.value), str(error.value.args)))
    assert str(sensitive) not in public
    assert encoded_key() not in public


@pytest.mark.parametrize("ending", (b"", b"\n", b"\r\n"), ids=("none", "lf", "crlf"))
def test_loads_key_file_with_one_optional_terminal_newline(tmp_path, ending):
    path = secure_file(tmp_path / "trust.key", encoded_key().encode("ascii") + ending)
    service = trust.load_trust_service({}, path)
    assert service.key_id == trust.NarrativeTrustService(KEY).key_id


@pytest.mark.parametrize(
    "case",
    (
        "missing",
        "directory",
        "empty",
        "oversize",
        "multiline",
        "bad-base64",
        "short-key",
    ),
)
def test_key_file_shape_and_content_fail_closed(tmp_path, case):
    path = tmp_path / "private-credential.key"
    if case == "directory":
        path.mkdir()
    elif case == "empty":
        secure_file(path, b"")
    elif case == "oversize":
        secure_file(path, b"x" * (trust.MAX_TRUST_KEY_FILE_BYTES + 1))
    elif case == "multiline":
        secure_file(path, encoded_key().encode("ascii") + b"\nextra")
    elif case == "bad-base64":
        secure_file(path, b"not-base64")
    elif case == "short-key":
        secure_file(path, base64.b64encode(b"short"))
    with pytest.raises(trust.TrustError) as error:
        trust.load_trust_service({}, path)
    assert error.value.reason_code in {trust.TRUST_KEY_FILE_INVALID, trust.TRUST_KEY_INVALID}
    public = " ".join((str(error.value), repr(error.value), str(error.value.args)))
    assert str(path) not in public
    assert "private-credential" not in public
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_symlink_key_file_is_rejected(tmp_path):
    target = secure_file(tmp_path / "target.key", encoded_key().encode("ascii"))
    link = tmp_path / "sensitive-link.key"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_FILE_INVALID):
        trust.load_trust_service({}, link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_group_or_other_readable_key_file_is_rejected(tmp_path):
    path = secure_file(tmp_path / "trust.key", encoded_key().encode("ascii"))
    path.chmod(0o644)
    assert stat.S_IMODE(path.stat().st_mode) & 0o077
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_FILE_INVALID):
        trust.load_trust_service({}, path)


def test_sensitive_environment_value_never_appears_on_public_exception_surfaces():
    sensitive = "credential=very-private-normalizer-key"
    with pytest.raises(trust.TrustError) as captured:
        trust.load_trust_service({trust.TRUST_KEY_ENV: sensitive}, None)
    error = captured.value
    public = " ".join(
        (
            str(error),
            repr(error),
            str(error.args),
            "".join(traceback.format_exception(error)),
        )
    )
    assert sensitive not in public
    assert "very-private-normalizer-key" not in public
    assert error.__cause__ is None
    assert error.__context__ is None


class RaisingEnvironment(dict):
    def get(self, key, default=None):
        del key, default
        raise RuntimeError("C:\\secret\\trust-key credential=private")


def test_environment_lookup_exception_is_privacy_normalized():
    with pytest.raises(trust.TrustError, match=trust.TRUST_KEY_INVALID) as captured:
        trust.load_trust_service(RaisingEnvironment(), None)
    error = captured.value
    public = " ".join((str(error), repr(error), "".join(traceback.format_exception(error))))
    assert "C:\\secret\\trust-key" not in public
    assert "credential=private" not in public
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "cancellation",
    (KeyboardInterrupt("cancel-key-read"), SystemExit("exit-key-read"), GeneratorExit("close-key-read")),
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_base_exceptions_from_key_file_boundary_are_not_normalized(tmp_path, monkeypatch, cancellation):
    path = secure_file(tmp_path / "trust.key", encoded_key().encode("ascii"))

    def cancel(*args, **kwargs):
        del args, kwargs
        raise cancellation

    monkeypatch.setattr(trust.os, "open", cancel)
    with pytest.raises(type(cancellation)) as captured:
        trust.load_trust_service({}, path)
    assert captured.value is cancellation


def test_receipt_api_does_not_accept_mapping_as_verified_receipt():
    service = trust.NarrativeTrustService(KEY)
    receipt = service.sign(trust.TRUST_DOMAIN_CLAIM, PAYLOAD)
    assert service.verify(
        trust.TRUST_DOMAIN_CLAIM,
        PAYLOAD,
        trust.receipt_to_payload(receipt),
    ) is False


def test_unknown_domain_cannot_be_signed_or_verified():
    service = trust.NarrativeTrustService(KEY)
    with pytest.raises(trust.TrustError, match=trust.TRUST_RECEIPT_INVALID):
        service.sign("normalizer-unknown-v1", PAYLOAD)
    receipt = service.sign(trust.TRUST_DOMAIN_CLAIM, PAYLOAD)
    assert service.verify("normalizer-unknown-v1", PAYLOAD, receipt) is False
