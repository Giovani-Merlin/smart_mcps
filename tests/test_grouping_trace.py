"""Tests for orchestrator/grouping/trace.py — the U8 versioned trace model and
recorder, and its wiring through compute_partition/run_grouping (plan U8/U9).

Reuses the grouping-fixture harness (tests/test_grouping_fixtures.py) for the
inertness/replay/closed-set properties, so every trace-model claim is checked
against the same real fixture shapes the partitioner itself is tested against.
"""

from __future__ import annotations


import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.partition import TaskGraph, canonical_pair, merge_small_groups
from orchestrator.grouping.pipeline import compute_partition, run_grouping
from orchestrator.grouping.trace import (
    MERGE_REJECTION_REASONS,
    GroupingTrace,
    TraceRecorder,
    serialize_trace,
)
from tests.test_grouper_pipeline import StubLlm, make_client
from tests.test_grouper_pipeline import make_repo as make_toy_repo
from tests.test_grouping_fixtures import ALL_FIXTURES, client_for
from tests.test_grouping_fixtures import make_repo as make_fixture_repo


def _graph(nodes, affinity=None, dependencies=None, slices=None):
    return TaskGraph(
        nodes=frozenset(nodes),
        affinity={canonical_pair(*k): v for k, v in (affinity or {}).items()},
        dependencies=dict(dependencies or {}),
        metadata={node: {"slice": label} for node, label in (slices or {}).items()},
    )


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("fixture tests must stay zero-token")


def _compute(tmp_path, fixture_name, real_files, config_overrides, recorder=None):
    config = OrchestratorConfig()
    for key, value in config_overrides.items():
        setattr(config.estimator, key, value)
    if fixture_name == "slice-over-budget":
        # U6: its reports slice is a declared, intentional cap overshoot, so the
        # overflow gate raises by default. These are trace-property tests, not
        # gate tests — accept the overshoot so the pipeline runs to completion and
        # the trace can be inspected. Mirrors the same override in
        # test_grouping_fixtures.py; the gate itself is covered by its own tests.
        config.partition.allow_oversized_slice = True
    repo, plan = make_fixture_repo(tmp_path, fixture_name, real_files=real_files)
    return compute_partition(
        plan_path=plan,
        repo_root=repo,
        config=config,
        llm_runner=_llm_must_not_be_called,
        client=client_for(repo, fixture_name),
        recorder=recorder,
    )


