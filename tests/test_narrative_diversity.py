from __future__ import annotations
from dataclasses import replace
import pytest
import narrative_translator as nt
from tests.test_narrative_translator_contract import context,codes,package

@pytest.mark.parametrize("case",["generic-human","generic-robot","source-human","source-agent","motif-conflict","motif-duplicate","subject-duplicate","subject-ref-duplicate"],ids=["generic-human","generic-robot","source-human","source-nonhuman-agent-policy-none","motif-conflict","motif-duplicate","subject-duplicate","subject-ref-duplicate"])
def test_visual_cases(case):
    v=package(); visual=v.visual_direction
    if case=="generic-human": visual=replace(visual,subjects=(nt.VisualSubjectRef("generic_human",None,[],[]),))
    elif case=="generic-robot": visual=replace(visual,subjects=(nt.VisualSubjectRef("generic_robot",None,[],[]),))
    elif case=="source-human": visual=replace(visual,human_presence_policy="source_grounded",subjects=(nt.VisualSubjectRef("source_human",None,["fact-1"],[]),))
    elif case=="source-agent": visual=replace(visual,subjects=(nt.VisualSubjectRef("source_nonhuman_agent",None,["fact-1"],[]),))
    elif case=="motif-conflict": visual=replace(visual,excluded_motifs=("EDGE",))
    elif case=="motif-duplicate": visual=replace(visual,approved_motifs=("edge"," EDGE "))
    elif case=="subject-duplicate": x=nt.VisualSubjectRef("object",None,[],[]); visual=replace(visual,subjects=(x,x))
    else: visual=replace(visual,human_presence_policy="source_grounded",subjects=(nt.VisualSubjectRef("source_human",None,["fact-1","fact-1"],[]),))
    v=replace(v,visual_direction=visual)
    if case=="source-human": assert nt.validate_human_story_package(v,context(v))
    elif case=="source-agent": assert codes(v,context(v)) == ("source_nonhuman_agent_policy_required",)
    else: assert codes(v,context(v))

def _visual_package(*,policy,subject):
    v=package()
    visual=replace(v.visual_direction,nonhuman_presence_policy=policy,subjects=(subject,))
    return replace(v,visual_direction=visual)

@pytest.mark.parametrize("_case",[pytest.param(None,id="nonhuman-policy-valid-source-grounded")])
def test_nonhuman_policy_valid_source_grounded(_case):
    v=_visual_package(policy="source_grounded",subject=nt.VisualSubjectRef("source_nonhuman_agent",None,("fact-1",),()))
    assert nt.validate_human_story_package(v,context(v)).package is v

@pytest.mark.parametrize("_case",[pytest.param(None,id="nonhuman-policy-none-rejects-source-agent")])
def test_nonhuman_policy_none_rejects_source_agent(_case):
    v=_visual_package(policy="none",subject=nt.VisualSubjectRef("source_nonhuman_agent",None,("fact-1",),()))
    assert codes(v,context(v)) == ("source_nonhuman_agent_policy_required",)

@pytest.mark.parametrize(
    "policy",
    ["arbitrary","canonical_only","SOURCE_GROUNDED","source_grounded "," source_grounded"],
    ids=["nonhuman-policy-arbitrary-rejected","nonhuman-policy-canonical-only-rejected","nonhuman-policy-uppercase-rejected","nonhuman-policy-trailing-space-rejected","nonhuman-policy-leading-space-rejected"],
)
def test_nonhuman_policy_unknown_values_rejected(policy):
    v=_visual_package(policy=policy,subject=nt.VisualSubjectRef("object",None,(),()))
    assert codes(v,context(v)) == ("nonhuman_presence_policy_invalid",)

@pytest.mark.parametrize("_case",[pytest.param(None,id="nonhuman-policy-empty-rejected")])
def test_nonhuman_policy_empty_rejected(_case):
    with pytest.raises(ValueError): replace(package().visual_direction,nonhuman_presence_policy="")

@pytest.mark.parametrize("policy",["none","source_grounded"],ids=["nonhuman-policy-object-with-none-allowed","nonhuman-policy-object-with-source-grounded-allowed"])
def test_nonhuman_policy_object_controls(policy):
    v=_visual_package(policy=policy,subject=nt.VisualSubjectRef("object",None,(),()))
    assert nt.validate_human_story_package(v,context(v)).package is v

@pytest.mark.parametrize("refs",[(),("unknown",)],ids=["nonhuman-policy-source-agent-missing-facts","nonhuman-policy-source-agent-unknown-fact"])
def test_nonhuman_policy_source_agent_requires_grounded_facts(refs):
    v=_visual_package(policy="source_grounded",subject=nt.VisualSubjectRef("source_nonhuman_agent",None,refs,()))
    assert codes(v,context(v)) == ("visual_direction_unsupported",)

