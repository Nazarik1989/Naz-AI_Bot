from __future__ import annotations

import copy
import argparse
import builtins
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import narrative_generation as ng
import narrative_translator as nt
import tools.run_narrative_generation_fixture as fixture_cli
from tools.run_narrative_generation_fixture import (
    DeterministicFixtureClient,
    fake_adjudication_payload,
    fake_generation_payload,
    fixture_to_input,
    load_fixture,
)


def scenario(name: str = "env_utf8"):
    data = load_fixture(name)
    context = fixture_to_input(data)
    draft_payload = fake_generation_payload(data)
    adjudication_payload = fake_adjudication_payload(draft_payload, context)
    return data, context, draft_payload, adjudication_payload


def parsed(name: str = "env_utf8"):
    _, context, draft_payload, adjudication_payload = scenario(name)
    drafts = ng.parse_generation_response(draft_payload, context)
    bindings = tuple(ng.build_draft_bindings(draft, context) for draft in drafts)
    adjudications = ng.parse_adjudication_response(adjudication_payload, drafts, bindings)
    return context, drafts, adjudications


def bindings_for(context, drafts):
    return tuple(ng.build_draft_bindings(draft, context) for draft in drafts)


def run_with(draft_payload=None, adjudication_payload=None, name: str = "env_utf8"):
    data, context, default_drafts, default_adjudication = scenario(name)
    draft_payload = default_drafts if draft_payload is None else draft_payload
    adjudication_payload = default_adjudication if adjudication_payload is None else adjudication_payload
    client = DeterministicFixtureClient(draft_payload, adjudication_payload)
    result = ng.NarrativeGenerationService(client, generation_model="draft-model", adjudication_model="judge-model").generate(context)
    return result, client, context, data


def run_explicit(context, draft_payload, adjudication_payload):
    client = DeterministicFixtureClient(draft_payload, adjudication_payload)
    result = ng.NarrativeGenerationService(client, generation_model="draft-model", adjudication_model="judge-model").generate(context)
    return result, client


class QueueClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def generate_json(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


# Architecture and immutable input


def test_module_import_has_no_runtime_side_effect_modules():
    forbidden = {"main", "story_production", "editorial_orchestrator", "memory", "sqlite3", "openai", "requests", "httpx"}
    before = set(sys.modules)
    importlib.reload(ng)
    assert not ((set(sys.modules) - before) & forbidden)


def test_module_source_has_no_runtime_imports():
    source = Path(ng.__file__).read_text(encoding="utf-8")
    for token in ("import main", "import sqlite3", "import openai", "import story_production", "import memory"):
        assert token not in source


def test_input_is_frozen():
    _, context, _, _ = scenario()
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.source_ref = "changed"


@pytest.mark.parametrize("field", ["naz_state", "void_state", "naz_canon", "void_canon", "editorial_plan", "diversity_context"])
def test_nested_authoritative_objects_are_frozen(field):
    _, context, _, _ = scenario()
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        setattr(getattr(context, field), next(iter(getattr(context, field).__dataclass_fields__)), "changed")


def test_prompt_context_state_binding_rejected():
    _, context, _, _ = scenario()
    bad = dataclasses.replace(context.naz_prompt_context, state_snapshot_ref="other")
    with pytest.raises(ValueError):
        dataclasses.replace(context, naz_prompt_context=bad)


def test_source_binding_rejected():
    _, context, _, _ = scenario()
    with pytest.raises(ValueError):
        dataclasses.replace(context, source_ref="other")


def test_generation_prompt_contains_character_freedom_not_fixed_formula():
    _, context, _, _ = scenario()
    prompt = ng.build_generation_request(context, "model").system_prompt.casefold()
    assert "may be primary" in prompt
    assert "naz gives motion" not in prompt
    assert "void gives perspective" not in prompt


def test_requests_are_provider_neutral_data():
    _, context, _, _ = scenario()
    request = ng.build_generation_request(context, "model")
    assert type(request) is ng.NarrativeModelRequest
    assert request.request_kind == "generation"
    assert request.response_schema["strict"] is True


def test_draft_binding_uses_checkpoint_one_digest_helper(monkeypatch):
    context, drafts, _ = parsed()
    original = nt._digest
    calls = []
    def spy(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(nt, "_digest", spy)
    binding = ng.build_draft_bindings(drafts[0], context)
    assert len(calls) >= 9
    assert len(binding.draft_digest) == 64


def test_authority_context_binding_has_complete_immutable_shape():
    _, context, _, _ = scenario()
    binding = ng.build_authority_context_binding(context)
    assert set(dataclasses.asdict(binding)) == {
        "binding_version", "plan_binding_digest", "source_payload_digest",
        "naz_state_digest", "void_state_digest", "relationship_state_digest",
        "naz_canon_digest", "void_canon_digest", "naz_prompt_context_digest",
        "void_prompt_context_digest", "relationship_prompt_context_digest",
        "diversity_context_digest", "evidence_policy_digest",
        "validation_contract_version", "authority_context_digest",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.authority_context_digest = "0" * 64


def test_adjudication_request_carries_code_issued_authority_echo():
    context, drafts, _ = parsed()
    bindings = bindings_for(context, drafts)
    request = ng.build_adjudication_request(context, drafts, bindings, "m")
    candidates = json.loads(request.user_prompt)["candidates"]
    assert [item["bindings"]["authority_context_digest"] for item in candidates] == [
        binding.authority_context_digest for binding in bindings
    ]


def test_adjudication_request_has_privacy_safe_relationship_summary():
    context, drafts, _ = parsed("duo_context")
    request = ng.build_adjudication_request(context, drafts, bindings_for(context, drafts), "m")
    payload = json.loads(request.user_prompt)
    summary = payload["context"]["relationship_state_summary"]
    assert set(summary) == {"snapshot_ref", "revision", "version", "trust", "warmth", "friction", "curiosity", "respect", "mode"}
    assert "inside_jokes" not in request.user_prompt
    assert "changed_minds" not in request.user_prompt
    assert "unresolved_topics" not in request.user_prompt


def test_relationship_summary_is_null_when_relationship_absent():
    _, context, _, _ = scenario("quiet_object")
    without_relation = dataclasses.replace(context, relationship_state=None, relationship_prompt_context=None)
    assert ng._prompt_context(without_relation)["relationship_state_summary"] is None


# Strict JSON/schema parser


@pytest.mark.parametrize("raw", ["", "not json", "```json\n{}\n```", "[]", "null", "{} trailing"])
def test_generation_parser_rejects_non_json_document(raw):
    _, context, _, _ = scenario()
    with pytest.raises(ng.NarrativeGenerationError) as error:
        ng.parse_generation_response(raw, context)
    assert error.value.repairable


def test_generation_parser_accepts_raw_json():
    _, context, payload, _ = scenario()
    result = ng.parse_generation_response(json.dumps(payload), context)
    assert len(result) == 3


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_generation_parser_requires_exactly_three_candidates(count):
    _, context, payload, _ = scenario()
    payload["candidates"] = payload["candidates"][:count]
    if count == 4:
        payload["candidates"].append(copy.deepcopy(payload["candidates"][0]))
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_generation_response(payload, context)


def test_generation_parser_rejects_top_level_extra_key():
    _, context, payload, _ = scenario()
    payload["commentary"] = "hello"
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_generation_response(payload, context)


def test_generation_parser_rejects_candidate_missing_key():
    _, context, payload, _ = scenario()
    del payload["candidates"][0]["story_type"]
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_generation_response(payload, context)


def test_generation_parser_rejects_candidate_extra_key():
    _, context, payload, _ = scenario()
    payload["candidates"][0]["score"] = 1
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_generation_response(payload, context)


def test_generation_parser_rejects_duplicate_candidate_id():
    _, context, payload, _ = scenario()
    payload["candidates"][1]["candidate_id"] = payload["candidates"][0]["candidate_id"]
    with pytest.raises(ng.NarrativeGenerationError, match="generation_candidate_id_duplicate"):
        ng.parse_generation_response(payload, context)


def test_generation_parser_allows_duplicate_rank_for_digest_tie_break():
    _, context, payload, _ = scenario()
    payload["candidates"][1]["rank"] = payload["candidates"][0]["rank"]
    drafts = ng.parse_generation_response(payload, context)
    assert drafts[0].rank == drafts[1].rank


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("hook", "source_fact_refs"), ["unknown-fact"], "draft_source_fact_ref_unknown"),
        (("hook", "editorial_refs"), ["unknown-editorial"], "draft_editorial_ref_unknown"),
        (("primary_interpretation", "canon_refs"), ["unknown-canon"], "draft_canon_ref_unknown"),
        (("visual_direction", "visual_canon_refs"), ["unknown-canon"], "draft_canon_ref_unknown"),
    ],
)
def test_generation_parser_rejects_unknown_references(path, value, reason):
    _, context, payload, _ = scenario()
    payload["candidates"][0][path[0]][path[1]] = value
    with pytest.raises(ng.NarrativeGenerationError, match=reason) as error:
        ng.parse_generation_response(payload, context)
    assert not error.value.repairable


