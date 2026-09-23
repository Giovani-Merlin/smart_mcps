"""Layout guard for the review-loop split (plan U1 and on).

Every later unit adds a mixin to `_GroupExecution`; this test is the
mechanical check every one of them must keep passing: the new modules import
without a cycle, each mixin's declared reads exist on the host, every host
attribute the mixins rely on is actually set by `_GroupExecution.__init__`,
and no two mixins in the MRO define the same method (a silent override would
mean one of them dead code).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from orchestrator.execution.host import ExecutionHost
from orchestrator.execution.review import _GroupExecution
from orchestrator.execution.escalating import EscalationHandlers
from orchestrator.execution.merge_ladder import MergeLadder
from orchestrator.execution.records import SessionRecords
from orchestrator.execution.reviewer import ReviewerRound
from orchestrator.execution.surprises import SurpriseHandling
from tests.test_review_loop import Harness, StubRunner, make_group

MIXINS = [MergeLadder, EscalationHandlers, SurpriseHandling, SessionRecords, ReviewerRound]


def test_new_modules_import_with_no_cycle():
    """Each module imports cleanly on its own, in either order — a real cycle
    would raise ImportError/AttributeError on a partially-initialized module."""
    for order in (
        [
            "orchestrator.execution.host",
            "orchestrator.execution.surprises",
            "orchestrator.execution.escalating",
            "orchestrator.execution.merge_ladder",
            "orchestrator.execution.records",
            "orchestrator.execution.reviewer",
            "orchestrator.execution.review",
        ],
        [
            "orchestrator.execution.review",
            "orchestrator.execution.reviewer",
            "orchestrator.execution.records",
            "orchestrator.execution.merge_ladder",
            "orchestrator.execution.escalating",
            "orchestrator.execution.surprises",
            "orchestrator.execution.host",
        ],
    ):
        script = "; ".join(f"import {name}" for name in order)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_mixin_annotations_name_a_host_attribute_or_method():
    # Raw annotation names only — not resolved types: `ExecutionHost`'s
    # annotations reference types imported under `TYPE_CHECKING`, which are
    # absent from the module's runtime globals, so `get_type_hints` would
    # raise trying to evaluate the (stringified, PEP 563) forward refs.
    host_hints = set(ExecutionHost.__annotations__)
    for mixin in MIXINS:
        mixin_annotations = getattr(mixin, "__annotations__", {})
        for name in mixin_annotations:
            assert name in host_hints or hasattr(ExecutionHost, name), (
                f"{mixin.__name__}.{name} names neither an ExecutionHost data "
                f"attribute nor an ExecutionHost method"
            )


def test_every_host_attribute_is_set_by_group_execution_init(tmp_path):
    runner = StubRunner({})
    harness = Harness(tmp_path, runner)
    group = make_group()
    ctx = harness.context(group)
    execution = _GroupExecution(harness.deps, ctx)
    instance_vars = vars(execution)
    for name in ExecutionHost.__annotations__:
        assert name in instance_vars, f"ExecutionHost.{name} is never assigned in __init__"


def test_no_method_name_collides_across_mixins():
    seen: dict[str, type] = {}
    for mixin in MIXINS:
        for name, value in vars(mixin).items():
            if name.startswith("__") or not callable(value):
                continue
            if name in seen and seen[name] is not mixin:
                raise AssertionError(
                    f"{name} is defined on both {seen[name].__name__} and {mixin.__name__}"
                )
            seen[name] = mixin


def test_review_module_reexports_are_the_documented_transitional_set():
    """Guards the U7 cleanup: these are exactly the names review.py must stop
    defining once every unit has moved its piece out."""
    import orchestrator.execution.review as review

    for name in (
        "SurpriseBoard",
        "SurpriseHandling",
        "surprise_residue",
        "format_residue_report",
        "REASON_GROUP_COMPLETED",
        "REASON_UNKNOWN_GROUP",
        "REASON_RUN_ENDED",
        "MergeConflict",
    ):
        assert hasattr(review, name)


def test_execution_modules_import_graph_is_acyclic():
    """Static import graph over the split modules; `merge_ladder -> escalating`
    is the one allowed inter-mixin edge. `TYPE_CHECKING` imports are skipped."""
    import ast

    names = ["surprises", "merge_ladder", "escalating", "records", "reviewer", "host", "review"]
    root = Path(__file__).resolve().parents[1] / "orchestrator" / "execution"
    graph: dict[str, set[str]] = {}
    for name in names:
        tree = ast.parse((root / f"{name}.py").read_text())
        deps: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module.rsplit(".", 1)[-1]
                if node.module.startswith("orchestrator.execution") and mod in names:
                    deps.add(mod)
        graph[name] = deps
    assert graph["merge_ladder"] & {"surprises", "escalating"} == {"escalating"}
    assert graph["escalating"] == set()
    assert graph["surprises"] == set()
    assert graph["records"] == set()
    assert graph["reviewer"] == set()

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        assert node not in visiting, f"import cycle through {node}"
        if node in done:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for name in names:
        visit(name)
