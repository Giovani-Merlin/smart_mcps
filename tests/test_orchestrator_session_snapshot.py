"""U30 tests: the orchestrator's own sessions — the base session and a
rewrite's speccer call — appear on the board alongside the coder/reviewer
sessions they drive.

Neither is a tracked ``SessionEntry``: ``start_base`` never calls
``record_session``, and ``_rewrite``'s one-shot ``claude -p`` call is recorded
only to the run's ``llm/calls.json`` (plan U14), never joined to a group id
there. ``build_snapshot`` therefore synthesizes both from what U14 already
persists on disk: ``manifest.base_session_id`` for the base session, and a
group's ``spec-gen<N>.json`` files for its rewrite calls.

The base session is attached to every group's own attempt history at
generation 1 — the same relationship a rewrite has to the generation it
produces, since every group's first coder is a fork of it — so a group that
was never re-specced still carries exactly one orchestrator row: the base
session.
"""

from __future__ import annotations

import json

from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.model import GroupManifestEntry, RunManifest, SessionEntry, SessionRole
from orchestrator.observatory.runs import build_snapshot


def _seed_manifest(tmp_path, *, with_base=True) -> RunPaths:
    paths = RunPaths(tmp_path, "r1")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    manifest = RunManifest(
        run_id="r1",
        plan_path="plan.md",
        base_session_id="base-sess-1" if with_base else None,
    )
    manifest.groups["g1"] = GroupManifestEntry(
        group_id="g1",
        group_name="g1",
        summary="",
        sessions=[
            SessionEntry(
                session_id="s1", role=SessionRole.CODER, generation=1, name="r1-g1-coder-g1"
            ),
            SessionEntry(
                session_id="s2", role=SessionRole.CODER, generation=2, name="r1-g1-coder-g2"
            ),
        ],
    )
    manifest.groups["g2"] = GroupManifestEntry(
        group_id="g2",
        group_name="g2",
        summary="",
        sessions=[
            SessionEntry(
                session_id="s3", role=SessionRole.CODER, generation=1, name="r1-g2-coder-g1"
            ),
        ],
    )
    ManifestStore(paths).save(manifest)
    return paths


def test_base_session_is_exposed_with_an_orchestrator_role(tmp_path):
    paths = _seed_manifest(tmp_path)
    snapshot = build_snapshot(paths, "proj")
    assert snapshot.base_session is not None
    assert snapshot.base_session.session_id == "base-sess-1"
    assert snapshot.base_session.role == "orchestrator"
    assert snapshot.base_session.name == "r1-base"


def test_no_base_session_id_serves_a_null_base_session(tmp_path):
    paths = _seed_manifest(tmp_path, with_base=False)
    snapshot = build_snapshot(paths, "proj")
    assert snapshot.base_session is None


def test_every_group_carries_the_base_session_at_generation_one(tmp_path):
    paths = _seed_manifest(tmp_path)
    snapshot = build_snapshot(paths, "proj")
    for gid in ("g1", "g2"):
        group = next(group for group in snapshot.groups if group.group_id == gid)
        base_rows = [s for s in group.sessions if s.role == "orchestrator"]
        assert len(base_rows) == 1
        assert base_rows[0].session_id == "base-sess-1"
        assert base_rows[0].generation == 1
        # positioned ahead of the group's own first-generation coder session
        assert group.sessions[0] is base_rows[0]


def test_rewrite_call_is_positioned_before_the_generation_it_produced(tmp_path):
    paths = _seed_manifest(tmp_path)
    group_dir = paths.group_dir("g1")
    group_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(group_dir / "spec-gen2.json", json.dumps({"spec": "rewritten"}))

    snapshot = build_snapshot(paths, "proj")
    g1 = next(group for group in snapshot.groups if group.group_id == "g1")
    roles = [(s.role, s.generation) for s in g1.sessions]

    orchestrator_index = roles.index(("orchestrator", 2))
    coder_gen2_index = next(
        i for i, (role, gen) in enumerate(roles) if role == "coder" and gen == 2
    )
    assert orchestrator_index < coder_gen2_index
    # generation-1 sessions (base session, then coder) are untouched and still
    # precede everything from generation 2.
    assert roles[0] == ("orchestrator", 1)
    assert roles[1] == ("coder", 1)


def test_group_never_rewritten_shows_no_orchestrator_rows_beyond_the_base_session(tmp_path):
    paths = _seed_manifest(tmp_path)
    snapshot = build_snapshot(paths, "proj")
    for gid in ("g1", "g2"):
        group = next(group for group in snapshot.groups if group.group_id == gid)
        orchestrator_rows = [s for s in group.sessions if s.role == "orchestrator"]
        assert [row.session_id for row in orchestrator_rows] == ["base-sess-1"]


def test_a_group_directory_that_does_not_exist_yields_no_rewrite_rows(tmp_path):
    paths = _seed_manifest(tmp_path)
    snapshot = build_snapshot(paths, "proj")
    g1 = next(group for group in snapshot.groups if group.group_id == "g1")
    assert [s.role for s in g1.sessions] == ["orchestrator", "coder", "coder"]