@pytest.mark.parametrize("injected", sorted(ng.FORBIDDEN_AUTHORITY_KEYS))
def test_generation_parser_rejects_authoritative_field_injection(injected):
    _, context, payload, _ = scenario()
    payload["candidates"][0]["hook"][injected] = "model-authored"
    with pytest.raises(ng.NarrativeGenerationError, match="model_authority_injection") as error:
        ng.parse_generation_response(payload, context)
    assert not error.value.repairable


def test_generation_parser_rejects_identical_candidates_with_new_labels():
    _, context, payload, _ = scenario()
    base = payload["candidates"][0]
    for index in (1, 2):
        replacement = copy.deepcopy(base)
        replacement["candidate_id"] = f"candidate-{index + 1}"
        replacement["rank"] = index + 1
        payload["candidates"][index] = replacement
    with pytest.raises(ng.NarrativeGenerationError, match="generation_candidates_not_substantively_diverse") as error:
        ng.parse_generation_response(payload, context)
    assert not error.value.repairable


@pytest.mark.parametrize(
    ("primary", "secondary", "mode"),
    [("other", None, "none"), ("naz", "naz", "explicit"), ("naz", None, "explicit"), ("naz", "void", "none")],
)
def test_generation_parser_rejects_invalid_character_modes(primary, secondary, mode):
    _, context, payload, _ = scenario()
    candidate = payload["candidates"][0]
    candidate["primary_character_id"] = primary
    candidate["secondary_character_id"] = secondary
    candidate["presence_mode"] = mode
    with pytest.raises(ng.NarrativeGenerationError, match="draft_character_mode_invalid"):
        ng.parse_generation_response(payload, context)


def test_generation_allows_naz_primary():
    _, drafts, _ = parsed()
    assert any(item.primary_character_id == "naz" for item in drafts)


def test_generation_allows_void_primary():
    _, drafts, _ = parsed()
    assert any(item.primary_character_id == "void" for item in drafts)


def test_generation_allows_primary_only():
    _, drafts, _ = parsed("quiet_object")
    assert all(item.secondary_character_id is None for item in drafts)


def test_generation_allows_duo_without_disagreement():
    _, drafts, _ = parsed("duo_context")
    duo = next(item for item in drafts if item.secondary_character_id)
    assert "cooperative" in duo.interaction_mode
    assert "disagree" not in duo.relation_to_story


@pytest.mark.parametrize(
    "case",
    ["unknown-human-policy", "source-human-policy", "source-nonhuman-policy", "canonical-character-policy"],
    ids=lambda value: f"context-incompatible-{value}",
)
def test_generation_context_preflight_rejects_visual_policy_mismatch(case):
    _, context, payload, _ = scenario()
    visual = payload["candidates"][0]["visual_direction"]
    subject = visual["subjects"][0]
    if case == "unknown-human-policy":
        visual["human_presence_policy"] = "unknown"
    elif case == "source-human-policy":
        subject["subject_kind"] = "source_human"
    elif case == "source-nonhuman-policy":
        subject["subject_kind"] = "source_nonhuman_agent"
    else:
        subject.update({"subject_kind": "naz", "character_id": "naz", "identity_canon_refs": ["naz-visual"]})
    with pytest.raises(ng.NarrativeGenerationError, match="generation_candidate_context_incompatible") as error:
        ng.parse_generation_response(payload, context)
    assert error.value.repairable


# Adjudication cardinality and decisions


def test_adjudication_requires_every_candidate():
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    payload["candidates"].pop()
    with pytest.raises(ng.NarrativeGenerationError, match="adjudication_candidate_missing"):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


def test_adjudication_rejects_extra_candidate():
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    extra = copy.deepcopy(payload["candidates"][0])
    extra["candidate_id"] = "candidate-extra"
    payload["candidates"].append(extra)
    with pytest.raises(ng.NarrativeGenerationError, match="adjudication_candidate_extra"):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


def test_adjudication_rejects_duplicate_candidate():
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    payload["candidates"][1]["candidate_id"] = "candidate-1"
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra"])
def test_adjudication_statement_cardinality(mutation):
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    decisions = payload["candidates"][0]["statement_decisions"]
    if mutation == "missing":
        decisions.pop()
    elif mutation == "duplicate":
        decisions.append(copy.deepcopy(decisions[0]))
    else:
        decisions.append({"statement_name": "extra", "statement_digest": "0" * 64, "decision": "supported", "reason_codes": []})
    with pytest.raises(ng.NarrativeGenerationError):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


def test_primary_only_rejects_secondary_continuity():
    context, drafts, _ = parsed("quiet_object")
    _, _, _, payload = scenario("quiet_object")
    payload["candidates"][0]["secondary_continuity"] = {"character_id": "void", "interpretation_digest": "0" * 64, "decision": "supported", "reason_codes": []}
    with pytest.raises(ng.NarrativeGenerationError, match="adjudication_continuity_extra"):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


def test_duo_requires_secondary_continuity():
    context, drafts, _ = parsed("duo_context")
    _, _, _, payload = scenario("duo_context")
    duo = next(item for item in payload["candidates"] if item["secondary_continuity"])
    duo["secondary_continuity"] = None
    with pytest.raises(ng.NarrativeGenerationError, match="adjudication_continuity_missing"):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


def test_duo_requires_relationship_continuity():
    context, drafts, _ = parsed("duo_context")
    _, _, _, payload = scenario("duo_context")
    duo = next(item for item in payload["candidates"] if item["relationship_continuity"])
    duo["relationship_continuity"] = None
    with pytest.raises(ng.NarrativeGenerationError, match="adjudication_continuity_missing"):
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))


