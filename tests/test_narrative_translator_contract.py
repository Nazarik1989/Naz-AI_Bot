from __future__ import annotations
from dataclasses import replace
import os
import subprocess
import sys
import unicodedata
import pytest
import narrative_translator as nt

PLAN = nt.EditorialPlanBinding("plan", "source", "story_first", "story_pack")
FACTS = tuple(nt.SourceFact(f"fact-{n}", text) for n, text in enumerate(("Atomically created file.", "UTF-8 without a BOM.", "Current path was checked.", "Repeated run confirmed result.", "Manual checking beats blind trust."), 1))

def st(fid): return nt.GroundedStatement(next(x.text for x in FACTS if x.fact_id == fid), [fid], "observed", ["plan:plan"], [])
def state(char, rev=1): return nt.CharacterStateSnapshot(char, f"{char}-v1", rev, 50, 60, 20, 70, 60, 40, "quiet", "quiet", "calm", "topic", ["event"], f"{char}:{rev}")
def canon(char, relationship=False):
    refs=[nt.CanonSourceRef(char,f"{char}-personality",f"C:/private/{char}.md","v1","a"*64,"personality"),nt.CanonSourceRef(char,f"{char}-visual",f"C:/private/{char}-v.md","v1","b"*64,"visual")]
    if relationship: refs.append(nt.CanonSourceRef(char,f"{char}-relationship",f"C:/private/{char}-r.md","v1","c"*64,"relationship"))
    return nt.CharacterCanonSnapshot(char,refs,f"canon:{char}")
def interp(char, snapshot, relation=None):
    text = "Naz quietly considers the repeated check before accepting the calm result." if char=="naz" else "VOID warmly shares the modest result and remains beside the careful work."
    return nt.CharacterInterpretation(char,text,["fact-5"],[f"{char}-personality"],snapshot.snapshot_ref,relation.snapshot_ref if relation else None,"reflection","risk","warm","field note","close","none",None,"open",["bound"])
def package(primary="naz", mode="none"):
    first=state(primary); second=None; states=[first]; canons=[canon(primary,mode!="none")]; rel=None; duo=nt.DuoNarrativeContext("none",None,None,None,[])
    if mode in ("implicit","explicit"):
        other="void" if primary=="naz" else "naz"; otherstate=state(other); rel=nt.RelationshipStateSnapshot("rel-v1",1,60,60,20,70,80,"care","topic",["u"],["j"],["m"],"rel:1"); second=interp(other,otherstate,rel); states.append(otherstate); canons.append(canon(other,True)); duo=nt.DuoNarrativeContext(mode,rel.snapshot_ref,"agreement" if mode=="explicit" else None,"context",["fact-4"])
    return nt.HumanStoryPackage(nt.HUMAN_STORY_SCHEMA,"plan","source",list(FACTS),st("fact-1"),st("fact-2"),st("fact-3"),st("fact-4"),st("fact-5"),interp(primary,first,rel),second,states,canons,rel,duo,nt.VisualDirection("documentary","A checked file.","none","none",["edge"],[],["fact-1"],[f"{primary}-visual"],[nt.VisualSubjectRef("object",None,[],[])]),"story",nt.ConfidenceAssessment("high",[]))
def context(value, plan=PLAN):
    chars=tuple(x for x in (value.primary_interpretation,value.secondary_interpretation) if x); needed=value.duo_context.presence_mode!="none"
    policy=nt.EvidenceAuthorityPolicy("policy-v1","semantic","sem-v1",[nt.CharacterEvidencePolicy(x.character_id,f"cont:{x.character_id}","cont-v1") for x in chars],"rel-auth" if needed else None,"rel-v1" if needed else None,"visual","visual-v1")
    semantic=[nt.SemanticGroundingEvidence(plan.plan_id,plan.source_ref,nt._interpretation_digest(x,next(y for y in value.character_states if y.character_id==x.character_id),plan,policy.semantic_rules_version),x.source_fact_refs,"semantic","sem-v1","supported") for x in chars]
    continuity=[nt.CharacterContinuityEvidence(plan.plan_id,plan.source_ref,x.character_id,x.state_snapshot_ref,next(y.revision for y in value.character_states if y.character_id==x.character_id),x.relationship_snapshot_ref,nt._interpretation_digest(x,next(y for y in value.character_states if y.character_id==x.character_id),plan,policy.semantic_rules_version),f"cont:{x.character_id}","cont-v1","supported") for x in chars]
    relation=None
    if needed:
        r=value.relationship_state
        relation=nt.RelationshipContinuityEvidence(
            plan_id=plan.plan_id,source_ref=plan.source_ref,presence_mode=value.duo_context.presence_mode,
            interaction_mode=value.duo_context.interaction_mode,relation_to_story=value.duo_context.relation_to_story,
            primary_character_id=value.primary_interpretation.character_id,secondary_character_id=value.secondary_interpretation.character_id,
            primary_interpretation_digest=nt.character_interpretation_digest(value.primary_interpretation),
            secondary_interpretation_digest=nt.character_interpretation_digest(value.secondary_interpretation),
            relationship_snapshot_ref=r.snapshot_ref,relationship_revision=r.revision,relationship_version=r.version,
            source_fact_refs=value.duo_context.source_fact_refs,
            duo_context_digest=nt.relationship_continuity_digest(plan=plan,duo_context=value.duo_context,primary_interpretation=value.primary_interpretation,secondary_interpretation=value.secondary_interpretation,relationship_snapshot=r,rules_version="rel-v1"),
            authority_ref="rel-auth",rules_version="rel-v1",decision="supported",
        )
    return nt.HumanStoryValidationContext(plan,FACTS,[nt.CharacterSnapshotAuthority(x.character_id,x.snapshot_ref,x.revision,x.core_version) for x in value.character_states],nt.RelationshipSnapshotAuthority(value.relationship_state.snapshot_ref,value.relationship_state.revision,value.relationship_state.version) if needed else None,semantic,continuity,relation,nt.VisualGroundingEvidence(plan.plan_id,plan.source_ref,nt._visual_digest(value.visual_direction,plan,policy.visual_rules_version),"visual","visual-v1","supported"),policy,nt.NarrativeDiversityContext([]))
