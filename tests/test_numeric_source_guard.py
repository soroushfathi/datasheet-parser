import pytest

from datasheet_parser.models import Claim, SectionResult
from datasheet_parser.numeric_source_guard import (
    converted_source_numbers,
    guard_numeric_sources,
    source_numbers,
)


def test_decimal_prefix_cannot_be_invented_and_original_draft_remains():
    claim=Claim(id="9.1",requirement_id="9.specific",parameter="Output voltage",kind="quantity",
        raw_value="1.470 / 1.500 / 1.530",value="1.53 V",quantity={"minimum":1.47,"typical":1.5,
        "maximum":1.53,"unit":"V","expression":None},conditions=[],evidence_ids=["r"],
        status="present",explanation="",applies_to=[])
    section=SectionResult(section_id=9,status="present",findings=[{"requirement_id":"9.specific",
        "status":"present","evidence_ids":["r"],"reason":""}],claims=[claim],pins=[],circuits=[],notes=[])
    audit=guard_numeric_sources([section],[{"id":"r","kind":"table","rows":[["1.470","1.500",".530"]],
                                         "text":"1.470 1.500 .530"}],{9:{"r"}})
    assert section.claims[0].quantity.maximum is None
    assert section.claims[0].quantity.typical==1.5
    assert section.claims[0].status=="ambiguous" and section.findings[0].status=="ambiguous"
    assert audit[0]["original_model_claim"]["quantity"]["maximum"]==1.53


def test_explicit_tolerance_and_scientific_literals_are_preserved():
    assert {4.2,4.8,0.0013} <= source_numbers("4.50±0.30 mm; 1.3e-3 A")


def test_image_only_numeric_reading_is_not_cleared_by_missing_text():
    claim=Claim(id="7.1",requirement_id="7.dimensions",parameter="Drawing dimension A",kind="dimension",
        raw_value="6.40..6.70",value="6.40..6.70 mm",quantity={"minimum":6.4,"typical":None,
        "maximum":6.7,"unit":"mm","expression":None},conditions=[],evidence_ids=["image"],
        status="present",explanation="",applies_to=[])
    section=SectionResult(section_id=7,status="present",findings=[],claims=[claim],pins=[],circuits=[],notes=[])
    assert not guard_numeric_sources([section],[{"id":"image","kind":"page","text":"PACKAGE OUTLINE"}],{7:{"image"}})
    assert section.claims[0].quantity.maximum==6.7


def test_explicit_unit_prefix_conversion_is_preserved_without_cross_unit_inference():
    assert converted_source_numbers("10Hz≤f≤10kHz", "Hz")=={10,10000}
    assert next(iter(converted_source_numbers("100µV", "mV")))==pytest.approx(0.1)
    assert not converted_source_numbers("10mV", "A")
    claim=Claim(id="9.1",requirement_id="9.specific",parameter="Noise bandwidth",kind="quantity",
        raw_value="10Hz≤f≤10kHz",value="10..10000 Hz",quantity={"minimum":10,"typical":None,
        "maximum":10000,"unit":"Hz","expression":None},conditions=[],evidence_ids=["r"],
        status="present",explanation="",applies_to=[])
    section=SectionResult(section_id=9,status="present",findings=[],claims=[claim],pins=[],circuits=[],notes=[])
    assert not guard_numeric_sources([section],[{"id":"r","kind":"table","rows":[["10","100","200"]],
        "text":"10Hz≤f≤10kHz; 10 100 200"}],{9:{"r"}})
    assert section.claims[0].quantity.maximum==10000