@pytest.mark.parametrize("decision_field", ["overall_decision", "visual_grounding", "primary_continuity", "statement_decisions"])
def test_unsupported_adjudication_rejects_candidate(decision_field):
    _, _, drafts_payload, adjudication = scenario()
    candidate = adjudication["candidates"][0]
    if decision_field == "overall_decision":
        candidate[decision_field] = "rejected"
        candidate["reason_codes"] = ["candidate_unsupported"]
    elif decision_field == "statement_decisions":
        candidate[decision_field][0]["decision"] = "rejected"
        candidate[decision_field][0]["reason_codes"] = ["unsupported_fact"]
    else:
        candidate[decision_field]["decision"] = "rejected"
        candidate[decision_field]["reason_codes"] = ["visual_ungrounded" if decision_field == "visual_grounding" else "continuity_conflict"]
    result, _, _, _ = run_with(drafts_payload, adjudication)
    first = next(item for item in result.candidates if item.candidate_id == "candidate-1")
    assert not first.accepted
    assert result.selected_candidate_id == "candidate-2"


def test_adjudication_statement_binding_mismatch_rejected():
    _, _, drafts_payload, adjudication = scenario()
    adjudication["candidates"][0]["statement_decisions"][0]["statement_digest"] = "0" * 64
    result, _, _, _ = run_with(drafts_payload, adjudication)
    assert "generation_adjudication_binding_invalid" in result.candidates[0].reason_codes


@pytest.mark.parametrize(
    "value",
    ["SUPPORTED", "Rejected", "unknown", "maybe", "", True, 1],
    ids=["decision-uppercase", "decision-titlecase", "decision-unknown", "decision-maybe", "decision-empty", "decision-bool", "decision-int"],
)
def test_adjudication_decision_enum_is_exact(value):
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    payload["candidates"][0]["visual_grounding"]["decision"] = value
    with pytest.raises(ng.NarrativeGenerationError) as error:
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))
    assert not error.value.repairable


def test_adjudication_rejected_requires_reason():
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    payload["candidates"][0]["visual_grounding"]["decision"] = "rejected"
    with pytest.raises(ng.NarrativeGenerationError, match="reason_missing") as error:
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))
    assert not error.value.repairable


def test_adjudication_supported_reject_reason_conflicts():
    context, drafts, _ = parsed()
    _, _, _, payload = scenario()
    payload["candidates"][0]["visual_grounding"]["reason_codes"] = ["visual_ungrounded"]
    with pytest.raises(ng.NarrativeGenerationError, match="reason_conflict") as error:
        ng.parse_adjudication_response(payload, drafts, bindings_for(context, drafts))
    assert not error.value.repairable


# Assembly, evidence, validation, selection


def test_authoritative_fields_come_from_input():
    context, drafts, _ = parsed()
    package = ng.assemble_human_story_package(drafts[0], context)
    assert package.plan_id == context.editorial_plan.plan_id
    assert package.source_ref == context.source_ref
    assert package.source_facts is context.source_facts
    assert package.character_states[0] is context.naz_state


def test_primary_only_does_not_attach_relationship_state():
    context, drafts, _ = parsed()
    package = ng.assemble_human_story_package(drafts[0], context)
    assert package.relationship_state is None
    assert package.duo_context.relationship_snapshot_ref is None


def test_duo_attaches_authoritative_relationship_state():
    context, drafts, _ = parsed("duo_context")
    draft = next(item for item in drafts if item.secondary_character_id)
    package = ng.assemble_human_story_package(draft, context)
    assert package.relationship_state is context.relationship_state
    assert package.duo_context.relationship_snapshot_ref == context.relationship_state.snapshot_ref


