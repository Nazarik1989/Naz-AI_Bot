"""One-request Unix socket service exposing the narrow Naz Voice Hub contract."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import memory
import naz_realtime_adapter as adapter


PROTOCOL = "voice_hub.naz.v1"
MAX_IPC_BYTES = 64 * 1024


def _exact_fields(payload: dict[str, Any], fields: set[str]) -> None:
    if set(payload) != fields:
        raise ValueError("request fields are invalid")


def handle_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 20 <= len(request_id) <= 100:
        raise ValueError("request_id is invalid")
    if payload.get("protocol") != PROTOCOL:
        raise ValueError("protocol is invalid")
    operation = payload.get("operation")
    if operation == "persona_instructions":
        _exact_fields(payload, {"protocol", "request_id", "operation", "user_id"})
        instructions = adapter.get_persona_instructions(payload["user_id"])
        return {"request_id": request_id, "ok": True, "instructions": instructions}
    if operation == "final_summary":
        _exact_fields(
            payload,
            {"protocol", "request_id", "operation", "user_id", "idempotency_key", "summary"},
        )
        saved = adapter.save_final_summary(
            payload["user_id"],
            payload["summary"],
            idempotency_key=payload["idempotency_key"],
        )
        return {
            "request_id": request_id,
            "ok": True,
            "receipt": f"naz:{payload['idempotency_key']}",
            "saved": saved,
        }
    raise ValueError("operation is invalid")


async def serve_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    request_id = "invalid"
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        if not raw or len(raw) > MAX_IPC_BYTES:
            raise ValueError("request size is invalid")
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("request_id"), str):
            request_id = payload["request_id"][:100]
        response = handle_request(payload)
    except PermissionError:
        response = {"request_id": request_id, "ok": False, "error": "forbidden"}
    except (TypeError, ValueError, json.JSONDecodeError):
        response = {"request_id": request_id, "ok": False, "error": "invalid_request"}
    except Exception:
        response = {"request_id": request_id, "ok": False, "error": "internal_error"}
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    writer.write(encoded)
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def run(socket_path: Path) -> None:
    memory.init_db()
    if not socket_path.is_absolute():
        raise RuntimeError("socket path must be absolute")
    socket_parent = socket_path.parent.resolve(strict=True)
    socket_path = socket_parent / socket_path.name
    if not socket_parent.is_dir():
        raise RuntimeError("socket parent directory does not exist")
    if socket_path.exists() or socket_path.is_symlink():
        if socket_path.is_symlink() or not socket_path.is_socket():
            raise RuntimeError("refusing to replace a non-socket path")
        socket_path.unlink()
    server = await asyncio.start_unix_server(serve_client, path=str(socket_path))
    os.chmod(socket_path, 0o660)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Naz Realtime Voice adapter")
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path(os.getenv("NAZ_REALTIME_SOCKET", "/run/naz-realtime/adapter.sock")),
    )
    args = parser.parse_args()
    asyncio.run(run(args.socket))


if __name__ == "__main__":
    main()
