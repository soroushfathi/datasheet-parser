"""An identical fact in another schematic cannot erase a local omission."""

import json

from test_figure_workflow import FigureProvider, figure_fixture
from test_optimized_workflow import no_network_provider_or_tokenizer  # noqa: F401


class ScopedAbsenceProvider(FigureProvider):
    def execute(self, request, folder, images):
        response, telemetry = super().execute(request, folder, images)
        role, _, _, number = request.task.split(":")
        inventory = json.loads(request.prompt.split("RAW_PIN_INVENTORY: ", 1)[1].split("\n", 1)[0])
        common_pin_alias = inventory[0]["evidence_ids"][0]
        if role != "review":
            component = next(claim for claim in response["claims"] if claim["requirement_id"] == "2.components")
            component.update(parameter="R1 resistance", value="1 kOhm" if number == "13" else "10 kOhm",
                             conditions=[f"Scenario: Figure {number}"],
                             evidence_ids=[*component["evidence_ids"], common_pin_alias])
        else:
            # The first allegation is genuine within figure 13. The same literal
            # is a false absence within figure 27, where the original recorded it.
            # Both cite the common pin table, which must not connect their scopes.
            response["issues"] = [{
                "category": "missing_fact", "claim_id": None, "requirement_id": "2.components",
                "severity": "warning", "evidence_ids": [response["reviewed_region_ids"][-1], common_pin_alias],
                "explanation": "Missing fact: R1 resistance: 10 kOhm",
            }]
        return response, telemetry


def test_shared_pin_citation_does_not_reconcile_missing_fact_from_another_figure(tmp_path):
    parser, _, store, run = figure_fixture(tmp_path)
    provider = ScopedAbsenceProvider(parser.settings)
    parser.provider = provider
    original = parser.extract_retrieved(run, 2)
    row = parser.review_retrieved(run, 2, original)
    assert len(provider.requests) == 6  # Two source units × extract/blind/review; no extra model call.
    assert len(row["issues"]) == 1
    issue = row["issues"][0]
    assert issue["category"] == "missing_fact"
    assert any(rid.startswith("parse:p2") for rid in issue["evidence_ids"])
    assert not any(rid.startswith("parse:p3") for rid in issue["evidence_ids"])
    assert "parse:p1:t1" in issue["evidence_ids"]  # The shared proof remains visible, without sharing absence scope.

    audit = row["review_reconciliation"]
    assert audit["scope"] == "source_figure_units"
    assert len(audit["raw_review"]["issues"]) == 2
    assert {unit["figure_number"] for unit in audit["unit_reviews"]} == {"13", "27"}
    assert all(len(unit["raw_review"]["issues"]) == 1 for unit in audit["unit_reviews"])
    assert len(audit["decisions"]) == 1
    decision = audit["decisions"][0]
    assert decision["figure_number"] == "27"
    assert decision["unit_issue_index"] == 0
    assert "raw_issue_index" not in decision  # No merged/global issue index is claimed.
    assert decision["matches"][0]["claim_id"] == "2.f27.2"
    assert not next(unit for unit in audit["unit_reviews"] if unit["figure_number"] == "13")["decisions"]

    parts = store.find("review_figure_parts", {"run_id": run["id"]})
    assert len(parts) == 2 and all(len(part["review"]["issues"]) == 1 for part in parts)
    assert store.get("reviews", f"{run['id']}:2") == row
    assert parser.review_retrieved(run, 2, original) == row
    assert len(provider.requests) == 6  # The durable audit and raw parts survive a normal resume.