def test_validation_context_uses_checkpoint_one_digest_helpers(monkeypatch):
    context, drafts, adjudications = parsed()
    package = ng.assemble_human_story_package(drafts[0], context)
    called = {"statement": 0, "interpretation": 0, "visual": 0}
    for name, key in (("_statement_digest", "statement"), ("_interpretation_digest", "interpretation"), ("_visual_digest", "visual")):
        original = getattr(nt, name)
        def wrapper(*args, _original=original, _key=key, **kwargs):
            called[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(nt, name, wrapper)
    ng.build_validation_context(package, context, adjudications.candidates[0])
    assert all(value > 0 for value in called.values())


@pytest.mark.parametrize("evidence_field", ["semantic_grounding_evidence", "character_continuity_evidence", "visual_grounding_evidence"])
def test_evidence_mutation_fails_checkpoint_one_validation(evidence_field):
    context, drafts, adjudications = parsed()
    package = ng.assemble_human_story_package(drafts[0], context)
    validation = ng.build_validation_context(package, context, adjudications.candidates[0])
    if evidence_field == "semantic_grounding_evidence":
        first = dataclasses.replace(validation.semantic_grounding_evidence[0], statement_digest="0" * 64)
        validation = dataclasses.replace(validation, semantic_grounding_evidence=(first,) + validation.semantic_grounding_evidence[1:])
    elif evidence_field == "character_continuity_evidence":
        first = dataclasses.replace(validation.character_continuity_evidence[0], interpretation_digest="0" * 64)
        validation = dataclasses.replace(validation, character_continuity_evidence=(first,))
    else:
        visual = dataclasses.replace(validation.visual_grounding_evidence, visual_digest="0" * 64)
        validation = dataclasses.replace(validation, visual_grounding_evidence=visual)
    with pytest.raises(nt.HumanStoryValidationError):
        nt.validate_human_story_package(package, validation)


def test_all_three_fixture_candidates_validate():
    result, _, _, _ = run_with()
    assert len(result.accepted_candidate_ids) == 3


@pytest.mark.parametrize("name", ["env_utf8", "quiet_object", "duo_context"])
def test_each_fixture_selects_a_candidate(name):
    result, _, _, _ = run_with(name=name)
    assert result.selected_candidate_id == "candidate-1"
    assert result.model_call_count == 2


def test_selection_is_deterministic_and_idempotent():
    first, _, _, _ = run_with()
    second, _, _, _ = run_with()
    assert ng.safe_result_summary(first) == ng.safe_result_summary(second)


def test_selection_uses_rank_before_digest():
    result, _, _, _ = run_with()
    assert result.selected_candidate_id == "candidate-1"
    assert result.candidates[0].rank == 1


def test_selection_uses_digest_to_break_rank_tie():
    _, context, drafts, _ = scenario()
    drafts["candidates"][1]["rank"] = 1
    adjudication = fake_adjudication_payload(drafts, context)
    result, _, _, _ = run_with(drafts, adjudication)
    tied = [item for item in result.candidates if item.rank == 1]
    expected = min(tied, key=lambda item: item.package_digest).candidate_id
    assert result.selected_candidate_id == expected


def test_no_valid_candidate_fails_closed():
    _, _, drafts, adjudication = scenario()
    for candidate in adjudication["candidates"]:
        candidate["overall_decision"] = "rejected"
        candidate["reason_codes"] = ["candidate_unsupported"]
    result, _, _, _ = run_with(drafts, adjudication)
    assert result.selected_candidate_id is None
    assert result.reason_codes == ("narrative_generation_no_valid_candidate",)
    assert all(item.package is None for item in result.candidates)


def test_diversity_collision_rejects_colliding_candidate():
    baseline, _, context, _ = run_with()
    package = baseline.candidates[0].package
    assert package is not None
    _, _, adjudications = parsed()
    validation = ng.build_validation_context(package, context, adjudications.candidates[0])
    validated = nt.validate_human_story_package(package, validation)
    repeated_context = dataclasses.replace(context, diversity_context=nt.NarrativeDiversityContext((validated.derived_diversity_signature,)))
    data = load_fixture("env_utf8")
    drafts = fake_generation_payload(data)
    client = DeterministicFixtureClient(drafts, fake_adjudication_payload(drafts, repeated_context))
    result = ng.NarrativeGenerationService(client, generation_model="m", adjudication_model="m").generate(repeated_context)
    assert not result.candidates[0].accepted
    assert "narrative_structure_repeated" in result.candidates[0].reason_codes


# Call budget, repair rules, privacy


def test_happy_path_uses_one_generation_and_one_adjudication_call():
    result, client, _, _ = run_with()
    assert client.calls == ["generation", "adjudication"]
    assert result.model_call_count == 2


def test_one_schema_repair_is_allowed():
    data, context, drafts, adjudication = scenario()
    client = QueueClient(["not-json", drafts, adjudication])
    result = ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert result.model_call_count == 3
    assert [request.request_kind for request in client.requests] == ["generation", "repair", "adjudication"]


def test_second_repair_is_not_allowed():
    _, context, drafts, _ = scenario()
    client = QueueClient(["not-json", drafts, "not-json"])
    with pytest.raises(ng.NarrativeGenerationError):
        ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert [request.request_kind for request in client.requests] == ["generation", "repair", "adjudication"]


def test_semantic_error_is_never_repaired():
    _, context, drafts, _ = scenario()
    drafts["candidates"][0]["hook"]["source_fact_refs"] = ["invented"]
    client = QueueClient([drafts])
    with pytest.raises(ng.NarrativeGenerationError) as error:
        ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert not error.value.repairable
    assert len(client.requests) == 1


def test_diversity_error_is_never_repaired():
    _, context, drafts, _ = scenario()
    base = drafts["candidates"][0]
    for index in (1, 2):
        drafts["candidates"][index] = copy.deepcopy(base)
        drafts["candidates"][index]["candidate_id"] = f"candidate-{index + 1}"
        drafts["candidates"][index]["rank"] = index + 1
    client = QueueClient([drafts])
    with pytest.raises(ng.NarrativeGenerationError) as error:
        ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert not error.value.repairable
    assert len(client.requests) == 1


def test_safe_summary_excludes_prompts_and_raw_responses():
    result, _, context, _ = run_with()
    rendered = json.dumps(ng.safe_result_summary(result), ensure_ascii=False)
    assert context.naz_prompt_context.prompt_text not in rendered
    assert context.void_prompt_context.prompt_text not in rendered
    assert context.naz_state.last_event not in rendered


@pytest.mark.parametrize("secret", ["OPENAI_API_KEY", "api_key", "token", "prompt_text", "raw_response", "recent_events"])
def test_safe_summary_has_no_sensitive_keys(secret):
    result, _, _, _ = run_with()
    assert secret not in json.dumps(ng.safe_result_summary(result), ensure_ascii=False)


def test_service_does_not_mutate_input_snapshots():
    _, context, _, _ = scenario()
    before = dataclasses.asdict(context)
    run_with()
    assert dataclasses.asdict(context) == before


def test_run_id_is_deterministic():
    first, _, _, _ = run_with()
    second, _, _, _ = run_with()
    assert first.run_id == second.run_id


def test_model_names_are_reported_without_provider_state():
    result, _, _, _ = run_with()
    assert result.generation_model == "draft-model"
    assert result.adjudication_model == "judge-model"


def test_env_fixture_contains_only_authorized_operational_facts():
    data = load_fixture("env_utf8")
    text = " ".join(item[1] for item in data["facts"]).casefold()
    for forbidden in ("outage", "users", "clients", "damage", "urgent incident"):
        assert forbidden not in text
    assert "utf-8" in text
    assert "manual" in text


def test_fixture_payloads_do_not_copy_bible_texts():
    fixture_dir = Path(__file__).parent / "fixtures" / "narrative_generation"
    combined = " ".join(path.read_text(encoding="utf-8") for path in fixture_dir.glob("*.json")).casefold()
    assert "character_duo_bible" not in combined
    assert "canon source hash" not in combined


# Targeted binding regression coverage


@pytest.mark.parametrize(
    "mutation",
    [
        "hook-text", "human-problem", "tension", "turning-point", "resolution",
        "primary-text", "primary-metadata", "secondary-text", "interaction-mode",
        "relation-to-story", "visual-prose", "visual-subject", "visual-policy", "plan-axis",
    ],
    ids=lambda value: f"stale-adjudication-{value}",
)
def test_stale_adjudication_binding_rejected(mutation):
    name = "duo_context" if mutation in {"secondary-text", "interaction-mode", "relation-to-story"} else "env_utf8"
    _, context, payload, old_adjudication = scenario(name)
    candidate_index = 2 if name == "duo_context" else 0
    candidate = payload["candidates"][candidate_index]
    if mutation == "hook-text":
        candidate["hook"]["text"] += " Changed after adjudication."
    elif mutation in ng.STORY_FIELDS:
        candidate[mutation.replace("-", "_")]["text"] += " Changed after adjudication."
    elif mutation == "human-problem":
        candidate["human_problem"]["text"] += " Changed after adjudication."
    elif mutation == "turning-point":
        candidate["turning_point"]["text"] += " Changed after adjudication."
    elif mutation == "primary-text":
        candidate["primary_interpretation"]["text"] += " Changed after adjudication."
    elif mutation == "primary-metadata":
        candidate["primary_interpretation"]["rhetorical_form"] = "changed_form"
    elif mutation == "secondary-text":
        candidate["secondary_interpretation"]["text"] += " Changed after adjudication."
    elif mutation == "interaction-mode":
        candidate["interaction_mode"] = "changed_cooperation"
    elif mutation == "relation-to-story":
        candidate["relation_to_story"] = "a changed relationship to the same grounded material"
    elif mutation == "visual-prose":
        candidate["visual_direction"]["narrative_subject"] += "; changed after adjudication"
    elif mutation == "visual-subject":
        candidate["visual_direction"]["subjects"][0]["source_fact_refs"] = ["fact-1"]
    elif mutation == "visual-policy":
        candidate["visual_direction"]["human_presence_policy"] = "source_grounded"
    elif mutation == "plan-axis":
        plan = dataclasses.replace(context.editorial_plan, semantic_theme="changed after adjudication")
        context = dataclasses.replace(context, editorial_plan=plan)
    result, _ = run_explicit(context, payload, old_adjudication)
    rejected = next(item for item in result.candidates if item.candidate_id == candidate["candidate_id"])
    assert "generation_adjudication_binding_invalid" in rejected.reason_codes
    assert not rejected.accepted
    assert result.selected_candidate_id != rejected.candidate_id


def _replace_ref(value, old, new):
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = _replace_ref(item, old, new)
    elif isinstance(value, list):
        return [_replace_ref(item, old, new) for item in value]
    elif value == old:
        return new
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        "source-fact-text", "source-fact-id", "source-fact-order",
        "naz-state-revision", "naz-state-scalar", "void-state-revision", "void-state-scalar",
        "relationship-snapshot-ref", "relationship-revision", "relationship-mode", "relationship-scalar",
        "naz-canon-hash", "void-canon-hash", "canon-conflict-state",
        "plan-id", "source-ref", "plan-axis",
        "naz-prompt-context", "void-prompt-context", "relationship-prompt-context",
        "diversity-context", "semantic-rules-version", "character-rules-version",
        "relationship-rules-version", "visual-rules-version", "validation-contract-version",
    ],
    ids=lambda value: f"stale-authority-{value}",
)
def test_stale_authority_context_binding_rejected(monkeypatch, mutation):
    _, context, payload, old_adjudication = scenario("duo_context")
    if mutation == "source-fact-text":
        facts = list(context.source_facts)
        facts[0] = dataclasses.replace(facts[0], text=facts[0].text + " changed")
        context = dataclasses.replace(context, source_facts=tuple(facts))
    elif mutation == "source-fact-id":
        old, new = context.source_facts[0].fact_id, "fact-rekeyed"
        facts = list(context.source_facts)
        facts[0] = dataclasses.replace(facts[0], fact_id=new)
        context = dataclasses.replace(context, source_facts=tuple(facts))
        _replace_ref(payload, old, new)
    elif mutation == "source-fact-order":
        context = dataclasses.replace(context, source_facts=tuple(reversed(context.source_facts)))
    elif mutation in {"naz-state-revision", "naz-state-scalar", "void-state-revision", "void-state-scalar"}:
        character = mutation.split("-")[0]
        field = f"{character}_state"
        state = getattr(context, field)
        state = dataclasses.replace(
            state,
            **({"revision": state.revision + 1} if mutation.endswith("revision") else {"warmth": state.warmth + 1}),
        )
        context = dataclasses.replace(context, **{field: state})
    elif mutation in {
        "relationship-snapshot-ref", "relationship-revision", "relationship-mode",
        "relationship-scalar", "relationship-prompt-context",
    }:
        relation = context.relationship_state
        if mutation == "relationship-snapshot-ref":
            relation = dataclasses.replace(relation, snapshot_ref="relationship-new")
            prompt = dataclasses.replace(context.relationship_prompt_context, relationship_snapshot_ref="relationship-new")
            context = dataclasses.replace(context, relationship_state=relation, relationship_prompt_context=prompt)
        elif mutation == "relationship-revision":
            context = dataclasses.replace(context, relationship_state=dataclasses.replace(relation, revision=relation.revision + 1))
        elif mutation == "relationship-mode":
            context = dataclasses.replace(context, relationship_state=dataclasses.replace(relation, mode="changed-mode"))
        elif mutation == "relationship-scalar":
            context = dataclasses.replace(context, relationship_state=dataclasses.replace(relation, trust=relation.trust + 1))
        elif mutation == "relationship-prompt-context":
            prompt = dataclasses.replace(context.relationship_prompt_context, prompt_text=context.relationship_prompt_context.prompt_text + " changed")
            context = dataclasses.replace(context, relationship_prompt_context=prompt)
    elif mutation in {"naz-canon-hash", "void-canon-hash"}:
        character = mutation.split("-")[0]
        field = f"{character}_canon"
        canon = getattr(context, field)
        refs = list(canon.canon_refs)
        refs[0] = dataclasses.replace(refs[0], source_hash=refs[0].source_hash + "-changed")
        context = dataclasses.replace(context, **{field: dataclasses.replace(canon, canon_refs=tuple(refs))})
    elif mutation == "canon-conflict-state":
        context = dataclasses.replace(context, naz_canon=dataclasses.replace(context.naz_canon, conflict_reason_codes=("conflict",)))
    elif mutation == "plan-id":
        context = dataclasses.replace(context, editorial_plan=dataclasses.replace(context.editorial_plan, plan_id="plan-new"))
    elif mutation == "source-ref":
        plan = dataclasses.replace(context.editorial_plan, source_ref="fixture:new-source")
        context = dataclasses.replace(context, source_ref="fixture:new-source", editorial_plan=plan)
    elif mutation == "plan-axis":
        context = dataclasses.replace(context, editorial_plan=dataclasses.replace(context.editorial_plan, seriousness="changed"))
    elif mutation in {"naz-prompt-context", "void-prompt-context"}:
        character = mutation.split("-")[0]
        field = f"{character}_prompt_context"
        prompt = getattr(context, field)
        context = dataclasses.replace(context, **{field: dataclasses.replace(prompt, prompt_text=prompt.prompt_text + " changed")})
    elif mutation == "diversity-context":
        signature = nt.NarrativeDiversitySignature(
            "naz", None, "none", "a", "b", "c", "d", "e", "f", None, "artifact", "open",
        )
        context = dataclasses.replace(context, diversity_context=nt.NarrativeDiversityContext((signature,)))
    else:
        constant = {
            "semantic-rules-version": "SEMANTIC_RULES",
            "character-rules-version": "CHARACTER_RULES",
            "relationship-rules-version": "RELATIONSHIP_RULES",
            "visual-rules-version": "VISUAL_RULES",
        }.get(mutation)
        if constant is not None:
            monkeypatch.setattr(ng, constant, getattr(ng, constant) + "-changed")
        else:
            monkeypatch.setattr(nt, "VALIDATION_CONTRACT_VERSION", nt.VALIDATION_CONTRACT_VERSION + "-changed")
    result, _ = run_explicit(context, payload, old_adjudication)
    for candidate in result.candidates:
        assert "generation_adjudication_binding_invalid" in candidate.reason_codes
        assert not candidate.accepted
    assert result.selected_candidate_id is None


