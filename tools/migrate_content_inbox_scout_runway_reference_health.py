"""Import current Scout Runway reference evidence without provider transport."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
import naz_story_worker  # noqa: E402
import story_pack_control  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import one historical plan into reference-health-v1."
    )
    parser.add_argument("--plan-id", required=True)
    for scene in ("01", "02", "05"):
        parser.add_argument(f"--scene-{scene}-status", required=True)
        parser.add_argument(f"--scene-{scene}-task-digest", required=True)
    parser.add_argument("--scene-02-failure-code", required=True)
    parser.add_argument("--scene-05-failure-code", required=True)
    return parser


def cli() -> int:
    args = _parser().parse_args()
    try:
        catalog = naz_story_worker._reference_catalog(
            naz_story_worker.load_config().reference_path
        )
        result = story_pack_control.import_current_plan_reference_health(
            main.NAZ_STORY_PACK_ROOT,
            args.plan_id,
            health_root=main.NAZ_RUNWAY_REFERENCE_HEALTH_ROOT,
            references={role: selection.path for role, selection in catalog.items()},
            audited_tasks={
                "01_hook": {
                    "status": args.scene_01_status,
                    "failure_code": None,
                    "task_identity_digest": args.scene_01_task_digest,
                },
                "02_problem": {
                    "status": args.scene_02_status,
                    "failure_code": args.scene_02_failure_code,
                    "task_identity_digest": args.scene_02_task_digest,
                },
                "05_conclusion": {
                    "status": args.scene_05_status,
                    "failure_code": args.scene_05_failure_code,
                    "task_identity_digest": args.scene_05_task_digest,
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "blocked",
            "reason": str(getattr(exc, "reason_code", exc)),
            "provider_calls": 0,
        }, sort_keys=True))
        return 1
    print(json.dumps({"status": "migrated", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
