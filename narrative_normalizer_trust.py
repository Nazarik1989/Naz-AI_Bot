"""Keyed integrity primitives owned by the Narrative Normalizer.

The module is intentionally standalone: it has no dependency on the normalizer
runtime or on CP1/CP2.  It provides deterministic, domain-separated HMAC-SHA256
receipts and a strict secret loader suitable for the local CLI.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Mapping


TRUST_RECEIPT_SCHEMA_VERSION = "normalizer-trust-receipt-v1"
TRUST_ALGORITHM = "hmac-sha256"
TRUST_KEY_ENV = "NARRATIVE_NORMALIZER_TRUST_KEY"
MIN_TRUST_KEY_BYTES = 32
MAX_TRUST_KEY_FILE_BYTES = 4096

TRUST_DOMAIN_EVIDENCE = "normalizer-evidence-v1"
TRUST_DOMAIN_DRAFT_REVIEW = "normalizer-draft-review-v1"
TRUST_DOMAIN_CLAIM = "normalizer-claim-v1"
TRUST_DOMAIN_REVIEW_LEDGER = "normalizer-review-ledger-v1"
TRUST_DOMAIN_APPROVAL_ATTESTATION = "normalizer-approval-attestation-v1"
TRUST_DOMAINS = frozenset({
    TRUST_DOMAIN_EVIDENCE,
    TRUST_DOMAIN_DRAFT_REVIEW,
    TRUST_DOMAIN_CLAIM,
    TRUST_DOMAIN_REVIEW_LEDGER,
    TRUST_DOMAIN_APPROVAL_ATTESTATION,
})

TRUST_KEY_MISSING = "narrative_normalizer_trust_key_missing"
TRUST_KEY_INVALID = "narrative_normalizer_trust_key_invalid"
TRUST_KEY_AMBIGUOUS = "narrative_normalizer_trust_key_ambiguous"
TRUST_KEY_FILE_INVALID = "narrative_normalizer_trust_key_file_invalid"
TRUST_PAYLOAD_INVALID = "narrative_normalizer_trust_payload_invalid"
TRUST_RECEIPT_INVALID = "narrative_normalizer_trust_invalid"

_HEX24 = re.compile(r"[0-9a-f]{24}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE_PREFIX = b"naz-ai-bot\0narrative-normalizer\0trust-v1"
_KEY_ID_CONTEXT = b"naz-ai-bot\0narrative-normalizer\0key-id-v1"
_RECEIPT_KEYS = frozenset({
    "schema_version",
    "algorithm",
    "key_id",
    "domain",
    "payload_digest",
    "seal",
})


class TrustError(ValueError):
    """Privacy-safe trust-domain error carrying only a stable reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _raise(reason_code: str) -> None:
    raise TrustError(reason_code)


def _normalize_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _raise(TRUST_PAYLOAD_INVALID)
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_json(asdict(value))
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                _raise(TRUST_PAYLOAD_INVALID)
            normalized[key] = _normalize_json(item)
        return normalized
    if type(value) in {list, tuple}:
        return [_normalize_json(item) for item in value]
    _raise(TRUST_PAYLOAD_INVALID)


def canonical_payload(value: object) -> bytes:
    """Return the one canonical UTF-8 JSON representation accepted for HMAC."""

    reason: str | None = None
    try:
        normalized = _normalize_json(value)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except TrustError as error:
        reason = error.reason_code
    except Exception:
        reason = TRUST_PAYLOAD_INVALID
    if reason is not None:
        raise TrustError(reason) from None
    raise AssertionError("unreachable")


def _valid_domain(domain: object) -> bool:
    return type(domain) is str and domain in TRUST_DOMAINS


@dataclass(frozen=True, slots=True)
class TrustReceipt:
    schema_version: str
    algorithm: str
    key_id: str
    domain: str
    payload_digest: str
    seal: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != TRUST_RECEIPT_SCHEMA_VERSION
            or self.algorithm != TRUST_ALGORITHM
            or type(self.key_id) is not str
            or _HEX24.fullmatch(self.key_id) is None
            or not _valid_domain(self.domain)
            or type(self.payload_digest) is not str
            or _HEX64.fullmatch(self.payload_digest) is None
            or type(self.seal) is not str
            or _HEX64.fullmatch(self.seal) is None
        ):
            _raise(TRUST_RECEIPT_INVALID)