def test_adjudication_authority_context_echo_mismatch_rejected():
    _, _, payload, adjudication = scenario()
    adjudication["candidates"][0]["authority_context_digest"] = "0" * 64
    result, _, _, _ = run_with(payload, adjudication)
    assert "generation_adjudication_binding_invalid" in result.candidates[0].reason_codes
    assert not result.candidates[0].accepted


def _validator_spy(monkeypatch, *, failure_on_call=None):
    original = ng.contract.validate_human_story_package
    calls = []
    def spy(package, validation_context):
        calls.append(package)
        if failure_on_call == len(calls):
            raise nt.HumanStoryValidationError(("human_story_schema_invalid",))
        return original(package, validation_context)
    monkeypatch.setattr(ng.contract, "validate_human_story_package", spy)
    return calls


def test_validator_called_three_times_happy_path(monkeypatch):
    calls = _validator_spy(monkeypatch)
    result, _, _, _ = run_with()
    assert len(calls) == 3
    assert len(result.accepted_candidate_ids) == 3


def test_validator_called_for_adjudication_rejected_candidate(monkeypatch):
    _, _, payload, adjudication = scenario()
    adjudication["candidates"][0]["overall_decision"] = "rejected"
    adjudication["candidates"][0]["reason_codes"] = ["candidate_unsupported"]
    calls = _validator_spy(monkeypatch)
    result, _, _, _ = run_with(payload, adjudication)
    assert len(calls) == 3
    assert not result.candidates[0].accepted


def test_validator_called_for_editorial_rejected_candidate(monkeypatch):
    _, _, payload, adjudication = scenario()
    adjudication["candidates"][0]["editorial_alignment"]["decision"] = "rejected"
    adjudication["candidates"][0]["editorial_alignment"]["reason_codes"] = ["candidate_unsupported"]
    calls = _validator_spy(monkeypatch)
    result, _, _, _ = run_with(payload, adjudication)
    assert len(calls) == 3
    assert "generation_editorial_alignment_invalid" in result.candidates[0].reason_codes


def test_validator_called_for_binding_invalid_candidate(monkeypatch):
    _, _, payload, adjudication = scenario()
    adjudication["candidates"][0]["authority_context_digest"] = "0" * 64
    calls = _validator_spy(monkeypatch)
    result, _, _, _ = run_with(payload, adjudication)
    assert len(calls) == 3
    assert not result.candidates[0].accepted


def test_validator_error_prevents_selection(monkeypatch):
    calls = _validator_spy(monkeypatch, failure_on_call=1)
    result, _, _, _ = run_with()
    assert len(calls) == 3
    assert not result.candidates[0].accepted
    assert result.selected_candidate_id == "candidate-2"


def _relationship_incompatible_batch():
    _, context, payload, _ = scenario()
    context = dataclasses.replace(context, relationship_state=None, relationship_prompt_context=None)
    repaired = copy.deepcopy(payload)
    third = repaired["candidates"][2]
    third["secondary_character_id"] = None
    third["presence_mode"] = "none"
    third["secondary_interpretation"] = None
    third["interaction_mode"] = None
    third["relation_to_story"] = None
    adjudication = fake_adjudication_payload(repaired, context)
    return context, payload, repaired, adjudication


