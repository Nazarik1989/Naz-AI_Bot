"""Run Checkpoint 2 narrative generation against a local fixture.

Offline execution is the default.  Live execution requires both ``--live`` and
``NARRATIVE_TRANSLATOR_LIVE=1`` and reads credentials only inside that path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import tempfile
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import narrative_generation as generation
import narrative_translator as contract


FIXTURE_DIR = ROOT / "tests" / "fixtures" / "narrative_generation"


class OutputPolicyError(ValueError):
    pass


def _locked_visual_mode(data: Mapping[str, object]) -> str:
    return "artifact" if data.get("fixture") == "quiet_object" else "documentary"


def load_fixture(name: str) -> dict[str, object]:
    path = FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_to_input(data: Mapping[str, object]) -> generation.NarrativeGenerationInput:
    facts = tuple(contract.SourceFact(str(item[0]), str(item[1])) for item in data["facts"])
    source_ref = str(data["source_ref"])
    plan_id = str(data["plan_id"])
    editorial_refs = ("editorial:theme", "editorial:structure", "editorial:visual")
    editorial = generation.NarrativeEditorialContext(
        plan_id, source_ref, "story_first", "story_pack", str(data["theme"]), "local_fixture",
        "attentive", "observer", "notice_to_verification", "alongside_reader", "three_variations",
        "concrete_detail", "open_observation", "measured", "balanced", "steady", "optional",
        "concrete_objects", _locked_visual_mode(data), str(data["visual_subject"]), "grounded", editorial_refs,
    )
    naz_state = contract.CharacterStateSnapshot("naz", "naz-core-v1", 7, 55, 64, 25, 71, 60, 48, "attentive", "curious", "steady", "fixture event", ("prior fixture",), "naz-state-fixture")
    void_state = contract.CharacterStateSnapshot("void", "void-core-v1", 5, 42, 58, 20, 68, 66, 35, "observant", "quiet", "calm", "fixture event", ("prior fixture",), "void-state-fixture")
    relation = contract.RelationshipStateSnapshot("duo-v1", 3, 72, 67, 18, 61, 75, "cooperative", "fixture", (), (), (), "relationship-state-fixture")

    def canon_snapshot(character: str) -> contract.CharacterCanonSnapshot:
        return contract.CharacterCanonSnapshot(character, (
            contract.CanonSourceRef(character, f"{character}-personality", f"canon/{character}/personality", "v1", f"{character}-personality-hash", "personality"),
            contract.CanonSourceRef(character, f"{character}-visual", f"canon/{character}/visual", "v1", f"{character}-visual-hash", "visual"),
            contract.CanonSourceRef(character, f"{character}-relationship", f"canon/{character}/relationship", "v1", f"{character}-relationship-hash", "relationship"),
        ), f"{character}-canon-fixture")

    naz_canon = canon_snapshot("naz")
    void_canon = canon_snapshot("void")
    return generation.NarrativeGenerationInput(
        source_ref=source_ref,
        source_facts=facts,
        editorial_plan=editorial,
        naz_state=naz_state,
        void_state=void_state,
        relationship_state=relation,
        naz_canon=naz_canon,
        void_canon=void_canon,
        naz_prompt_context=generation.CharacterPromptContext("naz", tuple(x.source_id for x in naz_canon.canon_refs), "v1", naz_state.snapshot_ref, str(data["naz_prompt"])),
        void_prompt_context=generation.CharacterPromptContext("void", tuple(x.source_id for x in void_canon.canon_refs), "v1", void_state.snapshot_ref, str(data["void_prompt"])),
        relationship_prompt_context=generation.RelationshipPromptContext(relation.snapshot_ref, str(data["relationship_prompt"])),
        diversity_context=contract.NarrativeDiversityContext(()),
    )


def _statement(text: str, refs: list[str]) -> dict[str, object]:
    return {"text": text, "inference_kind": "bounded_interpretation", "source_fact_refs": refs, "editorial_refs": ["editorial:theme"], "canon_refs": []}


def _interpretation(character: str, text: str, refs: list[str], *, emotion: str, form: str, ending: str) -> dict[str, object]:
    return {
        "character_id": character,
        "text": text,
        "source_fact_refs": refs,
        "canon_refs": [f"{character}-personality"],
        "interpretation_mode": "situated_observation",
        "thematic_axis": "verification_and_attention",
        "emotional_register": emotion,
        "rhetorical_form": form,
        "narrative_distance": "close",
        "humor_mode": "none",
        "sarcasm_target": null_value(),
        "ending_mode": ending,
        "continuity_basis": ["explicit prompt context", "current immutable state"],
    }


def null_value() -> None:
    return None


def _visual(character: str, mode: str, subject: str, refs: list[str]) -> dict[str, object]:
    return {
        "mode_hint": mode,
        "narrative_subject": subject,
        "human_presence_policy": "none",
        "nonhuman_presence_policy": "none",
        "approved_motifs": ["visible working materials", "specific evidence"],
        "excluded_motifs": ["generic robot", "corporate spectacle"],
        "source_fact_refs": refs,
        "visual_canon_refs": [f"{character}-visual"],
        "subjects": [{"subject_kind": "object", "character_id": None, "source_fact_refs": refs, "identity_canon_refs": []}],
    }


def fake_generation_payload(data: Mapping[str, object]) -> dict[str, object]:
    facts = list(data["facts"])
    ids = [str(item[0]) for item in facts]
    snippets = [str(item[1]) for item in facts]
    first = ids[0]
    middle = ids[min(1, len(ids) - 1)]
    last = ids[-1]
    subject = str(data["visual_subject"])
    allow_duo = bool(data["allow_duo"])
    locked_visual_mode = _locked_visual_mode(data)
    locked_ending = "open_observation"

    def candidate(index: int, primary: str, secondary: str | None, mode: str, emotion: str, form: str, ending: str, visual_mode: str) -> dict[str, object]:
        relation = "explicit" if secondary else "none"
        if index == 1:
            story = {
                "hook": _statement(f"A close observation begins with one concrete detail: {snippets[0]}", [first]),
                "human_problem": _statement(f"Attention can drift from the object to an early conclusion, although {snippets[min(1, len(snippets) - 1)]}", [middle]),
                "tension": _statement("The scene holds between the desire to conclude and the discipline of checking what is actually present.", ids[: min(3, len(ids))]),
                "turning_point": _statement(f"A small return to direct evidence changes the pace: {snippets[-1]}", [last]),
                "resolution": _statement("The ending stays beside the verified details and leaves their wider meaning open.", ids),
            }
            primary_text = f"{primary.upper()} stays close to the material detail and lets confidence grow only as each recorded observation earns it."
        elif index == 2:
            story = {
                "hook": _statement(f"A work log opens after the first action has already happened: {snippets[0]}", [first]),
                "human_problem": _statement(f"The challenge is reconstructing a reliable sequence from separate checks, including this one: {snippets[min(1, len(snippets) - 1)]}", [middle]),
                "tension": _statement("Each entry is useful alone, but their order determines whether the result deserves confidence.", ids[: min(3, len(ids))]),
                "turning_point": _statement(f"The chronology becomes legible when the final verification is placed beside the earlier steps: {snippets[-1]}", [last]),
                "resolution": _statement("The log closes without triumph, preserving a traceable path another reader can inspect.", ids),
            }
            primary_text = f"{primary.upper()} treats sequence as the central subject, reading the notes as a record that can remain calm without becoming vague."
        else:
            story = {
                "hook": _statement(f"A visual explanation starts by placing the first recorded action in its own node: {snippets[0]}", [first]),
                "human_problem": _statement(f"Isolated facts do not yet show how the process holds together; another node records that {snippets[min(1, len(snippets) - 1)]}", [middle]),
                "tension": _statement("The diagram remains incomplete until its arrows distinguish preparation, verification, and review.", ids[: min(3, len(ids))]),
                "turning_point": _statement(f"The last evidence supplies the missing connection rather than a dramatic reveal: {snippets[-1]}", [last]),
                "resolution": _statement("The final frame keeps the explanatory structure visible and leaves room for a later branch.", ids),
            }
            primary_text = f"{primary.upper()} reads the process spatially, using the arrangement of evidence to show relations without forcing a universal conclusion."
        secondary_text = None if secondary is None else f"{secondary.upper()} follows the same evidence from a distinct angle, agreeing on the next check without being reduced to a fixed opposing role."
        return {
            "candidate_id": f"candidate-{index}",
            "rank": index,
            "primary_character_id": primary,
            "secondary_character_id": secondary,
            "presence_mode": relation,
            **story,
            "primary_interpretation": _interpretation(primary, primary_text, ids, emotion=emotion, form=form, ending=ending),
            "secondary_interpretation": None if secondary is None else _interpretation(secondary, secondary_text, ids, emotion="cooperative", form="parallel_note", ending=ending),
            "interaction_mode": None if secondary is None else mode,
            "relation_to_story": None if secondary is None else "two readings converge on one grounded next action",
            "visual_direction": _visual(primary, visual_mode, f"{subject}; variation {index}", ids),
            "story_type": ("intimate_observation", "process_chronicle", "explanatory_sequence")[index - 1],
        }

    candidates = [
        candidate(1, "naz", None, "none", "gentle_curiosity", "close_observation", locked_ending, locked_visual_mode),
        candidate(2, "void", None, "none", "measured_attention", "work_log", locked_ending, locked_visual_mode),
    ]
    if allow_duo:
        candidates.append(candidate(3, "void", "naz", "cooperative_review", "shared_focus", "two_notes", locked_ending, locked_visual_mode))
    else:
        candidates.append(candidate(3, "naz", None, "none", "quiet_warmth", "object_study", locked_ending, locked_visual_mode))
    return {"schema": generation.GENERATION_SCHEMA, "candidates": candidates}


def fake_adjudication_payload(
    drafts_payload: Mapping[str, object],
    context: generation.NarrativeGenerationInput,
) -> dict[str, object]:
    parsed_drafts = generation.parse_generation_response(drafts_payload, context)
    binding_by_id = {item.candidate_id: generation.build_draft_bindings(item, context) for item in parsed_drafts}
    candidates = []
    for draft in drafts_payload["candidates"]:
        binding = binding_by_id[draft["candidate_id"]]
        statement_bindings = {item.statement_name: item.statement_digest for item in binding.statement_bindings}
        statement_decisions = [
            {"statement_name": name, "statement_digest": statement_bindings[name], "decision": "supported", "reason_codes": []}
            for name in generation.STORY_FIELDS
            if draft[name]["inference_kind"] == "bounded_interpretation"
        ]
        secondary = draft["secondary_character_id"]
        candidates.append({
            "candidate_id": draft["candidate_id"],
            "authority_context_digest": binding.authority_context_digest,
            "draft_digest": binding.draft_digest,
            "statement_decisions": statement_decisions,
            "primary_continuity": {"character_id": draft["primary_character_id"], "interpretation_digest": binding.primary_interpretation_digest, "decision": "supported", "reason_codes": []},
            "secondary_continuity": None if secondary is None else {"character_id": secondary, "interpretation_digest": binding.secondary_interpretation_digest, "decision": "supported", "reason_codes": []},
            "relationship_continuity": None if secondary is None else {"relationship_payload_digest": binding.relationship_payload_digest, "decision": "supported", "reason_codes": []},
            "visual_grounding": {"visual_payload_digest": binding.visual_payload_digest, "decision": "supported", "reason_codes": []},
            "editorial_alignment": {"editorial_alignment_digest": binding.editorial_alignment_digest, "decision": "supported", "reason_codes": []},
            "overall_decision": "supported",
            "reason_codes": [],
        })
    return {"schema": generation.ADJUDICATION_SCHEMA, "candidates": candidates}


class DeterministicFixtureClient:
    def __init__(self, generation_payload: Mapping[str, object], adjudication_payload: Mapping[str, object]):
        self.generation_payload = generation_payload
        self.adjudication_payload = adjudication_payload
        self.calls: list[str] = []

    def generate_json(self, request: generation.NarrativeModelRequest) -> Mapping[str, object]:
        self.calls.append(request.request_kind)
        if request.request_kind == "generation":
            return self.generation_payload
        if request.request_kind == "adjudication":
            return self.adjudication_payload
        raise RuntimeError("offline fixture does not require repair")


class LiveOpenAIClient:
    def __init__(self, api_key: str, base_url: str | None):
        from openai import OpenAI

        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def generate_json(self, request: generation.NarrativeModelRequest) -> str:
        response = self._client.chat.completions.create(
            model=request.model,
            messages=[{"role": "system", "content": request.system_prompt}, {"role": "user", "content": request.user_prompt}],
            response_format={"type": "json_schema", "json_schema": dict(request.response_schema)},
            temperature=0,
        )
        return response.choices[0].message.content or ""


def run(
    args: argparse.Namespace,
    *,
    provider_factory=None,
) -> tuple[generation.NarrativeGenerationResult | None, dict[str, object]]:
    data = load_fixture(args.fixture)
    context = fixture_to_input(data)
    if args.live:
        if os.environ.get("NARRATIVE_TRANSLATOR_LIVE") != "1":
            return None, {"live_smoke": "not_run", "reason": "NARRATIVE_TRANSLATOR_LIVE_not_enabled"}
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None, {"live_smoke": "not_run", "reason": "local_credentials_unavailable"}
        factory = LiveOpenAIClient if provider_factory is None else provider_factory
        client = factory(api_key, os.environ.get("OPENAI_BASE_URL"))
    else:
        draft_payload = fake_generation_payload(data)
        client = DeterministicFixtureClient(draft_payload, fake_adjudication_payload(draft_payload, context))
    service = generation.NarrativeGenerationService(client, generation_model=args.model, adjudication_model=args.model)
    result = service.generate(context)
    report = generation.safe_result_summary(result)
    report["fixture"] = args.fixture
    report["mode"] = "live" if args.live else "offline"
    if args.show_content and result.selected_candidate_id is not None:
        selected = next(item for item in result.candidates if item.candidate_id == result.selected_candidate_id)
        assert selected.package is not None
        report["selected_content"] = {name: getattr(selected.package, name).text for name in generation.STORY_FIELDS}
    return result, report


def write_output(path_value: str, rendered: str, *, overwrite: bool) -> None:
    path = Path(path_value)
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise OutputPolicyError("output_parent_missing")
    output_bytes = (rendered + "\n").encode("utf-8")
    if not overwrite:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise OutputPolicyError("output_exists") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(output_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise
        return
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            handle.write(output_bytes)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", choices=("env_utf8", "quiet_object", "duo_context"), default="env_utf8")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--show-content", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--model", default="local-narrative-model")
    args = parser.parse_args()
    if args.overwrite and not args.output:
        parser.error("--overwrite requires --output")
    _, report = run(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        try:
            write_output(args.output, rendered, overwrite=args.overwrite)
        except OutputPolicyError as error:
            parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
