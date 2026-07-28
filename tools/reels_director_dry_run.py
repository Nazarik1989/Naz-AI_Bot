"""Run one Reels director call without persistence, rendering, or publication."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402
import character_state as naz_character  # noqa: E402
import story_production  # noqa: E402


RUBRICS = (
    {
        "key": "agent_content",
        "name": "Рабочая хроника Naz",
        "kind": "work_chronicle",
        "angle": "turn a verified work episode into one coherent release without exposing private material",
        "track_tags": "daily,focus,builder,reflective",
    },
)


async def run(date_text: str) -> dict[str, object]:
    safe_context, risks, resolved_date = main.collect_agent_materials(
        date_text, "reels director no-persist dry-run"
    )
    if not safe_context or resolved_date != date_text:
        raise RuntimeError("director_dry_run_source_missing")
    source_ref = f"agent_content:{resolved_date}:{main.agent_content_hash_for_date(date_text)}"
    source_row = main.chronicle_source_row(
        source_ref=source_ref,
        safe_context=safe_context,
        risks=risks,
        topic=f"рабочая хроника Naz {resolved_date}",
    )
    with patch.object(main.memory, "get_recent_content_signatures", return_value=[]):
        plan = main.scheduled_plan(
            user_id=0,
            platform="telegram",
            slot="agent_content_sync",
            seed=source_ref,
            rubric_rows=RUBRICS,
            source_rows=(source_row,),
            character=naz_character.CharacterState(),
        )
    if (
        plan.production_mode != "story_first"
        or plan.content_format != "story_pack"
    ):
        raise RuntimeError("director_dry_run_not_story_first")

    safe_facts = tuple(source_row.get("safe_facts", ()))
    treatment = await main.generate_reels_director_treatment(plan, safe_facts)
    pack = story_production.plan_story_pack(
        plan,
        safe_facts,
        director_treatment=treatment,
    )
    story_production.validate_story_pack(pack)
    return {
        "status": "accepted",
        "date": resolved_date,
        "plan_id": pack.plan_id,
        "schema": pack.schema,
        "director_version": pack.director_version,
        "scene_count": pack.scene_count,
        "naz_scenes": sum(scene.subject_kind == "naz_human" for scene in treatment.scenes),
        "object_scenes": sum(
            scene.subject_kind == "physical_object" for scene in treatment.scenes
        ),
        "renderer": pack.renderer,
        "persisted": False,
        "media_calls": 0,
    }


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("date", help="Agent Content date in YYYY-MM-DD form")
    args = parser.parse_args()
    if not main.AGENT_CONTENT_DATE_PATTERN.fullmatch(args.date):
        parser.error("date must use YYYY-MM-DD")
    try:
        result = asyncio.run(run(args.date))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "rejected",
            "date": args.date,
            "reason_codes": list(main.reels_director_reason_codes(exc)),
            "persisted": False,
            "media_calls": 0,
        }, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