def test_context_incompatible_relationship_repaired(monkeypatch):
    context, incompatible, repaired, adjudication = _relationship_incompatible_batch()
    client = QueueClient([incompatible, repaired, adjudication])
    calls = _validator_spy(monkeypatch)
    result = ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert [request.request_kind for request in client.requests] == ["generation", "repair", "adjudication"]
    assert len(calls) == 3
    assert len(result.accepted_candidate_ids) == 3


def test_context_incompatible_relationship_repair_fails_closed(monkeypatch):
    context, incompatible, _, _ = _relationship_incompatible_batch()
    client = QueueClient([incompatible, incompatible])
    calls = _validator_spy(monkeypatch)
    with pytest.raises(ng.NarrativeGenerationError, match="generation_candidate_context_incompatible"):
        ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert [request.request_kind for request in client.requests] == ["generation", "repair"]
    assert calls == []


def test_context_incompatible_batch_has_no_partial_validation(monkeypatch):
    context, incompatible, _, _ = _relationship_incompatible_batch()
    client = QueueClient([incompatible, incompatible])
    calls = _validator_spy(monkeypatch)
    with pytest.raises(ng.NarrativeGenerationError):
        ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a").generate(context)
    assert calls == []
    assert all(request.request_kind != "adjudication" for request in client.requests)


def _assembly_failure_setup(monkeypatch, failure):
    _, context, payload, _ = scenario()
    client = QueueClient([payload])
    original = ng.assemble_human_story_package
    assembly_calls = []
    def fail_third(draft, generation_input):
        assembly_calls.append(draft.candidate_id)
        if len(assembly_calls) == 3:
            raise failure
        return original(draft, generation_input)
    monkeypatch.setattr(ng, "assemble_human_story_package", fail_third)
    validator_calls = _validator_spy(monkeypatch)
    service = ng.NarrativeGenerationService(client, generation_model="g", adjudication_model="a")
    return service, context, client, assembly_calls, validator_calls


@pytest.mark.parametrize(
    ("exception_type", "sensitive_message"),
    [
        (RuntimeError, "sensitive-runtime-assembly-detail"),
        (ValueError, "sensitive-value-assembly-detail"),
        (TypeError, "sensitive-type-assembly-detail"),
    ],
    ids=["assembly-unexpected-runtime-error", "assembly-unexpected-value-error", "assembly-unexpected-type-error"],
)
def test_unexpected_assembly_exception_is_safely_normalized(
    monkeypatch,
    caplog,
    capsys,
    exception_type,
    sensitive_message,
):
    service, context, client, assembly_calls, validator_calls = _assembly_failure_setup(
        monkeypatch,
        exception_type(sensitive_message),
    )
    no_result = object()
    result = no_result
    with pytest.raises(ng.NarrativeGenerationError) as captured:
        result = service.generate(context)
    error = captured.value
    captured_output = capsys.readouterr()
    formatted_traceback = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    public_surfaces = (
        str(error),
        repr(error),
        repr(error.args),
        repr(vars(error)),
        " ".join(error.reason_codes),
        formatted_traceback,
        caplog.text,
        captured_output.out,
        captured_output.err,
    )
    assert type(error) is ng.NarrativeGenerationError
    assert error.reason_codes == ("narrative_generation_internal_assembly_error",)
    assert str(error) == "narrative_generation_internal_assembly_error"
    assert error.repairable is False
    assert error.__cause__ is None
    assert error.__context__ is None
    assert all(sensitive_message not in surface for surface in public_surfaces)
    assert result is no_result
    assert assembly_calls == ["candidate-1", "candidate-2", "candidate-3"]
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


def test_assembly_domain_error_preserved(monkeypatch):
    domain_error = ng.NarrativeGenerationError("relationship_state_unavailable", repairable=True)
    service, context, client, _, validator_calls = _assembly_failure_setup(monkeypatch, domain_error)
    with pytest.raises(ng.NarrativeGenerationError) as captured:
        service.generate(context)
    assert captured.value is domain_error
    assert captured.value.reason_codes == ("relationship_state_unavailable",)
    assert captured.value.repairable
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


def test_assembly_error_repairable_false(monkeypatch):
    service, context, _, _, _ = _assembly_failure_setup(monkeypatch, RuntimeError("internal"))
    with pytest.raises(ng.NarrativeGenerationError) as captured:
        service.generate(context)
    assert captured.value.repairable is False


def test_assembly_error_original_message_hidden(monkeypatch):
    secret = "plan_id=private source_ref=private event_id=private"
    service, context, _, _, _ = _assembly_failure_setup(monkeypatch, RuntimeError(secret))
    with pytest.raises(ng.NarrativeGenerationError) as captured:
        service.generate(context)
    public_surface = " ".join((str(captured.value), repr(captured.value), *captured.value.reason_codes))
    assert secret not in public_surface
    assert "private" not in public_surface


def test_assembly_error_no_public_cause(monkeypatch):
    service, context, _, _, _ = _assembly_failure_setup(monkeypatch, RuntimeError("private"))
    with pytest.raises(ng.NarrativeGenerationError) as captured:
        service.generate(context)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_assembly_error_no_partial_result(monkeypatch):
    service, context, _, assembly_calls, _ = _assembly_failure_setup(monkeypatch, RuntimeError("private"))
    no_result = object()
    result = no_result
    with pytest.raises(ng.NarrativeGenerationError):
        result = service.generate(context)
    assert result is no_result
    assert assembly_calls == ["candidate-1", "candidate-2", "candidate-3"]


def test_assembly_error_no_extra_model_call(monkeypatch):
    service, context, client, _, validator_calls = _assembly_failure_setup(monkeypatch, RuntimeError("private"))
    with pytest.raises(ng.NarrativeGenerationError):
        service.generate(context)
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


def test_assembly_keyboard_interrupt_not_swallowed(monkeypatch):
    interruption = KeyboardInterrupt()
    service, context, client, _, validator_calls = _assembly_failure_setup(monkeypatch, interruption)
    with pytest.raises(KeyboardInterrupt) as captured:
        service.generate(context)
    assert captured.value is interruption
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


def test_assembly_system_exit_not_swallowed(monkeypatch):
    termination = SystemExit(17)
    service, context, client, _, validator_calls = _assembly_failure_setup(monkeypatch, termination)
    with pytest.raises(SystemExit) as captured:
        service.generate(context)
    assert captured.value is termination
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


def test_assembly_generator_exit_not_swallowed(monkeypatch):
    termination = GeneratorExit()
    service, context, client, _, validator_calls = _assembly_failure_setup(monkeypatch, termination)
    with pytest.raises(GeneratorExit) as captured:
        service.generate(context)
    assert captured.value is termination
    assert [request.request_kind for request in client.requests] == ["generation"]
    assert validator_calls == []


# Editorial alignment


@pytest.mark.parametrize(
    ("field", "value"),
    [("visual_mode", "artifact"), ("ending", "closed_answer"), ("seriousness", "severe"), ("semantic_theme", "changed theme")],
    ids=["editorial-plan-axis-visual", "editorial-plan-axis-ending", "editorial-plan-axis-seriousness", "editorial-plan-axis-theme"],
)
def test_editorial_plan_axis_changes_request(field, value):
    _, context, _, _ = scenario()
    baseline = ng.build_generation_request(context, "m").user_prompt
    changed = dataclasses.replace(context, editorial_plan=dataclasses.replace(context.editorial_plan, **{field: value}))
    assert ng.build_generation_request(changed, "m").user_prompt != baseline