def codes(value, ctx=None):
    with pytest.raises(nt.HumanStoryValidationError) as e: nt.validate_human_story_package(value,ctx or context(value))
    return e.value.reason_codes

@pytest.mark.parametrize("mode,fmt",[("standard","story_pack"),("","story_pack"),("STORY_FIRST","story_pack"),("story_first ","story_pack"),("story_first","standard"),("story_first","")],ids=["production-mode-standard","production-mode-missing","production-mode-uppercase","production-mode-trailing-space","content-format-wrong","content-format-missing"])
def test_boundary_rejects_non_story_first(mode,fmt):
    if not mode or not fmt:
        with pytest.raises(ValueError): replace(PLAN,production_mode=mode,content_format=fmt)
    else:
        assert codes(package(),context(package(),replace(PLAN,production_mode=mode,content_format=fmt))) == ("human_story_story_first_required",)

@pytest.mark.parametrize("field",["plan_id","source_ref"],ids=["plan-id-mismatch","source-ref-mismatch"])
def test_plan_binding_mismatch(field):
    v=package(); changed=replace(v,**{field:"different"})
    assert codes(changed,context(changed)) == (("human_story_plan_binding_invalid",) if field=="plan_id" else ("human_story_source_binding_invalid",))
@pytest.mark.parametrize("change",["rename","reorder","text","extra","missing"],ids=["fact-id-renamed","fact-order-changed","fact-text-changed","fact-extra","fact-missing"])
def test_fact_identity_cases(change):
    v=package(); facts=list(v.source_facts)
    if change=="rename": facts[0]=replace(facts[0],fact_id="renamed")
    elif change=="reorder": facts[0],facts[1]=facts[1],facts[0]
    elif change=="text": facts[0]=replace(facts[0],text="changed")
    elif change=="extra": facts.append(nt.SourceFact("fact-x","x"))
    elif change=="missing": facts.pop()
    assert "source_fact_" in " ".join(codes(replace(v,source_facts=facts),context(replace(v,source_facts=facts))))

@pytest.mark.parametrize("variant",["unicode","line-ending","trailing-space"],ids=["fact-unicode-composition-changed","fact-line-ending-changed","fact-trailing-space-changed"])
def test_fact_byte_exact_variants(variant):
    v=package(); expected=list(v.source_facts); actual=list(v.source_facts)
    if variant=="unicode":
        expected_text=unicodedata.normalize("NFC","Cafe\u0301 result")
        actual_text=unicodedata.normalize("NFD",expected_text)
    elif variant=="line-ending": expected_text,actual_text="Line one\r\nLine two","Line one\nLine two"
    else: expected_text,actual_text="Atomically created file. ","Atomically created file."
    expected[0]=replace(expected[0],text=expected_text); actual[0]=replace(actual[0],text=actual_text)
    changed=replace(v,source_facts=actual,hook=replace(v.hook,text=actual_text))
    ctx=replace(context(changed),expected_source_facts=tuple(expected))
    assert codes(changed,ctx) == ("source_fact_text_changed",)
@pytest.mark.parametrize("kind",["set","generator","mapping","list-subclass","tuple-subclass"],ids=["set","generator","mapping","list-subclass","tuple-subclass"])
def test_immutable_container_rejection(kind):
    values={"set":{"x"},"generator":(x for x in ["x"]),"mapping":{"x":1},"list-subclass":type("L",(list,),{})(["x"]),"tuple-subclass":type("T",(tuple,),{})(["x"])}
    with pytest.raises(TypeError): nt.GroundedStatement("x",values[kind],"observed")

def test_nested_list_rejected():
    with pytest.raises(TypeError): nt.GroundedStatement("x",(["fact-1"],),"observed")