def receipt_to_payload(receipt: TrustReceipt) -> dict[str, str]:
    if type(receipt) is not TrustReceipt:
        _raise(TRUST_RECEIPT_INVALID)
    return {
        "schema_version": receipt.schema_version,
        "algorithm": receipt.algorithm,
        "key_id": receipt.key_id,
        "domain": receipt.domain,
        "payload_digest": receipt.payload_digest,
        "seal": receipt.seal,
    }


def receipt_from_payload(value: object) -> TrustReceipt:
    reason: str | None = None
    try:
        if type(value) is not dict or frozenset(value) != _RECEIPT_KEYS:
            _raise(TRUST_RECEIPT_INVALID)
        return TrustReceipt(
            schema_version=value["schema_version"],
            algorithm=value["algorithm"],
            key_id=value["key_id"],
            domain=value["domain"],
            payload_digest=value["payload_digest"],
            seal=value["seal"],
        )
    except TrustError as error:
        reason = error.reason_code
    except Exception:
        reason = TRUST_RECEIPT_INVALID
    if reason is not None:
        raise TrustError(reason) from None
    raise AssertionError("unreachable")


class NarrativeTrustService:
    """Immutable signing capability whose representation never exposes its key."""

    __slots__ = ("__key", "__key_id")

    def __init__(self, key: bytes):
        if type(key) is not bytes or len(key) < MIN_TRUST_KEY_BYTES:
            _raise(TRUST_KEY_INVALID)
        reason: str | None = None
        key_id: str | None = None
        try:
            key_id = hmac.new(key, _KEY_ID_CONTEXT, hashlib.sha256).hexdigest()[:24]
        except Exception:
            reason = TRUST_KEY_INVALID
        if reason is not None or key_id is None:
            raise TrustError(TRUST_KEY_INVALID) from None
        object.__setattr__(self, "_NarrativeTrustService__key", bytes(key))
        object.__setattr__(self, "_NarrativeTrustService__key_id", key_id)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("NarrativeTrustService is immutable")

    def __repr__(self) -> str:
        return f"NarrativeTrustService(key=<redacted>, key_id={self.key_id!r})"

    __str__ = __repr__

    @property
    def key_id(self) -> str:
        return self.__key_id

    @staticmethod
    def _message(domain: str, encoded_payload: bytes) -> bytes:
        return _MESSAGE_PREFIX + b"\0" + domain.encode("ascii") + b"\0" + encoded_payload

    def sign(self, domain: str, payload: object) -> TrustReceipt:
        if not _valid_domain(domain):
            _raise(TRUST_RECEIPT_INVALID)
        reason: str | None = None
        try:
            encoded = canonical_payload(payload)
            return TrustReceipt(
                schema_version=TRUST_RECEIPT_SCHEMA_VERSION,
                algorithm=TRUST_ALGORITHM,
                key_id=self.key_id,
                domain=domain,
                payload_digest=hashlib.sha256(encoded).hexdigest(),
                seal=hmac.new(self.__key, self._message(domain, encoded), hashlib.sha256).hexdigest(),
            )
        except TrustError as error:
            reason = error.reason_code
        except Exception:
            reason = TRUST_RECEIPT_INVALID
        if reason is not None:
            raise TrustError(reason) from None
        raise AssertionError("unreachable")

    def verify(self, domain: str, payload: object, receipt: TrustReceipt) -> bool:
        if not _valid_domain(domain) or type(receipt) is not TrustReceipt:
            return False
        try:
            encoded = canonical_payload(payload)
            expected_digest = hashlib.sha256(encoded).hexdigest()
            expected_seal = hmac.new(
                self.__key,
                self._message(domain, encoded),
                hashlib.sha256,
            ).hexdigest()
            domain_matches = hmac.compare_digest(receipt.domain, domain)
            key_matches = hmac.compare_digest(receipt.key_id, self.key_id)
            digest_matches = hmac.compare_digest(receipt.payload_digest, expected_digest)
            seal_matches = hmac.compare_digest(receipt.seal, expected_seal)
            return bool(domain_matches and key_matches and digest_matches and seal_matches)
        except Exception:
            return False

    def require_valid(self, domain: str, payload: object, receipt: TrustReceipt) -> None:
        if not self.verify(domain, payload, receipt):
            _raise(TRUST_RECEIPT_INVALID)


