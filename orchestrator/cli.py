"""smart-mcps-orchestrate CLI.

U4 ships the `group` command with `--dry-run` as the human checkpoint before any
execution; U9 adds `run` / `status` / `resume` on top.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.config import load_config
from orchestrator.grouping.graphing import CodegraphClient, GraphBuildError
from orchestrator.grouping.llm import JsonRunner, LlmError
from orchestrator.grouping.partition import GroupCycleError
from orchestrator.grouping.pipeline import GrouperError, run_grouping, serialize_grouping
from orchestrator.model import GroupingResult


def main(
    argv: list[str] | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
) -> int:
    """Entry point. ``llm_runner`` and ``client`` are injectable for offline tests."""
    parser = argparse.ArgumentParser(prog="smart-mcps-orchestrate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    group_cmd = subparsers.add_parser("group", help="compute groups from a plan document")
    group_cmd.add_argument("plan", type=Path, help="path to the plan document")
    group_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")
    group_cmd.add_argument("--config", type=Path, default=None, help="config TOML path")
    group_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print groups, DAG, and estimates without writing artifacts",
    )

    args = parser.parse_args(argv)
    if args.command == "group":
        return _cmd_group(args, llm_runner, client)
    parser.error(f"unknown command {args.command!r}")
    return 2


def _cmd_group(
    args: argparse.Namespace,
    llm_runner: JsonRunner | None,
    client: CodegraphClient | None,
) -> int:
    repo_root = args.repo.resolve()
    config_path = args.config or repo_root / ".orchestrator" / "config.toml"
    config = load_config(config_path)
    try:
        result, base_context = run_grouping(
            plan_path=args.plan,
            repo_root=repo_root,
            config=config,
            llm_runner=llm_runner,
            client=client,
        )
    except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        _print_report(result)
        return 0

    out_dir = repo_root / ".orchestrator"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "groups.json").write_text(serialize_grouping(result))
    (out_dir / "base-context.md").write_text(base_context)
    print(f"wrote {out_dir / 'groups.json'} and {out_dir / 'base-context.md'}")
    _print_report(result)
    return 0


def _print_report(result: GroupingResult) -> None:
    print(f"plan: {result.plan_path}")
    print(f"groups: {len(result.groups)}")
    for group in result.groups:
        deps = ", ".join(group.dependencies) if group.dependencies else "none"
        print(f"\n{group.id}: {group.name}")
        print(f"  summary: {group.summary}")
        print(f"  tasks: {', '.join(group.tasks)}")
        print(f"  files: {', '.join(group.files) if group.files else 'none'}")
        print(f"  est. tokens: {group.estimated_tokens}")
        print(f"  difficulty: {group.difficulty:.2f} → {group.intensity.value}")
        print(f"  depends on: {deps}")
        print(f"  verification: {len(group.verification)} item(s)")
    if result.flags:
        print("\nflags:")
        for flag in result.flags:
            print(f"  - {flag}")


if __name__ == "__main__":
    sys.exit(main())