def test_cross_process_digest_stable():
    script="import narrative_translator as n; print(n._digest(('alpha','beta','gamma')))"
    outputs=[]
    for seed in ("1","2"):
        env=dict(os.environ,PYTHONHASHSEED=seed)
        outputs.append(subprocess.check_output([sys.executable,"-c",script],cwd=os.getcwd(),env=env,text=True).strip())
    assert outputs[0] == outputs[1]
@pytest.mark.parametrize("field,value",[("revision",True),("revision","1"),("revision",1.0),("revision",-1),("energy",True),("energy",101)],ids=["bool-revision","string-revision","float-revision","negative-revision","bool-axis","range-axis"])
def test_state_scalars(field,value):
    v=package()
    with pytest.raises((TypeError,ValueError)): replace(v.character_states[0],**{field:value})
def test_adapter_revalidates_and_minimizes_projection():
    v=package(primary="void"); brief=nt.build_storyboard_narrative_brief(v,context(v)); nt.validate_storyboard_narrative_brief_structure(brief)
    assert brief.primary_interpretation.character_id=="void" and not hasattr(brief,"character_states") and not hasattr(brief.canon_refs[0],"source_path")

class StringSubclass(str): pass

@pytest.mark.parametrize("case",["source-fact-text-list","source-fact-id-list","source-fact-text-dict","source-fact-text-string-subclass","grounded-statement-text-list","canon-source-id-list","interpretation-text-list","authority-ref-list","rules-version-list"],ids=["source-fact-text-list-rejected","source-fact-id-list-rejected","source-fact-text-dict-rejected","source-fact-text-string-subclass-rejected","grounded-statement-text-list-rejected","canon-source-id-list-rejected","interpretation-text-list-rejected","authority-ref-list-rejected","rules-version-list-rejected"])
def test_scalar_masquerading_rejected(case):
    v=package()
    with pytest.raises(TypeError):
        if case=="source-fact-text-list": nt.SourceFact("fact",["mutable"])
        elif case=="source-fact-id-list": nt.SourceFact(["fact"],"text")
        elif case=="source-fact-text-dict": nt.SourceFact("fact",{"text":"mutable"})
        elif case=="source-fact-text-string-subclass": nt.SourceFact("fact",StringSubclass("text"))
        elif case=="grounded-statement-text-list": nt.GroundedStatement(["text"],["fact-1"],"observed")
        elif case=="canon-source-id-list": replace(v.character_canons[0].canon_refs[0],source_id=["id"])
        elif case=="interpretation-text-list": replace(v.primary_interpretation,text=["text"])
        elif case=="authority-ref-list": nt.CharacterEvidencePolicy("naz",["authority"],"v1")
        else: nt.CharacterEvidencePolicy("naz","authority",["v1"])

def test_scalar_mutation_cannot_change_digest():
    refs=["fact-1"]
    statement=nt.GroundedStatement("Stable text",refs,"observed")
    before=nt._digest(statement); refs.append("fact-2")
    assert statement.source_fact_refs == ("fact-1",) and nt._digest(statement) == before

@pytest.mark.parametrize("case",["duplicate-facts","unknown-story-ref","unknown-visual-ref","missing-primary-state","relationship-ref-none","relationship-ref-required","same-character-ids","bad-package-digest","signature-identity"],ids=["brief-duplicate-facts","brief-unknown-story-ref","brief-unknown-visual-ref","brief-missing-primary-state","brief-relationship-ref-none","brief-relationship-ref-required","brief-same-character-ids","brief-bad-package-digest","brief-signature-identity"])
def test_brief_structural_matrix(case):
    v=package(mode="implicit" if case in {"relationship-ref-required","same-character-ids"} else "none")
    brief=nt.build_storyboard_narrative_brief(v,context(v))
    if case=="duplicate-facts": brief=replace(brief,source_facts=(brief.source_facts[0],brief.source_facts[0],*brief.source_facts[2:]))
    elif case=="unknown-story-ref": brief=replace(brief,hook=replace(brief.hook,source_fact_refs=("unknown",)))
    elif case=="unknown-visual-ref": brief=replace(brief,visual_direction=replace(brief.visual_direction,source_fact_refs=("unknown",)))
    elif case=="missing-primary-state": brief=replace(brief,character_state_refs=())
    elif case=="relationship-ref-none": brief=replace(brief,relationship_state_ref=nt.RelationshipStateReceiptRef("rel",1,"v1"))
    elif case=="relationship-ref-required": brief=replace(brief,relationship_state_ref=None)
    elif case=="same-character-ids": brief=replace(brief,secondary_interpretation=replace(brief.secondary_interpretation,character_id=brief.primary_interpretation.character_id))
    elif case=="bad-package-digest": brief=replace(brief,package_digest="bad")
    else: brief=replace(brief,derived_diversity_signature=replace(brief.derived_diversity_signature,primary_character_id="void"))
    with pytest.raises(nt.HumanStoryValidationError) as caught: nt.validate_storyboard_narrative_brief_structure(brief)
    assert "storyboard_" in " ".join(caught.value.reason_codes)
