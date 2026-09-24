"""Integration-branch merges: approved groups land in dependency order (plan U8).

Each run owns ``orchestrator/run-<run_id>``, created from the launch ref and
checked out in its own worktree so the operator's checkout is never disturbed.
Approved group branches merge with ``--no-ff`` (one merge commit per group — the
audit trail). A conflict aborts cleanly, leaves the integration branch untouched,
and escalates as a MergeConflict surprise naming the incoming group and the
already-merged groups whose files collide; the review loop routes the group to
rewriting and dependents stay paused (plan Key Technical Decisions). The final
merge to the main branch is manual — this module never touches it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from orchestrator.config import ExecutionConfig, PreflightConfig, WorkspaceConfig
from orchestrator.execution.preflight import PreflightBaseline, run_preflight
from orchestrator.execution.worktrees import (
    IGNORED_OUTPUTS_DIRNAME,
    WorktreeError,
    WorktreeRefreshConflict,
    _git,
    _git_ok,
    _refresh_onto_tip,
    create_worktree,
    integration_branch,
    materialize_data_layer,
    provision_env,
    provision_node_env,
    remove_worktree,
    rescue_ignored_outputs,
    write_provisioning_record,
)
from orchestrator.model import Group


class MergeConflict(Exception):
    """Raised by the merge seam (U8). Routes the merging group to rewriting and
    fans a surprise out to the groups involved."""

    def __init__(self, message: str, affected_groups: list[str] | None = None):
        super().__init__(message)
        self.affected_groups = affected_groups or []


class MergeError(Exception):
    """A git operation failed for a reason other than a content conflict."""


#: Files whose change means the environment built from them is now stale.
_ENV_MANIFESTS = ("pyproject.toml", "uv.lock", "package.json", "package-lock.json")


def _is_env_manifest(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in _ENV_MANIFESTS


def commits_ahead(worktree: Path, base: str, branch: str) -> int:
    """Commits on ``branch`` not yet reachable from ``base``.

    Must be read *before* a merge, not after (plan U1): once ``branch`` merges
    cleanly its commits are reachable from ``base`` too, so this same count reads
    zero afterwards — a post-merge check cannot tell a real merge from a no-op.
    """
    return int(_git_ok(worktree, "rev-list", "--count", f"{base}..{branch}").strip())


class IntegrationMerger:
    """Owns the per-run integration branch; matches the ReviewDeps.merge_group seam."""

    def __init__(
        self,
        repo_root: Path,
        run_id: str,
        launch_ref: str = "HEAD",
        *,
        preflight_config: PreflightConfig | None = None,
        preflight_baseline: PreflightBaseline | None = None,
        preflight_output_dir: Callable[[str], Path] | None = None,
        log: Callable[[str], None] | None = None,
        provision_args: list[str] | None = None,
        provision_env_vars: dict[str, str] | None = None,
        provision_strict: bool = False,
        workspace: WorkspaceConfig | None = None,
    ):
        self.repo_root = repo_root
        self.run_id = run_id
        self.launch_ref = launch_ref
        self.branch = integration_branch(run_id)
        self.merged: list[Group] = []
        # Approvals of independent groups can land concurrently; merges serialize.
        self._lock = threading.Lock()
        self._preflight_config = preflight_config or PreflightConfig()
        # What was already red on the launch branch. Preflight compares against
        # it so a tree whose only failures are pre-existing still merges; None
        # (no baseline captured) keeps the strict exit-code gate.
        self._preflight_baseline = preflight_baseline
        # Defaults keep every existing in-process test (constructed with no
        # RunPaths at all) byte-identical: check output lands under the repo's
        # own `.orchestrator/` tree, keyed by run and group, same shape as
        # RunPaths.group_dir without requiring one.
        self._preflight_output_dir = preflight_output_dir or (
            lambda gid: repo_root / ".orchestrator" / "runs" / run_id / "groups" / gid
        )
        self._log = log or (lambda _text: None)
        # The integration worktree is the tree that represents the run's output
        # (plan U32) — an operator naturally goes there to run what was just
        # built, so it is provisioned exactly like a group worktree, once.
        self._provision_args = provision_args or []
        self._provision_env_vars = provision_env_vars
        self._provisioned = False
        self._provision_lock = threading.Lock()
        # `provision_on_failure = "fail"`: a sync failure here raises out of
        # `ensure()` at run start — the earliest possible moment, before any
        # group has a worktree — instead of being a warning nobody reads.
        self._provision_strict = provision_strict
        self._workspace = workspace

    def set_preflight_baseline(self, baseline: PreflightBaseline | None) -> None:
        """Install the baseline after construction (F1, r20260924): a fresh run
        captures it in the integration worktree, which only exists once
        ``ensure()`` has run — so the merger is built first, with no baseline,
        and told about it here. The launch reads the saved file back rather
        than passing the object through, so the merger holds exactly what a
        resume would load."""
        self._preflight_baseline = baseline

    def ensure(self) -> Path:
        """Create (or reuse) the integration branch and its worktree. Idempotent."""
        path = create_worktree(
            self.repo_root,
            run_id=self.run_id,
            group_id="integration",
            name="integration",
            branch=self.branch,
            start_point=self.launch_ref,
            workspace=self._workspace,
            log=self._log,
        )
        self._provision_once(path)
        return path

    def _provision_once(self, path: Path) -> None:
        """Provision the integration worktree the first time ``ensure()`` sees
        it exist (plan U32). Guarded by its own lock, never ``self._lock``:
        ``tip()`` calls ``ensure()`` while already holding ``self._lock``, so
        reacquiring it here would deadlock. Attempted exactly once per process
        regardless of outcome — a failed sync is recorded and reported, not
        retried on every subsequent ``tip()`` call."""
        with self._provision_lock:
            if self._provisioned:
                return
            self._provisioned = True
        group_dir = self._preflight_output_dir("integration")

        detail: dict[str, str] = {}

        def _record(state: str, argv: list[str]) -> None:
            write_provisioning_record(
                group_dir, worktree=path, command=argv, state=state, detail=detail.get("text", "")
            )

        provision_env(
            path,
            log=self._log,
            env=self._provision_env_vars,
            extra_args=self._provision_args,
            on_state=_record,
            on_detail=lambda text: detail.__setitem__("text", text),
            strict=self._provision_strict,
        )
        # The JavaScript half: the integration worktree runs the same merge
        # gate a group's does, and its UI steps need `ui/node_modules` too. No
        # `on_state` — the provisioning record holds one command, the venv's.
        provision_node_env(
            path,
            log=self._log,
            env=self._provision_env_vars,
            frontend_dirs=self._preflight_config.frontend_dirs,
        )

    def _reprovision_if_manifests_changed(self, path: Path, group_id: str, *, since: str) -> None:
        """Re-sync the integration environment when a merge changed what it is
        built from (plan: run-notes sweep B). ``_provision_once`` runs once per
        process, so a group that added a dependency used to leave the venv
        stale for every later gate run and for the driver (yt-dlp missing,
        twice, on drummAI). Never strict: the merge already landed, and a
        failed re-sync is a logged problem for the next gate to surface, not a
        reason to fail a merge that already happened."""
        changed = _git_ok(path, "diff", "--name-only", since, "HEAD").splitlines()
        manifests = [name for name in changed if _is_env_manifest(name)]
        if not manifests:
            return
        self._log(
            f"integration worktree re-provisioned after merge({group_id}): "
            f"{', '.join(sorted(manifests))} changed"
        )
        provision_env(
            path,
            log=self._log,
            env=self._provision_env_vars,
            extra_args=self._provision_args,
            strict=False,
        )
        provision_node_env(
            path,
            log=self._log,
            env=self._provision_env_vars,
            frontend_dirs=self._preflight_config.frontend_dirs,
        )

    def tip(self) -> str:
        """Current integration-branch commit — the branch point for a group's
        worktree at its ready→running transition, and the reviewer's diff base."""
        with self._lock:
            self.ensure()
            return _git_ok(self.repo_root, "rev-parse", self.branch).strip()

    def merge_group(self, group: Group, worktree: Path) -> str:
        """Merge an approved group's branch; raises MergeConflict on collision.
        Returns the merge commit sha (plan U6: the Artifact Manifest's `commit`).

        Refreshes the group worktree onto the current integration tip, runs
        Preflight on that refreshed tree, and only then merges — all under one
        acquisition of ``self._lock`` (plan U4): the tree Preflight checks is
        the tree that ships, and a second ``merge_group`` call for another
        group cannot interleave between the refresh and the merge. A textual
        conflict during the refresh raises ``MergeConflict`` (the existing
        conflict ladder), not a Preflight failure — Preflight never runs on a
        tree the refresh itself could not produce.
        """
        with self._lock:
            integration_wt = self.ensure()
            branch = _git_ok(worktree, "branch", "--show-current").strip()
            if not branch:
                raise MergeError(f"worktree {worktree} is not on a branch")
            try:
                _refresh_onto_tip(worktree, group_id=group.id, branch=branch, tip=self.branch)
            except WorktreeRefreshConflict as exc:
                raise MergeConflict(
                    f"refreshing {group.id} ({branch}) onto {self.branch} conflicted on: "
                    f"{', '.join(exc.paths) or 'unknown files'}",
                    affected_groups=[group.id, *self._groups_owning(exc.paths)],
                ) from exc
            ahead = commits_ahead(integration_wt, self.branch, branch)
            if ahead == 0:
                raise MergeError(
                    f"group {group.id} branch {branch} has no commits ahead of "
                    f"{self.branch} — refusing to merge nothing"
                )
            run_preflight(
                worktree,
                config=self._preflight_config,
                output_dir=self._preflight_output_dir(group.id),
                log=self._log,
                declared_files=group.files,
                baseline=self._preflight_baseline,
                uv_run_args=self._provision_args,
            )
            message = f"merge({self.run_id}): {group.id} {group.name}"
            tip_before = _git_ok(integration_wt, "rev-parse", "HEAD").strip()
            result = _git(integration_wt, "merge", "--no-ff", "-m", message, branch)
            if result.returncode != 0:
                conflicted = _git_ok(
                    integration_wt, "diff", "--name-only", "--diff-filter=U"
                ).splitlines()
                _git(integration_wt, "merge", "--abort")
                raise MergeConflict(
                    f"merging {group.id} ({branch}) conflicted on: "
                    f"{', '.join(conflicted) or 'unknown files'}",
                    affected_groups=[group.id, *self._groups_owning(conflicted)],
                )
            merge_sha = _git_ok(integration_wt, "rev-parse", "HEAD").strip()
            self.merged.append(group)
            # A group may have registered a new large file mid-run; the
            # integration tree gets its link now rather than at the next ensure().
            materialize_data_layer(integration_wt, self.repo_root, self._workspace, log=self._log)
            self._reprovision_if_manifests_changed(integration_wt, group.id, since=tip_before)
            rescue_ignored_outputs(
                worktree,
                self._preflight_output_dir(group.id) / IGNORED_OUTPUTS_DIRNAME,
                cap_bytes=ExecutionConfig().review_scratch_cap_bytes,
                log=self._log,
            )
            try:
                # Cleanup only after a clean merge; a dirty worktree (uncommitted
                # leftovers) is left in place for inspection rather than forced.
                remove_worktree(self.repo_root, worktree)
            except WorktreeError:
                pass
            return merge_sha

    def _groups_owning(self, paths: list[str]) -> list[str]:
        """Already-merged groups whose declared files collide with the conflict."""
        conflicted = set(paths)
        return [g.id for g in self.merged if conflicted.intersection(g.files)]