class TestRecorderIsInert:
    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_partition_identical_with_and_without_recorder(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        without = _compute(tmp_path / "a", fixture_name, real_files, config_overrides, None)
        recorder = TraceRecorder()
        with_recorder = _compute(
            tmp_path / "b", fixture_name, real_files, config_overrides, recorder
        )
        assert without.partition == with_recorder.partition
        assert without.last_stage == with_recorder.last_stage
        assert without.flags == with_recorder.flags


class TestStageEntriesRecorded:
    def test_greenfield_cross_stack_has_one_entry_per_executed_stage(self, tmp_path):
        recorder = TraceRecorder()
        outcome = _compute(tmp_path, "greenfield-cross-stack", None, {}, recorder)
        stage_names = [stage.stage for stage in recorder.trace.stages]
        # This fixture declares slices (contraction, not louvain) and always
        # runs through compute_partition with a finite budget_cap, so split
        # always executes too.
        assert stage_names == ["contraction", "lift", "split", "merge", "repair", "renumber"]
        all_nodes = set(outcome.graph.nodes)
        for stage in recorder.trace.stages:
            assert set(stage.partition) == all_nodes


class TestTraceIsReplayable:
    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_replaying_stages_reconstructs_the_final_partition(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        recorder = TraceRecorder()
        outcome = _compute(tmp_path, fixture_name, real_files, config_overrides, recorder)
        assert recorder.trace.stages, "at least one stage must be recorded"

        replayed: dict[str, int] = {}
        for stage in recorder.trace.stages:
            replayed.update(stage.partition)

        assert replayed == outcome.partition


class TestMergeRejectionReasonsClosedSet:
    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_every_rejected_candidate_has_a_closed_set_reason(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        recorder = TraceRecorder()
        _compute(tmp_path, fixture_name, real_files, config_overrides, recorder)
        rejected = [m for m in recorder.trace.merges if not m.accepted]
        for candidate in rejected:
            assert candidate.reason in MERGE_REJECTION_REASONS
        accepted = [m for m in recorder.trace.merges if m.accepted]
        for candidate in accepted:
            assert candidate.reason == ""

    def test_over_budget_reason_on_the_known_shape(self):
        """Same shape as test_partition.py::test_merge_respects_budget_cap."""
        g = _graph("a b".split(), dependencies={("a", "b"): 1.0})
        recorder = TraceRecorder()
        merge_small_groups(g, {"a": 0, "b": 1}, lambda n: 3.0, budget_cap=5.0, recorder=recorder)
        assert [m.reason for m in recorder.trace.merges] == ["over_budget"]

    def test_not_chain_compatible_reason_on_the_known_shape(self):
        """A candidate pair must exist (a dependency edge between the two
        groups) for chain_compatible to even run: here a->b makes {b} a
        candidate merge into {a, x}, but x and b share no reachability either
        way, so the cross pair (x, b) fails the guard."""
        g = _graph("a b x".split(), dependencies={("a", "b"): 1.0})
        recorder = TraceRecorder()
        merge_small_groups(
            g, {"a": 0, "x": 0, "b": 1}, lambda n: 1.0, budget_cap=100.0, recorder=recorder
        )
        assert [m.reason for m in recorder.trace.merges] == ["not_chain_compatible"]

    def test_would_create_cycle_reason_on_the_known_shape(self):
        """Same shape as
        test_partition.py::TestMergeAcyclicGuard::test_merge_creating_cross_group_cycle_is_rejected."""
        g = _graph(
            "hub feature agg".split(),
            dependencies={
                ("hub", "feature"): 1.0,
                ("feature", "agg"): 1.0,
                ("hub", "agg"): 1.0,
            },
        )
        work = {"hub": 1.0, "feature": 10.0, "agg": 1.0}
        recorder = TraceRecorder()
        merge_small_groups(
            g,
            {"hub": 0, "feature": 1, "agg": 2},
            lambda n: work[n],
            budget_cap=5.0,
            recorder=recorder,
        )
        reasons = {m.reason for m in recorder.trace.merges if not m.accepted}
        assert "would_create_cycle" in reasons
        for candidate in recorder.trace.merges:
            if not candidate.accepted:
                assert candidate.reason in MERGE_REJECTION_REASONS

    def test_makespan_regression_reason_on_a_shape_that_isolates_it(self):
        """b and c are mutually dependent (each reachable from the other, so
        chain_compatible never rejects a candidate pairing them) and a->c;
        after b merges into c, merging that group into a leaves work (10+5+10)
        serialized on one worker, which the simulated zero-communication
        makespan rejects even though every other guard passes."""
        g = _graph(
            "a b c".split(), dependencies={("c", "b"): 1.0, ("a", "c"): 1.0, ("b", "c"): 1.0}
        )
        work = {"a": 10.0, "b": 5.0, "c": 10.0}
        recorder = TraceRecorder()
        merge_small_groups(g, {"a": 0, "b": 1, "c": 2}, lambda n: work[n], None, recorder=recorder)
        reasons = [m.reason for m in recorder.trace.merges if not m.accepted]
        assert "makespan_regression" in reasons
        for candidate in recorder.trace.merges:
            if not candidate.accepted:
                assert candidate.reason in MERGE_REJECTION_REASONS


class TestSchemaVersionAndRoundtrip:
    def test_schema_version_is_exposed(self, tmp_path):
        recorder = TraceRecorder()
        _compute(tmp_path, "greenfield-cross-stack", None, {}, recorder)
        assert recorder.trace.schema_version == 1

    def test_roundtrips_through_json_unchanged(self, tmp_path):
        recorder = TraceRecorder()
        _compute(tmp_path, "observatory-round-a", None, {}, recorder)
        dumped = recorder.trace.model_dump_json()
        restored = GroupingTrace.model_validate_json(dumped)
        assert restored == recorder.trace


class TestNoTimestampByteStable:
    def test_model_has_no_timestamp_field(self):
        assert "created_at" not in GroupingTrace.model_fields
        for name in GroupingTrace.model_fields:
            assert "time" not in name and "date" not in name

    def test_serializing_the_same_trace_twice_is_byte_identical(self, tmp_path):
        recorder = TraceRecorder()
        _compute(tmp_path, "hub-in-the-middle", None, {}, recorder)
        assert serialize_trace(recorder.trace) == serialize_trace(recorder.trace)

    def test_two_runs_of_the_same_fixture_serialize_identically(self, tmp_path):
        """Plan U5 (added 2026-07-30): provenance carries a real wall-clock
        ``timestamp`` — the one field in the whole trace that is *expected* to
        differ run to run, by design (it records *when* the grouping ran, not
        what it computed). Byte-stability of everything else still holds;
        this test excludes just that one leaf field rather than the whole
        ``provenance`` section, so a real content drift inside provenance
        (e.g. the index fingerprint) would still be caught."""
        recorder_a = TraceRecorder()
        _compute(tmp_path / "a", "no-affinity-sink", None, {}, recorder_a)
        recorder_b = TraceRecorder()
        _compute(tmp_path / "b", "no-affinity-sink", None, {}, recorder_b)
        exclude = {"provenance": {"timestamp"}}
        assert recorder_a.trace.model_dump(exclude=exclude) == recorder_b.trace.model_dump(
            exclude=exclude
        )


class TestScorecardRecorded:
    """Plan U5: the scorecard is computed once and recorded verbatim — the
    values ``group --no-spec`` prints come straight from this same entry, so
    they can never drift from what lands in grouping-trace.json (see
    tests/test_cli.py::TestScorecardPrinting for the CLI-surface half)."""

    def test_scorecard_present_and_internally_consistent(self, tmp_path):
        recorder = TraceRecorder()
        outcome = _compute(tmp_path, "pure-backend", None, {}, recorder)
        sc = recorder.trace.scorecard
        assert sc is not None
        by_gid: dict[int, int] = {}
        for gid in outcome.partition.values():
            by_gid[gid] = by_gid.get(gid, 0) + 1
        assert sc.group_count == len(by_gid)
        assert sc.slice_integrity_ok is True
        assert 0.0 <= sc.work_fraction_min <= sc.work_fraction_mean <= sc.work_fraction_max
        assert sc.critical_path_length >= 1

    def test_no_partition_produced_means_no_scorecard(self, tmp_path):
        """A grouping that raises before producing a partition (plan U5) must
        not carry a stale or partial scorecard."""
        recorder = TraceRecorder()
        repo, plan = make_fixture_repo(tmp_path, "slice-over-budget")
        config = OrchestratorConfig()
        config.estimator.token_budget = 8_000  # forces the slice-overflow GrouperError
        with pytest.raises(Exception):
            compute_partition(
                plan_path=plan,
                repo_root=repo,
                config=config,
                llm_runner=_llm_must_not_be_called,
                client=client_for(repo, "slice-over-budget"),
                recorder=recorder,
            )
        assert recorder.trace.scorecard is None


class TestProvenanceAndIndexFingerprint:
    """Plan U5: timestamp, plan path, plan content hash, repo commit SHA,
    worktree-dirty flag, and an index fingerprint — sufficient to attribute a
    partition to the exact plan, repo state, and index that produced it."""

    def test_all_fields_recorded(self, tmp_path):
        recorder = TraceRecorder()
        _compute(tmp_path, "pure-backend", None, {}, recorder)
        prov = recorder.trace.provenance
        assert prov is not None
        assert prov.timestamp  # non-empty ISO string
        assert prov.plan_path.endswith("plan.md")
        assert len(prov.plan_content_sha256) == 64  # sha256 hex digest
        assert isinstance(prov.worktree_dirty, bool)
        assert len(prov.index_fingerprint) == 64

    def test_same_plan_repo_commit_and_index_produce_the_same_fingerprint(self, tmp_path):
        recorder_a = TraceRecorder()
        _compute(tmp_path / "a", "pure-backend", None, {}, recorder_a)
        recorder_b = TraceRecorder()
        _compute(tmp_path / "b", "pure-backend", None, {}, recorder_b)
        assert (
            recorder_a.trace.provenance.index_fingerprint
            == recorder_b.trace.provenance.index_fingerprint
        )

    def test_changing_only_operational_counters_leaves_the_fingerprint_unchanged(self, tmp_path):
        """Plan U5: the fingerprint is a content hash of the logical export, not
        of `status -j`'s operational counters — changing only ``nodeCount``/
        ``edgeCount`` (queue depth, uptime, cache size in the real CLI) with the
        query results held fixed must not move the fingerprint. This replaces
        the old status-counter-driven expectation this test used to encode,
        which was exactly the bug plan U5 fixes."""
        from orchestrator.grouping.graphing import CodegraphClient
        from tests.test_grouping_fixtures import (
            STATUS_JSON,
            STATUS_JSON_RESYNCED,
            stub_codegraph_runner,
        )
        from tests.test_grouping_fixtures import make_repo as make_stub_repo

        repo, plan = make_stub_repo(tmp_path, "pure-backend")

        recorder_before = TraceRecorder()
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=CodegraphClient(
                repo_root=repo, runner=lambda a: stub_codegraph_runner(a, STATUS_JSON)
            ),
            recorder=recorder_before,
        )

        recorder_after = TraceRecorder()
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=CodegraphClient(
                repo_root=repo, runner=lambda a: stub_codegraph_runner(a, STATUS_JSON_RESYNCED)
            ),
            recorder=recorder_after,
        )

        assert (
            recorder_before.trace.provenance.index_fingerprint
            == recorder_after.trace.provenance.index_fingerprint
        )

    def test_changing_the_query_result_changes_the_fingerprint(self, tmp_path):
        """The mirror of the test above: real content — a symbol appearing in
        the bulk `query ""` result — must move the fingerprint even though
        `status -j` is held fixed."""
        from orchestrator.grouping.graphing import CodegraphClient
        from tests.test_grouping_fixtures import STATUS_JSON, stub_codegraph_runner
        from tests.test_grouping_fixtures import make_repo as make_stub_repo

        repo, plan = make_stub_repo(tmp_path, "pure-backend")
        new_symbol = [
            {
                "node": {
                    "id": "function:abc123",
                    "kind": "function",
                    "name": "new_fn",
                    "qualifiedName": "new_fn",
                    "filePath": "server.py",
                    "signature": "def new_fn(): ...",
                }
            }
        ]

        recorder_before = TraceRecorder()
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=CodegraphClient(
                repo_root=repo,
                runner=lambda a: stub_codegraph_runner(a, STATUS_JSON, query_result=[]),
            ),
            recorder=recorder_before,
        )

        recorder_after = TraceRecorder()
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=CodegraphClient(
                repo_root=repo,
                runner=lambda a: stub_codegraph_runner(a, STATUS_JSON, query_result=new_symbol),
            ),
            recorder=recorder_after,
        )

        assert (
            recorder_before.trace.provenance.index_fingerprint
            != recorder_after.trace.provenance.index_fingerprint
        )


