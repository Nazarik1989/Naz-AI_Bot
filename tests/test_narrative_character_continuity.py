from __future__ import annotations
from dataclasses import replace
import pytest
import narrative_translator as nt
from tests.test_narrative_translator_contract import context,codes,package

@pytest.mark.parametrize("mode",["none","implicit","explicit"],ids=["primary-only","implicit","explicit"])
def test_character_modes(mode):
    v=package(mode=mode); assert nt.validate_human_story_package(v,context(v)).package is v
def test_void_primary_and_agreement():
    v=package("void","explicit"); second=replace(v.secondary_interpretation,text="Naz sees the same result as a modest pause and stays nearby without argument."); v=replace(v,secondary_interpretation=second); assert nt.validate_human_story_package(v,context(v))
@pytest.mark.parametrize("text",["Naz.","Naz calm.","VOID warm."],ids=["name-only","short-naz","short-void"])
def test_short_text(text):
    v=package(); v=replace(v,primary_interpretation=replace(v.primary_interpretation,text=text)); assert "character_interpretation_too_short" in codes(v,context(v))
@pytest.mark.parametrize("case",["duplicate-state","extra-authority","wrong-revision","duplicate-canon","personality-missing","visual-is-not-personality","relationship-missing"],ids=["duplicate-state","extra-authority","wrong-revision","duplicate-canon","personality-missing","visual-not-personality","relationship-missing"])
def test_state_and_canon_cardinality(case):
    v=package("naz","explicit" if case=="relationship-missing" else "none"); c=context(v)
    if case=="duplicate-state": v=replace(v,character_states=(v.character_states[0],v.character_states[0]))
    elif case=="extra-authority": c=replace(c,character_snapshot_authorities=(*c.character_snapshot_authorities,nt.CharacterSnapshotAuthority("void","v",1,"void-v1")))
    elif case=="wrong-revision": c=replace(c,character_snapshot_authorities=(replace(c.character_snapshot_authorities[0],expected_revision=2),))
    elif case=="duplicate-canon": v=replace(v,character_canons=(v.character_canons[0],v.character_canons[0]))
    elif case=="personality-missing": v=replace(v,character_canons=(nt.CharacterCanonSnapshot("naz",[v.character_canons[0].canon_refs[1]],"c"),))
    elif case=="visual-is-not-personality": v=replace(v,primary_interpretation=replace(v.primary_interpretation,canon_refs=("naz-visual",)))
    else: v=replace(v,character_canons=tuple(replace(x,canon_refs=x.canon_refs[:2]) for x in v.character_canons))
    assert codes(v,context(v) if case not in {"extra-authority","wrong-revision"} else c)

@pytest.mark.parametrize(
    "variant",
    ["interaction","relation","primary-digest","secondary-digest","primary-swap","revision","rules","plan-source"],
    ids=[
        "relationship-evidence-interaction-mode","relationship-evidence-relation-to-story",
        "relationship-evidence-primary-digest","relationship-evidence-secondary-digest",
        "relationship-evidence-primary-swap","relationship-evidence-revision",
        "relationship-evidence-rules-version","relationship-evidence-plan-source",
    ],
)
def test_relationship_continuity_evidence_complete_binding(variant):
    original=package(mode="explicit"); original_context=context(original); old_evidence=original_context.relationship_continuity_evidence
    changed=original
    if variant=="interaction": changed=replace(original,duo_context=replace(original.duo_context,interaction_mode="disagreement"))
    elif variant=="relation": changed=replace(original,duo_context=replace(original.duo_context,relation_to_story="counterpoint"))
    elif variant=="primary-digest": changed=replace(original,primary_interpretation=replace(original.primary_interpretation,emotional_register="careful"))
    elif variant=="secondary-digest": changed=replace(original,secondary_interpretation=replace(original.secondary_interpretation,rhetorical_form="memo"))
    elif variant=="primary-swap": changed=replace(original,primary_interpretation=original.secondary_interpretation,secondary_interpretation=original.primary_interpretation)
    elif variant=="revision": changed=replace(original,relationship_state=replace(original.relationship_state,revision=2))
    elif variant=="plan-source":
        changed=replace(original,source_ref="source-v2")
        fresh=context(changed,replace(nt.EditorialPlanBinding("plan","source-v2","story_first","story_pack")))
        assert codes(changed,replace(fresh,relationship_continuity_evidence=old_evidence)) == ("relationship_continuity_evidence_invalid",)
        return
    fresh=context(changed)
    if variant=="rules": fresh=replace(fresh,authority_policy=replace(fresh.authority_policy,relationship_rules_version="rel-v2"))
    assert codes(changed,replace(fresh,relationship_continuity_evidence=old_evidence)) == ("relationship_continuity_evidence_invalid",)