def test_editorial_visual_mode_mismatch():
    _, context, payload, _ = scenario()
    payload["candidates"][0]["visual_direction"]["mode_hint"] = "artifact"
    adjudication = fake_adjudication_payload(payload, context)
    result, _ = run_explicit(context, payload, adjudication)
    assert "generation_editorial_locked_axis_mismatch" in result.candidates[0].reason_codes


def test_editorial_ending_mismatch():
    _, context, payload, _ = scenario()
    payload["candidates"][0]["primary_interpretation"]["ending_mode"] = "closed_answer"
    adjudication = fake_adjudication_payload(payload, context)
    result, _ = run_explicit(context, payload, adjudication)
    assert "generation_editorial_locked_axis_mismatch" in result.candidates[0].reason_codes


def test_editorial_alignment_stale_binding():
    _, context, payload, adjudication = scenario()
    changed = dataclasses.replace(context, editorial_plan=dataclasses.replace(context.editorial_plan, seriousness="changed"))
    result, _ = run_explicit(changed, payload, adjudication)
    assert "generation_adjudication_binding_invalid" in result.candidates[0].reason_codes


def test_editorial_supported_valid():
    result, _, _, _ = run_with()
    assert len(result.accepted_candidate_ids) == 3


def test_editorial_rejected_not_selected():
    _, _, payload, adjudication = scenario()
    decision = adjudication["candidates"][0]["editorial_alignment"]
    decision["decision"] = "rejected"
    decision["reason_codes"] = ["candidate_unsupported"]
    result, _, _, _ = run_with(payload, adjudication)
    assert not result.candidates[0].accepted
    assert result.selected_candidate_id == "candidate-2"


# Substantive diversity


def _same_text_payload(case):
    _, context, payload, _ = scenario("quiet_object")
    same_story = "The same concrete narrative sentence remains unchanged across every candidate in this controlled near duplicate probe."
    same_interpretation = "The same grounded interpretation remains unchanged across every candidate and supplies no distinct narrative perspective."
    for candidate in payload["candidates"]:
        for field in ng.STORY_FIELDS:
            candidate[field]["text"] = same_story
        candidate["primary_interpretation"]["text"] = same_interpretation
    if case == "visual-mode":
        for candidate, mode in zip(payload["candidates"], ("cinematic", "documentary", "artifact")):
            candidate["visual_direction"]["mode_hint"] = mode
    elif case == "emotional-label":
        for candidate, label in zip(payload["candidates"], ("warm", "cool", "neutral")):
            candidate["primary_interpretation"]["emotional_register"] = label
    elif case == "presence":
        duo = payload["candidates"][2]
        duo["secondary_character_id"] = "void"
        duo["presence_mode"] = "explicit"
        duo["secondary_interpretation"] = copy.deepcopy(payload["candidates"][1]["primary_interpretation"])
        duo["secondary_interpretation"]["text"] = "same"
        duo["interaction_mode"] = "parallel"
        duo["relation_to_story"] = "parallel"
    elif case == "punctuation":
        for index, candidate in enumerate(payload["candidates"]):
            for field in ng.STORY_FIELDS:
                candidate[field]["text"] = ("  " + same_story.upper() + ("!!!" if index else "."))
    return context, payload


@pytest.mark.parametrize(
    "case",
    ["character", "presence", "visual-mode", "emotional-label", "punctuation"],
    ids=[
        "identical-story-different-character-rejected",
        "identical-story-different-presence-rejected",
        "identical-story-different-visual-mode-rejected",
        "identical-story-different-emotional-label-rejected",
        "case-whitespace-punctuation-near-duplicate-rejected",
    ],
)
def test_substantive_duplicate_rejected(case):
    context, payload = _same_text_payload(case)
    with pytest.raises(ng.NarrativeGenerationError, match="generation_candidates_not_substantively_diverse") as error:
        ng.parse_generation_response(payload, context)
    assert not error.value.repairable


def test_substantively_different_stories_accepted():
    _, context, payload, _ = scenario()
    assert len(ng.parse_generation_response(payload, context)) == 3


# Live guards


def _cli_args(*, live=False, show_content=False, output=None):
    return argparse.Namespace(fixture="env_utf8", live=live, show_content=show_content, output=output, overwrite=False, model="m")


class _ForbiddenProvider:
    def __init__(self, *args, **kwargs):
        raise AssertionError("provider must not be created")


