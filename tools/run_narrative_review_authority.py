"""Explicit production entry point for the Narrative Review Authority."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import narrative_review_authority as authority
import narrative_review_authority_protocol as protocol
from narrative_review_authority_server import PeerRolePolicy, ReviewAuthorityServer


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run the standalone Narrative Review Authority Broker")
    value.add_argument("--authority-root", required=True)
    value.add_argument("--key-file", required=True)
    value.add_argument("--socket", required=True)
    value.add_argument("--git-root", required=True)
    value.add_argument("--protected-root", action="append", default=[])
    value.add_argument("--uid-role", action="append", default=[])
    value.add_argument("--gid-role", action="append", default=[])
    value.add_argument("--socket-owner-uid", type=int, required=True)
    value.add_argument("--socket-owner-gid", type=int, required=True)
    value.add_argument("--socket-mode", default="0660")
    return value


def _role_map(values: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for value in values:
        try:
            raw_id, role = value.split(":", 1)
            identity = int(raw_id)
        except (ValueError, TypeError):
            raise ValueError("review_authority_role_policy_invalid") from None
        if identity < 0 or role not in protocol.ROLES or identity in result:
            raise ValueError("review_authority_role_policy_invalid")
        result[identity] = role
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        mode = int(args.socket_mode, 8)
        policy = PeerRolePolicy(_role_map(args.uid_role), _role_map(args.gid_role))
        broker = authority.load_authority(
            authority_root=args.authority_root,
            key_file=args.key_file,
            git_root=args.git_root,
            protected_roots=args.protected_root,
        )
        with ReviewAuthorityServer(
            broker, socket_path=args.socket, peer_policy=policy,
            owner_uid=args.socket_owner_uid, owner_gid=args.socket_owner_gid,
            mode=mode,
        ) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        reason = getattr(error, "reason_code", str(error))
        if type(reason) is not str or not reason.startswith("review_authority_"):
            reason = "review_authority_startup_failed"
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
