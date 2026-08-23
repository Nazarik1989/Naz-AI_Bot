"""Explicit Unix-domain socket server for the Review Authority Broker."""
from __future__ import annotations

import os
import socket
import stat
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import narrative_review_authority as authority
import narrative_review_authority_protocol as protocol


SERVER_INVALID = "review_authority_server_invalid"
PEER_UNAUTHORIZED = "review_authority_peer_unauthorized"
SOCKET_INVALID = "review_authority_socket_invalid"


class ServerError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason: str) -> None:
    raise ServerError(reason)


@dataclass(frozen=True, slots=True)
class PeerRolePolicy:
    uid_roles: Mapping[int, str]
    gid_roles: Mapping[int, str]

    def __post_init__(self) -> None:
        uid = dict(self.uid_roles)
        gid = dict(self.gid_roles)
        if any(type(key) is not int or key < 0 or type(value) is not str or value not in protocol.ROLES for key, value in uid.items()):
            _fail(SERVER_INVALID)
        if any(type(key) is not int or key < 0 or type(value) is not str or value not in protocol.ROLES for key, value in gid.items()):
            _fail(SERVER_INVALID)
        object.__setattr__(self, "uid_roles", MappingProxyType(uid))
        object.__setattr__(self, "gid_roles", MappingProxyType(gid))

    def role_for(self, uid: int, gid: int) -> str:
        uid_role = self.uid_roles.get(uid)
        gid_role = self.gid_roles.get(gid)
        if uid_role is not None and gid_role is not None and uid_role != gid_role:
            _fail(PEER_UNAUTHORIZED)
        role = uid_role or gid_role
        if role is None:
            _fail(PEER_UNAUTHORIZED)
        return role


def _absolute_socket_path(value: str | os.PathLike[str]) -> Path:
    if not hasattr(socket, "AF_UNIX"):
        _fail(SOCKET_INVALID)
    if not isinstance(value, (str, os.PathLike)) or isinstance(value, bytes):
        _fail(SOCKET_INVALID)
    path = Path(value)
    if not path.is_absolute():
        _fail(SOCKET_INVALID)
    cursor = path
    while True:
        if os.path.lexists(cursor) and cursor.is_symlink():
            _fail(SOCKET_INVALID)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return path.resolve(strict=False)


def validate_socket(path: Path, *, owner_uid: int, owner_gid: int, mode: int) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise ServerError(SOCKET_INVALID) from None
    if (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != owner_uid or info.st_gid != owner_gid
        or stat.S_IMODE(info.st_mode) != mode
    ):
        _fail(SOCKET_INVALID)


class ReviewAuthorityServer:
    """One explicit server instance; importing this module has no side effects."""

    def __init__(
        self, broker: authority.ReviewAuthority, *, socket_path: str | os.PathLike[str],
        peer_policy: PeerRolePolicy, owner_uid: int, owner_gid: int, mode: int = 0o660,
        request_timeout: float = 10.0,
    ):
        if type(broker) is not authority.ReviewAuthority or type(peer_policy) is not PeerRolePolicy:
            _fail(SERVER_INVALID)
        if type(owner_uid) is not int or owner_uid < 0 or type(owner_gid) is not int or owner_gid < 0:
            _fail(SERVER_INVALID)
        if type(mode) is not int or mode & ~0o777 or mode == 0:
            _fail(SERVER_INVALID)
        if type(request_timeout) not in {int, float} or not 0.1 <= request_timeout <= 30:
            _fail(SERVER_INVALID)
        self.broker = broker
        self.socket_path = _absolute_socket_path(socket_path)
        self.peer_policy = peer_policy
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.mode = mode
        self.request_timeout = float(request_timeout)
        self._socket: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._socket is not None:
            _fail(SERVER_INVALID)
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir() or os.path.lexists(self.socket_path):
            _fail(SOCKET_INVALID)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.socket_path))
            os.chmod(self.socket_path, self.mode)
            if hasattr(os, "chown"):
                os.chown(self.socket_path, self.owner_uid, self.owner_gid)
            validate_socket(
                self.socket_path, owner_uid=self.owner_uid,
                owner_gid=self.owner_gid, mode=self.mode,
            )
            sock.listen(32)
            sock.settimeout(0.5)
        except BaseException:
            sock.close()
            if os.path.lexists(self.socket_path) and not self.socket_path.is_symlink():
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
            raise
        self._socket = sock

    @staticmethod
    def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
        if not hasattr(socket, "SO_PEERCRED"):
            _fail(PEER_UNAUTHORIZED)
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", raw)
        except Exception:
            raise ServerError(PEER_UNAUTHORIZED) from None
        if pid <= 0 or uid < 0 or gid < 0:
            _fail(PEER_UNAUTHORIZED)
        return pid, uid, gid

    def serve_once(self) -> None:
        sock = self._socket
        if sock is None:
            _fail(SERVER_INVALID)
        try:
            connection, _ = sock.accept()
        except socket.timeout:
            return
        with connection:
            connection.settimeout(self.request_timeout)
            request_id = "invalid-request"
            try:
                _, uid, gid = self.peer_credentials(connection)
                role = self.peer_policy.role_for(uid, gid)
                stream = connection.makefile("rwb", buffering=0)
                try:
                    # EOF is required before dispatch.  This validates the
                    # complete bounded connection as exactly one frame, so a
                    # delayed second frame can never follow a dispatched first.
                    request = protocol.request_from_payload(protocol.read_single_frame(stream))
                    request_id = request.request_id
                    response = self.broker.handle(role, request)
                    protocol.write_frame(stream, response.to_payload())
                finally:
                    stream.close()
            except (protocol.ProtocolError, ServerError) as error:
                reason = getattr(error, "reason_code", SERVER_INVALID)
                try:
                    response = protocol.make_error(request_id, reason)
                    connection.sendall(protocol.encode_frame(response.to_payload()))
                except Exception:
                    pass
            except Exception:
                try:
                    response = protocol.make_error(request_id, SERVER_INVALID)
                    connection.sendall(protocol.encode_frame(response.to_payload()))
                except Exception:
                    pass

    def serve_forever(self) -> None:
        if self._socket is None:
            self.start()
        while not self._stop.is_set():
            self.serve_once()

    def close(self) -> None:
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            sock.close()
        if os.path.lexists(self.socket_path):
            info = os.lstat(self.socket_path)
            if stat.S_ISSOCK(info.st_mode):
                os.unlink(self.socket_path)

    def __enter__(self) -> "ReviewAuthorityServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = (
    "PEER_UNAUTHORIZED", "SERVER_INVALID", "SOCKET_INVALID", "PeerRolePolicy",
    "ReviewAuthorityServer", "ServerError", "validate_socket",
)