def _decode_key_text(value: object) -> bytes:
    if type(value) is not str or not value or value != value.strip():
        _raise(TRUST_KEY_INVALID)
    try:
        encoded = value.encode("ascii")
        if len(encoded) > MAX_TRUST_KEY_FILE_BYTES or any(
            byte in b" \t\r\n\v\f" for byte in encoded
        ):
            _raise(TRUST_KEY_INVALID)
        key = base64.b64decode(encoded, validate=True)
        if base64.b64encode(key) != encoded:
            _raise(TRUST_KEY_INVALID)
    except TrustError:
        raise
    except (UnicodeEncodeError, binascii.Error, ValueError):
        _raise(TRUST_KEY_INVALID)
    if len(key) < MIN_TRUST_KEY_BYTES:
        _raise(TRUST_KEY_INVALID)
    return key


def _read_key_file(key_file: str | os.PathLike[str]) -> str:
    reason: str | None = None
    descriptor = -1
    result: str | None = None
    try:
        if not isinstance(key_file, (str, os.PathLike)) or isinstance(key_file, bytes):
            _raise(TRUST_KEY_FILE_INVALID)
        path = Path(key_file)
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            _raise(TRUST_KEY_FILE_INVALID)
        if before.st_size < 1 or before.st_size > MAX_TRUST_KEY_FILE_BYTES:
            _raise(TRUST_KEY_FILE_INVALID)
        if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
            _raise(TRUST_KEY_FILE_INVALID)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or (os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077)
        ):
            _raise(TRUST_KEY_FILE_INVALID)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024, MAX_TRUST_KEY_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_TRUST_KEY_FILE_BYTES:
                _raise(TRUST_KEY_FILE_INVALID)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None) != getattr(opened, "st_mtime_ns", None)
        ):
            _raise(TRUST_KEY_FILE_INVALID)
        raw = b"".join(chunks)
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or b"\r" in raw or b"\n" in raw:
            _raise(TRUST_KEY_FILE_INVALID)
        result = raw.decode("ascii")
    except TrustError as error:
        reason = error.reason_code
    except Exception:
        reason = TRUST_KEY_FILE_INVALID
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except Exception:
                if reason is None:
                    reason = TRUST_KEY_FILE_INVALID
    if reason is not None:
        raise TrustError(reason) from None
    if result is None:
        raise TrustError(TRUST_KEY_FILE_INVALID) from None
    return result


def load_trust_service(
    env: Mapping[str, str] | None,
    key_file: str | os.PathLike[str] | None,
) -> NarrativeTrustService:
    """Load one strict base64 key from exactly one dependency-injected source."""

    reason: str | None = None
    try:
        source = os.environ if env is None else env
        env_value = source.get(TRUST_KEY_ENV)
        if env_value is not None and key_file is not None:
            _raise(TRUST_KEY_AMBIGUOUS)
        if env_value is None and key_file is None:
            _raise(TRUST_KEY_MISSING)
        encoded = _read_key_file(key_file) if key_file is not None else env_value
        return NarrativeTrustService(_decode_key_text(encoded))
    except TrustError as error:
        reason = error.reason_code
    except Exception:
        reason = TRUST_KEY_INVALID
    if reason is not None:
        raise TrustError(reason) from None
    raise AssertionError("unreachable")


__all__ = (
    "MAX_TRUST_KEY_FILE_BYTES",
    "MIN_TRUST_KEY_BYTES",
    "NarrativeTrustService",
    "TRUST_ALGORITHM",
    "TRUST_DOMAIN_CLAIM",
    "TRUST_DOMAIN_APPROVAL_ATTESTATION",
    "TRUST_DOMAIN_DRAFT_REVIEW",
    "TRUST_DOMAIN_EVIDENCE",
    "TRUST_DOMAIN_REVIEW_LEDGER",
    "TRUST_DOMAINS",
    "TRUST_KEY_AMBIGUOUS",
    "TRUST_KEY_ENV",
    "TRUST_KEY_FILE_INVALID",
    "TRUST_KEY_INVALID",
    "TRUST_KEY_MISSING",
    "TRUST_PAYLOAD_INVALID",
    "TRUST_RECEIPT_INVALID",
    "TRUST_RECEIPT_SCHEMA_VERSION",
    "TrustError",
    "TrustReceipt",
    "canonical_payload",
    "load_trust_service",
    "receipt_from_payload",
    "receipt_to_payload",
)