class TestAcceptedOvershootRecorded:
    """The one existing 'accepted overshoot' mechanism ahead of U6's
    --allow-oversized-slice gate: repair_cycles (plan U5) keeps a group whole
    over budget when no acyclic re-split exists, appending a flags[] entry.
    The trace must carry that same entry, both in its repairs[] section and in
    the flags mirrored from the partitioner (plan U9: "as well as in flags[]")."""

    OVERSHOOT_PLAN = """# feat: repair overshoot repro

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: a1
    description: a1
    slice: s
    files: [a1.py]
    depends_on: []
  - task_id: b
    description: b
    files: [b.py]
    depends_on: [a1]
  - task_id: a2
    description: a2
    slice: s
    files: [a2.py]
    depends_on: [b]
```
"""

    def test_repair_overshoot_lands_in_both_flags_and_the_trace(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(self.OVERSHOOT_PLAN)
        config = OrchestratorConfig()
        config.estimator.token_budget = 4300  # tight enough to force repair overshoot
        # U6 landed after this test was written (its docstring says "ahead of U6's
        # gate"). Slice 's' is a declared slice that overshoots, so the U6 gate now
        # raises before repair_cycles can record anything. Accept the overshoot so
        # the U5 repair path this test actually targets still runs.
        config.partition.allow_oversized_slice = True
        # Likewise, the degeneracy gate landed after this test: an unre-splittable
        # repair overshoot is now a hard error by default. This test exists to prove
        # the overshoot is *recorded* when accepted, so it opts into accepting it.
        config.partition.allow_degenerate_partition = True
        recorder = TraceRecorder()
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=config,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
            recorder=recorder,
        )
        assert outcome.last_stage == "repair"
        assert any("stays" in flag and "over the" in flag for flag in outcome.flags)
        overshoot_flag = next(flag for flag in outcome.flags if "stays" in flag)

        assert overshoot_flag in recorder.trace.partition_flags
        assert overshoot_flag in recorder.trace.flags
        assert any(overshoot_flag in repair.overshoots for repair in recorder.trace.repairs)


