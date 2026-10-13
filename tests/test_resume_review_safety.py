import json

import pytest

from datasheet_parser.config import Settings
from datasheet_parser.engine import Engine
from datasheet_parser.evidence_packet import EvidencePacket
from datasheet_parser.models import Catalog
from datasheet_parser.requirements import requirements
from datasheet_parser.review_safety import review_contract, review_source_regions, target_identity
from datasheet_parser.store import MemoryStore


def empty_section():
    return {"section_id":12,"status":"ambiguous","findings":[
        {"requirement_id":req,"status":"not_found","evidence_ids":[],"reason":"not located"}
        for req in requirements(12)],"claims":[],"pins":[],"circuits":[],"notes":[]}


def test_absence_review_preserves_reader_scope_without_positive_claims():
    original={"section":empty_section(),"supplied_region_ids":["p2:a","p2:b"]}
    blind={"section":empty_section(),"supplied_region_ids":[]}
    assert review_source_regions(original,blind,set()) == {"p2:a","p2:b"}
    assert target_identity({"id":"x","evidence_ids":["uncitable"],"reason":"provenance"}) == {"id":"x"}


def test_exact_canonical_ids_only_from_supplied_packet():
    packet=EvidencePacket("",[],{"e0":"p2:a"},[],[],{})
    assert packet.decode({"evidence_ids":["p2:a","[e0]"]}) == {"evidence_ids":["p2:a","p2:a"]}
    with pytest.raises(ValueError,match="unsupplied"):
        packet.decode({"evidence_ids":["p1:target_provenance"]})


def test_empty_reader_and_reviewer_do_not_call_llm_or_certify_absence(tmp_path):
    store=MemoryStore()
    engine=Engine(store,None,Settings(mongo_uri="unused",work_dir=tmp_path))
    run={"id":"r","target":{"id":"t"},"quality_version":"quality-v1","contract_version":"compact-v2"}
    engine.retrieval_packet=lambda *args,**kwargs: EvidencePacket("boilerplate",[],{},[],[],{})
    section,*_=engine.read_retrieved(run,12)
    assert len(section.findings)==3 and all(f.status == "ambiguous" for f in section.findings)
    assert not section.claims
    record={"section":empty_section(),"supplied_region_ids":[]}
    engine.blind_retrieved=lambda *args: record
    for req in requirements(12):
        store.put("coverage",{"id":f"r:{req}"})
    review=engine._review_retrieved(run,12,record)
    assert review["review_method"]=="skipped_no_source"
    assert not review["supported_claim_ids"] and not review["reviewed_region_ids"]
    assert len(review["issues"])==3


def test_semantically_rejected_checkpoint_archived_and_one_correction(tmp_path):
    class Provider:
        def __init__(self):
            self.count=0
            self.last_record={}
        def generate(self,request):
            self.count+=1
            return {"targets":[],"ambiguities":["invalid"] if self.count==1 else []}
    provider=Provider()
    engine=Engine(MemoryStore(),provider,Settings(mongo_uri="unused",work_dir=tmp_path))
    def validate(response):
        if response.ambiguities:
            raise ValueError("unsupplied source")
        return response
    result=engine.validated_call("r","review:12",Catalog,"source",[],validate=validate)
    assert not result.ambiguities and provider.count==2
    rejected=list((tmp_path/"checkpoints"/"rejected").glob("*.json"))
    assert len(rejected)==1
    assert json.loads(rejected[0].read_text())["response"]["ambiguities"] == ["invalid"]


def test_declined_review_cannot_support_claims():
    from datasheet_parser.models import Review
    declined=Review(supported_claim_ids=[],issues=[],reviewed_region_ids=[])
    Engine._validate_review_part(declined,{"12.clocks"},[],allow_declined=True)
    with pytest.raises(ValueError):
        Engine._validate_review_part(declined,{"12.clocks"},[])
    declined.supported_claim_ids=["12.1"]
    with pytest.raises(ValueError):
        Engine._validate_review_part(declined,{"12.clocks"},[{"id":"12.1"}],allow_declined=True)


def test_review_contract_cannot_certify_findings_or_independent_claims():
    model=review_contract([], {"12.clocks"})
    with pytest.raises(ValueError):
        model.model_validate({"supported_claim_ids":["12.clocks"], "issues":[], "reviewed_region_ids":["e0"]})
    schema=model.model_json_schema()
    assert schema["properties"]["supported_claim_ids"]["maxItems"]==0
    model=review_contract([{"id":"6.1"}], {"6.types"})
    response={"supported_claim_ids":["6.1"], "issues":[{"category":"incorrect", "claim_id":"INDEPENDENT:6.1",
        "requirement_id":"6.types", "severity":"warning", "evidence_ids":[], "explanation":"blind reader error"}],
        "reviewed_region_ids":["e0"]}
    with pytest.raises(ValueError):
        model.model_validate(response)
    response["issues"][0]["claim_id"]=None
    assert model.model_validate(response).issues[0].claim_id is None


def test_nonconforming_router_response_gets_one_bounded_contract_correction(tmp_path):
    class Provider:
        def __init__(self):
            self.last_record={}
            self.count=0
        def generate(self, request):
            self.count+=1
            return {"supported_claim_ids":["12.clocks"] if self.count==1 else [],
                    "issues":[],"reviewed_region_ids":["e0"]}
    provider=Provider()
    engine=Engine(MemoryStore(),provider,Settings(mongo_uri="unused",work_dir=tmp_path))
    result=engine.validated_call("r","review:12",review_contract([], {"12.clocks"}),
                                "source",[],validate=lambda response:response)
    assert result.supported_claim_ids==[] and provider.count==2


def test_recorded_router_schema_rejection_is_preserved_and_corrected(tmp_path):
    from datasheet_parser.providers import RecordedProvider
    class Router(RecordedProvider):
        name="fixture"
        count=0
        def execute(self, request, folder, images):
            self.count+=1
            return {"supported_claim_ids":["12.clocks"] if self.count==1 else [],
                    "issues":[],"reviewed_region_ids":["e0"]}, {}
    settings=Settings(mongo_uri="unused",work_dir=tmp_path)
    provider=Router(settings)
    engine=Engine(MemoryStore(),provider,settings)
    result=engine.validated_call("r","review:12",review_contract([], {"12.clocks"}),
                                "source",[],validate=lambda response:response)
    assert result.supported_claim_ids==[] and provider.count==2
    raw=list((tmp_path/"llm").rglob("unvalidated-response.json"))
    assert len(raw)==1
    assert json.loads(raw[0].read_text())["supported_claim_ids"]==["12.clocks"]


def test_quarantined_response_requires_same_fingerprint_and_semantic_revalidation(tmp_path):
    class Provider:
        def __init__(self):
            self.count=0
            self.last_record={}
        def generate(self,request):
            self.count+=1
            return {"targets":[],"ambiguities":[]}
    provider=Provider()
    engine=Engine(MemoryStore(),provider,Settings(mongo_uri="unused",work_dir=tmp_path))
    def old_policy(response):
        raise ValueError("declined source inspection")
    with pytest.raises(ValueError):
        engine.validated_call("r","review:12",Catalog,"same source",[],validate=old_policy)
    assert provider.count==2
    recovered=engine.validated_call("r","review:12",Catalog,"same source",[],validate=lambda response:response)
    assert not recovered.targets and provider.count==2
    assert provider.last_record["semantic_revalidated"]
    engine.validated_call("r","review:12",Catalog,"different source",[],validate=lambda response:response)
    assert provider.count==3