@pytest.mark.parametrize(
    "old_policy,new_policy",
    [("none","source_grounded"),("source_grounded","none"),("source_grounded","arbitrary")],
    ids=["nonhuman-policy-change-invalidates-evidence","nonhuman-policy-source-grounded-to-none-invalidates-evidence","nonhuman-policy-source-grounded-to-arbitrary-invalidates-evidence"],
)
def test_nonhuman_policy_change_invalidates_visual_evidence(old_policy,new_policy):
    original=_visual_package(policy=old_policy,subject=nt.VisualSubjectRef("object",None,(),()))
    stale_context=context(original)
    changed=replace(original,visual_direction=replace(original.visual_direction,nonhuman_presence_policy=new_policy))
    found=codes(changed,stale_context)
    assert "visual_grounding_evidence_invalid" in found
    if new_policy=="arbitrary": assert "nonhuman_presence_policy_invalid" in found

@pytest.mark.parametrize("_case",[pytest.param(None,id="nonhuman-policy-generic-robot-still-rejected")])
def test_nonhuman_policy_generic_robot_still_rejected(_case):
    v=_visual_package(policy="source_grounded",subject=nt.VisualSubjectRef("generic_robot",None,(),()))
    assert codes(v,context(v)) == ("generic_robot_visual",)
@pytest.mark.parametrize("label",["risk","meaning","critique","trust","responsibility","human","automation"],ids=["risk","meaning","critique","trust","responsibility","human","automation"])
def test_keywords_are_not_role_bans(label):
    v=package(); v=replace(v,primary_interpretation=replace(v.primary_interpretation,thematic_axis=label)); assert nt.validate_human_story_package(v,context(v))
@pytest.mark.parametrize(
    "variant",
    ["same-text-labels","case-only","whitespace-only","punctuation-only","unicode-dash","same-structure-labels"],
    ids=["same-text-changed-labels","case-only-variant","whitespace-only-variant","punctuation-only-variant","unicode-dash-variant","same-structure-different-labels"],
)
def test_cosmetic_changes_do_not_evade_diversity(variant):
    v=package(); prior=nt.validate_human_story_package(v,context(v)).derived_diversity_signature
    interpretation=v.primary_interpretation
    if variant=="same-text-labels": interpretation=replace(interpretation,interpretation_mode="letter",thematic_axis="trust")
    elif variant=="case-only": interpretation=replace(interpretation,text=interpretation.text.upper())
    elif variant=="whitespace-only": interpretation=replace(interpretation,text="  ".join(interpretation.text.split()))
    elif variant=="punctuation-only": interpretation=replace(interpretation,text=interpretation.text.replace(".","!"))
    elif variant=="unicode-dash": interpretation=replace(interpretation,text=interpretation.text.replace("quietly","quietly —"))
    else: interpretation=replace(interpretation,interpretation_mode="memo",rhetorical_form="technical note",ending_mode="closed")
    changed=replace(v,primary_interpretation=interpretation)
    c=replace(context(changed),diversity_context=nt.NarrativeDiversityContext((prior,)))
    assert any(code.startswith("narrative_") for code in codes(changed,c))

def _distinct_story(*, rhetorical_form="field note"):
    v=package()
    facts=tuple(nt.SourceFact(f"new-{index}",text) for index,text in enumerate((
        "A ceramic sensor measured rainfall beside a greenhouse.",
        "The gardener compared three morning readings in a paper ledger.",
        "A loose cable caused one isolated spike in the second reading.",
        "Moving the cable restored the ordinary moisture curve.",
        "The repaired sensor now supports the next irrigation decision.",
    ),1))
    def statement(index):
        fact=facts[index]
        return nt.GroundedStatement(fact.text,(fact.fact_id,),"observed",("plan:plan",),())
    changed=replace(
        v,source_facts=facts,hook=statement(0),human_problem=statement(1),tension=statement(2),
        turning_point=statement(3),resolution=statement(4),
        primary_interpretation=replace(v.primary_interpretation,text="Naz studies the repaired garden sensor and treats the stable curve as a practical invitation to water carefully.",source_fact_refs=(facts[4].fact_id,),thematic_axis="care",rhetorical_form=rhetorical_form,ending_mode="closed"),
        visual_direction=replace(v.visual_direction,narrative_subject="A repaired sensor beside wet greenhouse soil.",source_fact_refs=(facts[0].fact_id,)),
    )
    return changed,facts

@pytest.mark.parametrize("variant",["new-story-same-keyword","new-rhetorical-form"],ids=["different-story-same-keyword-allowed","different-rhetorical-form-allowed"])
def test_substantive_diversity_is_allowed(variant):
    original=package(); prior=nt.validate_human_story_package(original,context(original)).derived_diversity_signature
    changed,facts=_distinct_story(rhetorical_form="letter" if variant=="new-rhetorical-form" else original.primary_interpretation.rhetorical_form)
    if variant=="new-story-same-keyword": changed=replace(changed,primary_interpretation=replace(changed.primary_interpretation,thematic_axis=original.primary_interpretation.thematic_axis))
    c=replace(context(changed),expected_source_facts=facts,diversity_context=nt.NarrativeDiversityContext((prior,)))
    assert nt.validate_human_story_package(changed,c).package is changed
