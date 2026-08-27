"""Plan U8: --no-spec and --dry-run write their trace/edge-provenance into a
preview subdirectory rather than beside a groups.json that may describe a
different partition.

Drives the real CLI (`main`) against the same fixtures `test_grouper_pipeline`
uses, since the bug is entirely about where `_cmd_group` writes files on disk.
"""

from __future__ import annotations

import json

from orchestrator.cli import main
from orchestrator.execution.manifest import describe_groupings
from orchestrator.grouping.trace import GroupingTrace
from test_grouper_pipeline import StubLlm, make_client, make_repo


def _groups_json(repo, name="plan"):
    return repo / ".orchestrator" / "groupings" / name / "groups.json"


def _sibling_trace(repo, name="plan"):
    return repo / ".orchestrator" / "groupings" / name / "grouping-trace.json"


def _preview_trace(repo, name="plan"):
    return repo / ".orchestrator" / "groupings" / name / "preview" / "grouping-trace.json"


class TestSpeclessRunAfterRealGrouping:
    """g28-preserves-existing"""

    def test_no_spec_leaves_existing_groups_json_and_sibling_trace_untouched(self, tmp_path):
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        groups_before = _groups_json(repo).read_text()
        trace_before = _sibling_trace(repo).read_text()

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        assert _groups_json(repo).read_text() == groups_before
        assert _sibling_trace(repo).read_text() == trace_before
        assert _preview_trace(repo).is_file()

    def test_dry_run_leaves_existing_groups_json_and_sibling_trace_untouched(self, tmp_path):
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        groups_before = _groups_json(repo).read_text()
        trace_before = _sibling_trace(repo).read_text()

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        assert _groups_json(repo).read_text() == groups_before
        assert _sibling_trace(repo).read_text() == trace_before
        assert _preview_trace(repo).is_file()


class TestObservatoryListingUsesGroupsJson:
    """g28-listing-uses-groups-json"""

    def test_grouping_preview_endpoint_reports_groups_json_not_preview_trace(self, tmp_path):
        from orchestrator.observatory.grouping import build_grouping_preview

        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        real_partition = json.loads(_groups_json(repo).read_text())

        # A specless run afterwards must not change what the launch-page
        # preview reports for this name.
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        view = build_grouping_preview(repo, "plan")
        assert view.present is True
        assert [g.id for g in view.groups] == [g["id"] for g in real_partition["groups"]]

    def test_index_fingerprint_reused_from_the_undisturbed_sibling_trace(self, tmp_path):
        """The fingerprint-compare-on-resume path (plan U7) reads
        ``groups_path.parent / "grouping-trace.json"``; a specless run must not
        have clobbered it with a different partition's trace."""
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        recorded_before = GroupingTrace.model_validate_json(_sibling_trace(repo).read_text())

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        recorded_after = GroupingTrace.model_validate_json(_sibling_trace(repo).read_text())
        assert recorded_before.provenance is not None
        assert recorded_after.provenance is not None
        assert (
            recorded_before.provenance.index_fingerprint
            == recorded_after.provenance.index_fingerprint
        )


class TestSpeclessRunBeforeAnyRealGrouping:
    """g28-not-a-failed-grouping"""

    def test_no_spec_leaves_the_name_absent_from_describe_groupings(self, tmp_path):
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert not _groups_json(repo).is_file()
        assert _preview_trace(repo).is_file()
        assert describe_groupings(repo) == []

    def test_dry_run_leaves_the_name_absent_from_describe_groupings(self, tmp_path):
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert not _groups_json(repo).is_file()
        assert _preview_trace(repo).is_file()
        assert describe_groupings(repo) == []

    def test_observatory_preview_labels_it_explicitly_as_a_preview(self, tmp_path):
        from orchestrator.observatory.grouping import build_grouping_preview

        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        view = build_grouping_preview(repo, "plan")
        assert view.present is False
        assert view.missing is not None
        assert "--no-spec" in view.missing or "--dry-run" in view.missing


class TestSubsequentRealGroupingIsNormal:
    """g28-real-grouping-normal"""

    def test_real_grouping_after_a_specless_run_writes_groups_json_normally(self, tmp_path):
        repo, plan = make_repo(tmp_path)

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert not _groups_json(repo).is_file()

        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0

        assert _groups_json(repo).is_file()
        assert _sibling_trace(repo).is_file()
        infos = describe_groupings(repo)
        assert [info.name for info in infos] == ["plan"]