def test_live_guard_dry_no_env(monkeypatch):
    monkeypatch.delenv("NARRATIVE_TRANSLATOR_LIVE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result, _ = fixture_cli.run(_cli_args(), provider_factory=_ForbiddenProvider)
    assert result.model_call_count == 2


def test_live_guard_dry_with_env(monkeypatch):
    monkeypatch.setenv("NARRATIVE_TRANSLATOR_LIVE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    result, _ = fixture_cli.run(_cli_args(), provider_factory=_ForbiddenProvider)
    assert result.model_call_count == 2


def test_live_guard_live_no_env(monkeypatch):
    monkeypatch.delenv("NARRATIVE_TRANSLATOR_LIVE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result, report = fixture_cli.run(_cli_args(live=True), provider_factory=_ForbiddenProvider)
    assert result is None and report["live_smoke"] == "not_run"


@pytest.mark.parametrize("value", ["true", "yes", "on", "0"], ids=["live-true-rejected", "live-yes-rejected", "live-on-rejected", "live-zero-rejected"])
def test_live_guard_unknown_truthy_rejected(monkeypatch, value):
    monkeypatch.setenv("NARRATIVE_TRANSLATOR_LIVE", value)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    result, report = fixture_cli.run(_cli_args(live=True), provider_factory=_ForbiddenProvider)
    assert result is None and report["live_smoke"] == "not_run"


def test_live_guard_live_no_credentials(monkeypatch):
    monkeypatch.setenv("NARRATIVE_TRANSLATOR_LIVE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result, report = fixture_cli.run(_cli_args(live=True), provider_factory=_ForbiddenProvider)
    assert result is None and report["reason"] == "local_credentials_unavailable"


def test_live_guard_import_no_provider(monkeypatch):
    imported = []
    original = builtins.__import__
    def spy(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            imported.append(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", spy)
    importlib.reload(fixture_cli)
    assert imported == []


def test_live_guard_help_no_provider(monkeypatch):
    monkeypatch.setattr(fixture_cli, "LiveOpenAIClient", _ForbiddenProvider)
    monkeypatch.setattr(sys, "argv", ["fixture", "--help"])
    with pytest.raises(SystemExit) as error:
        fixture_cli.main()
    assert error.value.code == 0


def test_live_guard_exact_one_with_credentials(monkeypatch):
    monkeypatch.setenv("NARRATIVE_TRANSLATOR_LIVE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    data = load_fixture("env_utf8")
    context = fixture_to_input(data)
    payload = fake_generation_payload(data)
    adjudication = fake_adjudication_payload(payload, context)
    created = []
    def factory(api_key, base_url):
        created.append((bool(api_key), base_url))
        return DeterministicFixtureClient(payload, adjudication)
    result, report = fixture_cli.run(_cli_args(live=True), provider_factory=factory)
    assert created == [(True, None)]
    assert result.model_call_count == 2
    assert "dummy" not in json.dumps(report)


# Output policy


def test_output_no_flag_no_file(tmp_path):
    fixture_cli.run(_cli_args())
    assert list(tmp_path.iterdir()) == []


def test_output_new_file_created(tmp_path):
    target = tmp_path / "result.json"
    fixture_cli.write_output(str(target), '{"safe":true}', overwrite=False)
    assert json.loads(target.read_text(encoding="utf-8")) == {"safe": True}


def test_output_existing_file_rejected(tmp_path):
    target = tmp_path / "result.json"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(fixture_cli.OutputPolicyError, match="output_exists"):
        fixture_cli.write_output(str(target), "new", overwrite=False)
    assert target.read_text(encoding="utf-8") == "old"


def test_output_overwrite_explicit(tmp_path):
    target = tmp_path / "result.json"
    target.write_text("old", encoding="utf-8")
    fixture_cli.write_output(str(target), '{"new":true}', overwrite=True)
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_output_overwrite_without_output_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["fixture", "--overwrite"])
    with pytest.raises(SystemExit) as error:
        fixture_cli.main()
    assert error.value.code == 2


def test_output_missing_parent_rejected(tmp_path):
    target = tmp_path / "missing" / "result.json"
    with pytest.raises(fixture_cli.OutputPolicyError, match="output_parent_missing"):
        fixture_cli.write_output(str(target), "safe", overwrite=False)
    assert not target.parent.exists()


def test_output_no_clobber_concurrent_create(monkeypatch, tmp_path):
    target = tmp_path / "result.json"
    original_open = fixture_cli.os.open
    def concurrent_open(path, flags, mode):
        Path(path).write_text("concurrent", encoding="utf-8")
        return original_open(path, flags, mode)
    monkeypatch.setattr(fixture_cli.os, "open", concurrent_open)
    with pytest.raises(fixture_cli.OutputPolicyError, match="output_exists"):
        fixture_cli.write_output(str(target), "new", overwrite=False)
    assert target.read_text(encoding="utf-8") == "concurrent"


def test_output_no_clobber_fileexists_race(monkeypatch, tmp_path):
    target = tmp_path / "result.json"
    def file_exists(*args, **kwargs):
        raise FileExistsError("race")
    monkeypatch.setattr(fixture_cli.os, "open", file_exists)
    with pytest.raises(fixture_cli.OutputPolicyError, match="output_exists"):
        fixture_cli.write_output(str(target), "new", overwrite=False)
    assert not target.exists()


def test_output_no_clobber_write_failure_cleans_partial(monkeypatch, tmp_path):
    target = tmp_path / "result.json"
    def fail_fsync(*args, **kwargs):
        raise OSError("forced write failure")
    monkeypatch.setattr(fixture_cli.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="forced write failure"):
        fixture_cli.write_output(str(target), "partial", overwrite=False)
    assert not target.exists()


def test_output_overwrite_still_uses_atomic_replace(monkeypatch, tmp_path):
    target = tmp_path / "result.json"
    target.write_text("old", encoding="utf-8")
    original_replace = fixture_cli.os.replace
    calls = []
    def spy(source, destination):
        calls.append((Path(source), Path(destination)))
        return original_replace(source, destination)
    monkeypatch.setattr(fixture_cli.os, "replace", spy)
    fixture_cli.write_output(str(target), "new", overwrite=True)
    assert len(calls) == 1 and calls[0][1] == target
    assert target.read_text(encoding="utf-8").strip() == "new"


def test_output_default_excludes_content():
    _, report = fixture_cli.run(_cli_args())
    assert "selected_content" not in report


def test_output_show_content_includes_selected_only():
    result, report = fixture_cli.run(_cli_args(show_content=True))
    assert set(report["selected_content"]) == set(ng.STORY_FIELDS)
    assert result.selected_candidate_id == "candidate-1"
    assert "candidate-2" not in json.dumps(report["selected_content"])


@pytest.mark.parametrize("forbidden", ["raw_response", "system_prompt", "OPENAI_API_KEY"], ids=["output-excludes-raw-response", "output-excludes-prompt", "output-excludes-credentials"])
def test_output_excludes_sensitive_material(monkeypatch, forbidden):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-secret")
    _, report = fixture_cli.run(_cli_args())
    rendered = json.dumps(report, ensure_ascii=False)
    assert forbidden not in rendered
    assert "dummy-secret" not in rendered


# Run identity


def test_run_id_changes_with_state_scalar():
    _, context, _, _ = scenario()
    changed = dataclasses.replace(context, naz_state=dataclasses.replace(context.naz_state, warmth=context.naz_state.warmth + 1))
    assert ng._input_run_id(context, "g", "a") != ng._input_run_id(changed, "g", "a")


def test_run_id_changes_with_character_prompt_text():
    _, context, _, _ = scenario()
    prompt = dataclasses.replace(context.naz_prompt_context, prompt_text=context.naz_prompt_context.prompt_text + " changed")
    changed = dataclasses.replace(context, naz_prompt_context=prompt)
    assert ng._input_run_id(context, "g", "a") != ng._input_run_id(changed, "g", "a")


def test_run_id_changes_with_repair_model():
    _, context, _, _ = scenario()
    assert ng._input_run_id(context, "g", "a", "repair-a") != ng._input_run_id(context, "g", "a", "repair-b")
    assert ng._input_run_id(context, "g", "a", None) != ng._input_run_id(context, "g", "a", "g")


def test_run_id_changes_with_validation_contract_version(monkeypatch):
    _, context, _, _ = scenario()
    baseline = ng._input_run_id(context, "g", "a")
    monkeypatch.setattr(nt, "VALIDATION_CONTRACT_VERSION", nt.VALIDATION_CONTRACT_VERSION + "-changed")
    assert ng._input_run_id(context, "g", "a") != baseline


def test_run_id_changes_with_generation_schema_version(monkeypatch):
    _, context, _, _ = scenario()
    baseline = ng._input_run_id(context, "g", "a")
    monkeypatch.setattr(ng, "GENERATION_SCHEMA", ng.GENERATION_SCHEMA + "-changed")
    assert ng._input_run_id(context, "g", "a") != baseline


def test_run_id_changes_with_adjudication_schema_version(monkeypatch):
    _, context, _, _ = scenario()
    baseline = ng._input_run_id(context, "g", "a")
    monkeypatch.setattr(ng, "ADJUDICATION_SCHEMA", ng.ADJUDICATION_SCHEMA + "-changed")
    assert ng._input_run_id(context, "g", "a") != baseline


def test_run_id_changes_with_authority_binding_version(monkeypatch):
    _, context, _, _ = scenario()
    baseline = ng._input_run_id(context, "g", "a")
    monkeypatch.setattr(ng, "AUTHORITY_CONTEXT_BINDING_VERSION", ng.AUTHORITY_CONTEXT_BINDING_VERSION + "-changed")
    assert ng._input_run_id(context, "g", "a") != baseline


@pytest.mark.parametrize(
    "constant",
    ["REPAIR_SCHEMA_VERSION", "REPAIR_RULES_VERSION"],
    ids=["run-id-repair-schema-version-change", "run-id-repair-rules-version-change"],
)
def test_run_id_changes_with_repair_contract(monkeypatch, constant):
    _, context, _, _ = scenario()
    baseline = ng._input_run_id(context, "g", "a")
    monkeypatch.setattr(ng, constant, getattr(ng, constant) + "-changed")
    assert ng._input_run_id(context, "g", "a") != baseline


def test_run_id_stable_between_processes():
    code = (
        "import narrative_generation as n; "
        "from tools.run_narrative_generation_fixture import load_fixture,fixture_to_input; "
        "c=fixture_to_input(load_fixture('env_utf8')); "
        "print(n._input_run_id(c,'g','a'))"
    )
    first = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1], check=True, capture_output=True, text=True).stdout.strip()
    second = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1], check=True, capture_output=True, text=True).stdout.strip()
    assert first == second
