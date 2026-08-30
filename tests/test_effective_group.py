"""The spec-gen overlay every restart path resolves groups through.

A speccer rewrite returns an in-memory ``Group`` carrying a new name/spec while
``groups.json`` stays the immutable grouper output; the durable record is
``groups/<gid>/spec-gen<N>.json``. Run r20260830-163212 proved that any restart
path reading ``groups.json`` bare re-derives the stale name and dies (resume) or
leaks the worktree (finish/retry). ``effective_group`` is the shared resolver;
these tests pin its selection rule and its never-fail contract.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.execution.manifest import RunPaths, atomic_write_text, effective_group
from orchestrator.model import Group, ReviewIntensity


def make_group(gid: str = "g1") -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
    )


def write_spec_gen(paths: RunPaths, group: Group, generation: int, **update) -> Group:
    rewritten = group.model_copy(update=update)
    atomic_write_text(
        paths.group_dir(group.id) / f"spec-gen{generation}.json",
        rewritten.model_dump_json(indent=2) + "\n",
    )
    return rewritten


def test_returns_the_original_when_no_rewrite_exists(tmp_path: Path):
    paths = RunPaths(tmp_path, "r1")
    group = make_group()
    # neither the run dir nor the group dir exists yet — the fresh-run case
    assert effective_group(paths, group) is group


def test_picks_the_highest_generation(tmp_path: Path):
    paths = RunPaths(tmp_path, "r1")
    group = make_group()
    write_spec_gen(paths, group, 1, name="rewrite one")
    latest = write_spec_gen(paths, group, 2, name="rewrite two", spec="new spec")
    # generation 10 beats 2 numerically, not lexically ("10" < "2" as strings)
    newest = write_spec_gen(paths, group, 10, name="rewrite ten")

    resolved = effective_group(paths, group)
    assert resolved.name == newest.name
    assert resolved.id == group.id
    assert resolved != latest


def test_falls_back_on_an_unparsable_file(tmp_path: Path):
    paths = RunPaths(tmp_path, "r1")
    group = make_group()
    path = paths.group_dir(group.id) / "spec-gen1.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json {")
    assert effective_group(paths, group) is group


def test_ignores_files_that_do_not_match_the_convention(tmp_path: Path):
    paths = RunPaths(tmp_path, "r1")
    group = make_group()
    group_dir = paths.group_dir(group.id)
    group_dir.mkdir(parents=True)
    (group_dir / "spec-genX.json").write_text("{}")
    (group_dir / "report-g1-r1.json").write_text("{}")
    assert effective_group(paths, group) is group
