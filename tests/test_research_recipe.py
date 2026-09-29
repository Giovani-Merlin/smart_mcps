"""Tests for orchestrator/recipes/research.py — the `research` Unit Recipe:
args, findings contract, and pricing (plan U6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.recipes.research import (
    Finding,
    FindingsReport,
    ResearchArgs,
    SpecRefinement,
    price_research,
)


class TestResearchArgs:
    def _base(self, **overrides):
        payload = {"question": "what should we use?", "output": "docs/research/x.md"}
        payload.update(overrides)
        return payload

    def test_output_outside_docs_research_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            ResearchArgs(**self._base(output="notes/x.md"))
        assert "output" in str(excinfo.value)

    def test_output_not_markdown_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            ResearchArgs(**self._base(output="docs/research/x.txt"))
        assert "output" in str(excinfo.value)

    def test_output_under_docs_research_accepted(self):
        args = ResearchArgs(**self._base())
        assert args.output == "docs/research/x.md"
        assert args.size == "medium"

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            ResearchArgs(**self._base(bogus="x"))


class TestFindingsReport:
    def _finding(self, **overrides) -> dict:
        payload = {
            "claim": "X is recommended",
            "sources": ["https://example.com/docs"],
            "confidence": "high",
        }
        payload.update(overrides)
        return payload

    def test_finding_with_no_source_rejected_naming_the_field(self):
        with pytest.raises(ValidationError) as excinfo:
            FindingsReport(
                status="completed",
                findings=[self._finding(sources=[])],
                verification_results=[],
            )
        message = str(excinfo.value)
        assert "findings.0.sources" in message

    def test_completed_with_zero_findings_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            FindingsReport(status="completed", findings=[], verification_results=[])
        assert "finding" in str(excinfo.value).lower()

    def test_completed_with_a_finding_accepted(self):
        report = FindingsReport(
            status="completed", findings=[self._finding()], verification_results=[]
        )
        assert len(report.findings) == 1

    def test_blocked_with_zero_findings_accepted(self):
        # Only a `completed` report requires a finding — a blocked/needs_input
        # research session may have nothing sourced yet.
        report = FindingsReport(status="blocked", findings=[], verification_results=[])
        assert report.findings == []

    def test_report_with_a_refinement_accepted(self):
        report = FindingsReport(
            status="completed",
            findings=[self._finding()],
            verification_results=[],
            spec_refinement=SpecRefinement(
                target_task="u10-optimize-recipe", refinement="use the noise floor from the ledger"
            ),
        )
        assert report.spec_refinement is not None
        assert report.spec_refinement.target_task == "u10-optimize-recipe"

    def test_finding_confidence_must_be_a_known_level(self):
        with pytest.raises(ValidationError):
            Finding(claim="x", sources=["a/b.py"], confidence="certain")


class TestPriceResearch:
    def test_no_size_declared_defaults_to_medium(self):
        price = price_research(None, {}, None)
        assert price.tokens == 60_000
        assert price.defaulted is True

    def test_size_declared_but_left_at_default_value_still_reads_as_defaulted(self):
        args = ResearchArgs(question="q", output="docs/research/x.md")
        price = price_research(args, {}, None)
        assert price.tokens == 60_000
        assert price.defaulted is True

    def test_size_explicitly_declared_is_not_defaulted(self):
        args = ResearchArgs(question="q", output="docs/research/x.md", size="large")
        price = price_research(args, {}, None)
        assert price.tokens == 120_000
        assert price.defaulted is False

    def test_small_size(self):
        args = ResearchArgs(question="q", output="docs/research/x.md", size="small")
        price = price_research(args, {}, None)
        assert price.tokens == 30_000
        assert price.defaulted is False
