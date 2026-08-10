"""Coverage for snapshot_grouping's directory handling.

The run's frozen copy of a grouping is what ADR 0002/0003 rely on: a later
``group --name <same>`` must not be able to rewrite a finished run's history. A
files-only copy quietly broke that guarantee for any nested artifact.
"""

from __future__ import annotations

from orchestrator.execution.manifest import snapshot_grouping


def test_snapshot_copies_nested_artifact_directories(tmp_path):
    """The grouper's LLM call records live in a nested ``llm/``. A files-only
    copy dropped them from every run snapshot while appearing to succeed."""
    source = tmp_path / "grouping"
    (source / "llm").mkdir(parents=True)
    (source / "groups.json").write_text("{}")
    (source / "llm" / "calls.json").write_text('{"calls": []}')
    (source / "llm" / "01-mapper-a0.request.txt").write_text("the prompt")

    dest = tmp_path / "run" / "grouping"
    snapshot_grouping(source, dest)

    assert (dest / "groups.json").read_text() == "{}"
    assert (dest / "llm" / "calls.json").read_text() == '{"calls": []}'
    assert (dest / "llm" / "01-mapper-a0.request.txt").read_text() == "the prompt"


def test_snapshot_includes_the_edge_provenance_sidecar(tmp_path):
    """Plan P2's sidecar is a top-level file, so the generic copy already covers it —
    asserted rather than assumed, because the run's frozen copy is the only place an
    operator can read a finished run's provenance from."""
    source = tmp_path / "grouping"
    source.mkdir(parents=True)
    (source / "groups.json").write_text("{}")
    (source / "edge-provenance.json").write_text('{"version": 1, "affinity": []}')

    dest = tmp_path / "run" / "grouping"
    snapshot_grouping(source, dest)

    assert (dest / "edge-provenance.json").read_text() == '{"version": 1, "affinity": []}'


def test_snapshot_is_repeatable(tmp_path):
    source = tmp_path / "grouping"
    (source / "llm").mkdir(parents=True)
    (source / "llm" / "calls.json").write_text("first")
    dest = tmp_path / "run" / "grouping"

    snapshot_grouping(source, dest)
    (source / "llm" / "calls.json").write_text("second")
    snapshot_grouping(source, dest)

    assert (dest / "llm" / "calls.json").read_text() == "second"