def test_relationship_continuity_payload_has_complete_versioned_shape():
    v=package(mode="explicit")
    payload=nt.relationship_continuity_payload(contract_version=nt.VALIDATION_CONTRACT_VERSION,plan_id="plan",source_ref="source",duo_context=v.duo_context,primary_interpretation=v.primary_interpretation,secondary_interpretation=v.secondary_interpretation,relationship_snapshot=v.relationship_state,rules_version="rel-v1")
    assert tuple(payload) == (
        "payload_version","validation_contract_version","plan_id","source_ref","presence_mode","interaction_mode",
        "relation_to_story","source_fact_refs","primary_character_id","secondary_character_id",
        "primary_interpretation_digest","secondary_interpretation_digest","relationship_snapshot_ref",
        "relationship_revision","relationship_version","authority_rules_version",
    )
    assert payload["primary_interpretation_digest"] == nt.character_interpretation_digest(v.primary_interpretation)
    assert payload["secondary_interpretation_digest"] == nt.character_interpretation_digest(v.secondary_interpretation)

@pytest.mark.parametrize(
    "variant",
    ["different-hash","different-kind","different-version","different-path"],
    ids=["canon-duplicate-source-id-different-hash","canon-duplicate-source-id-different-kind","canon-duplicate-source-id-different-version","canon-duplicate-source-id-different-path"],
)
def test_canon_source_id_is_unique_within_snapshot(variant):
    v=package(); snapshot=v.character_canons[0]; original=snapshot.canon_refs[0]
    changes={
        "different-hash":{"source_hash":"f"*64},
        "different-kind":{"kind":"relationship"},
        "different-version":{"source_version":"v2"},
        "different-path":{"source_path":"C:/other/canon.md"},
    }
    duplicate=replace(original,**changes[variant])
    changed=replace(v,character_canons=(replace(snapshot,canon_refs=(*snapshot.canon_refs,duplicate)),))
    assert codes(changed,context(changed)) == ("canon_source_id_duplicate",)

def test_canon_compatible_different_source_ids():
    v=package(); snapshot=v.character_canons[0]
    extra=replace(snapshot.canon_refs[0],source_id="naz-personality-secondary",source_path="C:/private/naz-secondary.md",source_hash="d"*64)
    changed=replace(v,character_canons=(replace(snapshot,canon_refs=(*snapshot.canon_refs,extra)),))
    assert nt.validate_human_story_package(changed,context(changed)).package is changed

def test_canon_explicit_conflict_rejected():
    v=package(); changed=replace(v,character_canons=(replace(v.character_canons[0],conflict_reason_codes=("canon-disagrees",)),))
    assert codes(changed,context(changed)) == ("character_canon_conflict",)

def test_canon_visual_cannot_replace_personality():
    v=package(); changed=replace(v,primary_interpretation=replace(v.primary_interpretation,canon_refs=("naz-visual",)))
    assert codes(changed,context(changed)) == ("character_personality_canon_missing",)

@pytest.mark.parametrize("character",["naz","void"],ids=["canon-visual-required-for-naz","canon-visual-required-for-void"])
def test_canonical_character_visual_requires_owned_visual_canon(character):
    v=package(primary=character)
    valid_visual=replace(v.visual_direction,human_presence_policy="canonical_only",subjects=(nt.VisualSubjectRef(character,character,(),(f"{character}-visual",)),))
    valid=replace(v,visual_direction=valid_visual)
    assert nt.validate_human_story_package(valid,context(valid)).package is valid
    personality_only=replace(valid_visual,visual_canon_refs=(f"{character}-personality",),subjects=(nt.VisualSubjectRef(character,character,(),(f"{character}-personality",)),))
    changed=replace(v,visual_direction=personality_only)
    assert codes(changed,context(changed)) == ("visual_canon_missing",)

@pytest.mark.parametrize("mode",["implicit","explicit"],ids=["canon-relationship-required-for-implicit","canon-relationship-required-for-explicit"])
def test_relationship_mode_requires_relationship_canon_for_each_character(mode):
    v=package(mode=mode)
    changed=replace(v,character_canons=tuple(replace(snapshot,canon_refs=tuple(ref for ref in snapshot.canon_refs if ref.kind != "relationship")) for snapshot in v.character_canons))
    assert codes(changed,context(changed)) == ("character_relationship_canon_missing",)