class TestRunGroupingFillsDifficulty:
    def test_full_path_fills_groups_with_signals_score_and_thresholds(self, tmp_path):
        recorder = TraceRecorder()
        repo, plan = make_toy_repo(tmp_path)
        config = OrchestratorConfig()
        result, _base_context = run_grouping(
            plan_path=plan,
            repo_root=repo,
            config=config,
            llm_runner=StubLlm(),
            client=make_client(repo),
            recorder=recorder,
        )
        assert recorder.trace.groups, "run_grouping must fill the trace's per-group section"
        recorded_ids = {entry.group_id for entry in recorder.trace.groups}
        assert recorded_ids == {group.id for group in result.groups}
        for entry, group in zip(
            sorted(recorder.trace.groups, key=lambda e: e.group_id),
            sorted(result.groups, key=lambda g: g.id),
        ):
            assert entry.difficulty == pytest.approx(group.difficulty)
            assert entry.intensity == group.intensity.value
            assert entry.d_review == config.difficulty.d_review
            assert entry.d_hard == config.difficulty.d_hard

    def test_partition_only_path_leaves_groups_empty(self, tmp_path):
        recorder = TraceRecorder()
        _compute(tmp_path, "greenfield-cross-stack", None, {}, recorder)
        assert recorder.trace.groups == []
