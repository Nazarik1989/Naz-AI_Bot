"""State-free client proxy for the Narrative Review Authority Broker."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Callable

import narrative_review_authority_protocol as protocol
from narrative_review_authority_server import validate_socket


CLIENT_INVALID = "review_authority_client_invalid"
BROKER_REJECTED = "review_authority_broker_rejected"


class ClientError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ReviewAuthorityClient:
    """Thin proxy containing only immutable transport configuration."""

    __slots__ = ("_socket_path", "_owner_uid", "_owner_gid", "_mode", "_timeout")

    def __init__(
        self, socket_path: str | os.PathLike[str], *, owner_uid: int,
        owner_gid: int, mode: int = 0o660, timeout: float = 10.0,
    ):
        if not hasattr(socket, "AF_UNIX"):
            raise ClientError(CLIENT_INVALID)
        if not isinstance(socket_path, (str, os.PathLike)) or isinstance(socket_path, bytes):
            raise ClientError(CLIENT_INVALID)
        path = Path(socket_path)
        if not path.is_absolute() or type(owner_uid) is not int or type(owner_gid) is not int:
            raise ClientError(CLIENT_INVALID)
        if type(mode) is not int or type(timeout) not in {int, float} or not 0.1 <= timeout <= 120:
            raise ClientError(CLIENT_INVALID)
        self._socket_path = str(path)
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid
        self._mode = mode
        self._timeout = float(timeout)

    def exchange(self, request_id: str, operation: str, payload: dict[str, object]) -> dict[str, object]:
        # Constructing Request is the exact-type pre-serialization boundary.
        try:
            request = protocol.Request(request_id, operation, payload)
            validate_socket(
                Path(self._socket_path), owner_uid=self._owner_uid,
                owner_gid=self._owner_gid, mode=self._mode,
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout)
                connection.connect(self._socket_path)
                stream = connection.makefile("rwb", buffering=0)
                try:
                    protocol.write_frame(stream, request.to_payload())
                    # One connection carries one request.  EOF on the server's
                    # read side is the unambiguous end-of-request marker.
                    connection.shutdown(socket.SHUT_WR)
                    response = protocol.response_from_payload(protocol.read_frame(stream))
                finally:
                    stream.close()
        except protocol.ProtocolError as error:
            raise ClientError(error.reason_code) from None
        except ClientError:
            raise
        except Exception:
            raise ClientError(CLIENT_INVALID) from None
        if response.request_id != request_id:
            raise ClientError(CLIENT_INVALID)
        if not response.ok:
            raise ClientError(response.error or BROKER_REJECTED)
        assert response.result is not None
        return response.result

    def health(self, request_id: str) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_HEALTH, {})

    def register_draft(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_REGISTER_DRAFT, payload)

    def latest_state(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_LATEST_STATE, payload)

    def append_review(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_APPEND_REVIEW, payload)

    def prepare_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_PREPARE_APPROVAL, payload)

    def commit_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_COMMIT_APPROVAL, payload)

    def verify_ready(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self.exchange(request_id, protocol.OP_VERIFY_READY, payload)


__all__ = ("BROKER_REJECTED", "CLIENT_INVALID", "ClientError", "ReviewAuthorityClient")
