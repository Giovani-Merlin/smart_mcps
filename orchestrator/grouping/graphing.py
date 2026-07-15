"""codegraph affinity adapter: task→region mappings → the TaskGraph U1 consumes.

Affinity edges come from real codegraph relations — shared files, call-graph
proximity, impact overlap — not symbol-name cosine (CoCoder's proxy, needed only
because its code didn't exist at graph time; see docs/research/cocoder-analysis.md
§7 point 3). Directed call and impact relations also become dependency edges:
the caller's task depends on the callee's task, and a task reading what another
task's write surface impacts depends on that task.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.grouping.partition import Pair, TaskGraph, canonical_pair

# The CLI defaults to 20 results, which would silently truncate hub fan-in counts
# (plan U2); every caller/callee query passes this explicit high limit instead.
CALL_QUERY_LIMIT = 1000
IMPACT_DEPTH = 2

# Full CLI argv → stdout. Injected in tests to replay captured fixture output.
Runner = Callable[[Sequence[str]], str]


class GraphBuildError(Exception):
    """codegraph output could not be turned into a task graph."""


@dataclass(frozen=True)
class TaskMapping:
    """One plan task mapped to code regions (files and symbols)."""

    task_id: str
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeWeights:
    """Configurable weights for the three affinity signals (plan R3)."""

    shared_file: float = 1.0
    call: float = 2.0
    impact: float = 1.5


@dataclass
class CodegraphClient:
    """Thin wrapper over the codegraph CLI's JSON output."""

    repo_root: Path
    runner: Runner | None = None
    call_limit: int = CALL_QUERY_LIMIT
    impact_depth: int = IMPACT_DEPTH

    def callers(self, symbol: str) -> list[dict]:
        args = ["callers", symbol, "-j", "-l", str(self.call_limit)]
        return self._query(args, expect_key="callers")

    def callees(self, symbol: str) -> list[dict]:
        args = ["callees", symbol, "-j", "-l", str(self.call_limit)]
        return self._query(args, expect_key="callees")

    def impact(self, symbol: str) -> list[dict]:
        args = ["impact", symbol, "-j", "-d", str(self.impact_depth)]
        return self._query(args, expect_key="affected")

    def query(self, search: str) -> list[dict]:
        """Symbol search; the CLI returns a JSON *array* of {node, score} entries."""
        raw = self._run(["query", search, "-j", "-l", str(self.call_limit)])
        if not raw.strip():
            raise GraphBuildError(f"codegraph query {search!r} produced empty output")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphBuildError(
                f"codegraph query {search!r} produced invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, list):
            raise GraphBuildError(f"codegraph query {search!r} output is not a list")
        return payload

    def symbol_exists(self, name: str) -> bool:
        """Exact-name check used to verify mapper output against the index (R2)."""
        return any(entry.get("node", {}).get("name") == name for entry in self.query(name))

    def files_overview(self) -> str:
        """Raw `codegraph files` output — the architecture summary for base context."""
        return self._run(["files"])

    def _query(self, args: Sequence[str], expect_key: str) -> list[dict]:
        raw = self._run(args)
        if not raw.strip():
            raise GraphBuildError(f"codegraph {args[0]} {args[1]!r} produced empty output")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphBuildError(
                f"codegraph {args[0]} {args[1]!r} produced invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict) or expect_key not in payload:
            raise GraphBuildError(
                f"codegraph {args[0]} {args[1]!r} output is missing the {expect_key!r} key"
            )
        entries = payload[expect_key]
        if not isinstance(entries, list):
            raise GraphBuildError(f"codegraph {args[0]} {args[1]!r} {expect_key!r} is not a list")
        return entries

    def _run(self, args: Sequence[str]) -> str:
        if self.runner is not None:
            return self.runner(args)
        result = subprocess.run(
            ["codegraph", *args, "-p", str(self.repo_root)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GraphBuildError(
                f"codegraph {args[0]} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout


@dataclass
class _EdgeAccumulator:
    affinity: dict[Pair, float] = field(default_factory=dict)
    dependencies: dict[Pair, float] = field(default_factory=dict)

    def add(self, upstream: str, downstream: str, weight: float) -> None:
        pair = canonical_pair(upstream, downstream)
        self.affinity[pair] = self.affinity.get(pair, 0.0) + weight
        key = (upstream, downstream)
        self.dependencies[key] = self.dependencies.get(key, 0.0) + weight

    def add_symmetric(self, a: str, b: str, weight: float) -> None:
        pair = canonical_pair(a, b)
        self.affinity[pair] = self.affinity.get(pair, 0.0) + weight


def build_task_graph(
    mappings: Sequence[TaskMapping],
    client: CodegraphClient,
    weights: EdgeWeights | None = None,
) -> TaskGraph:
    """Query codegraph for every mapped symbol and assemble the weighted task graph.

    Region-less tasks (unmappable plan tasks carried as prose-only nodes, plan U4)
    become isolated nodes; the mapper's prose-affinity fallback adds their edges.
    """
    weights = weights or EdgeWeights()
    ids = [m.task_id for m in mappings]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise GraphBuildError(f"duplicate task ids in mappings: {sorted(duplicates)}")

    file_owner: dict[str, set[str]] = {}
    symbol_owner: dict[str, set[str]] = {}
    for mapping in mappings:
        for file in mapping.files:
            file_owner.setdefault(file, set()).add(mapping.task_id)
        for symbol in mapping.symbols:
            symbol_owner.setdefault(symbol, set()).add(mapping.task_id)

    def owners_of(entry: dict) -> set[str]:
        owners = set(symbol_owner.get(entry.get("name", ""), ()))
        owners |= file_owner.get(entry.get("filePath", ""), set())
        return owners

    edges = _EdgeAccumulator()

    # Shared-file affinity: weight scales with the number of files two tasks share.
    for _file, owners in sorted(file_owner.items()):
        owner_list = sorted(owners)
        for i, a in enumerate(owner_list):
            for b in owner_list[i + 1 :]:
                edges.add_symmetric(a, b, weights.shared_file)

    metadata: dict[str, dict[str, object]] = {}
    seen_calls: set[tuple[str, str, str, str]] = set()
    seen_impacts: set[tuple[str, str, str, str]] = set()
    for mapping in sorted(mappings, key=lambda m: m.task_id):
        task = mapping.task_id
        fan_in = fan_out = 0
        max_symbol_fan_in = max_symbol_fan_out = 0
        for symbol in sorted(mapping.symbols):
            callers = client.callers(symbol)
            callees = client.callees(symbol)
            fan_in += len(callers)
            fan_out += len(callees)
            max_symbol_fan_in = max(max_symbol_fan_in, len(callers))
            max_symbol_fan_out = max(max_symbol_fan_out, len(callees))

            # Call proximity: the caller's task depends on the callee's task.
            for caller in callers:
                for other in owners_of(caller) - {task}:
                    key = (task, other, symbol, caller.get("name", ""))
                    if key not in seen_calls:
                        seen_calls.add(key)
                        edges.add(upstream=task, downstream=other, weight=weights.call)
            for callee in callees:
                for other in owners_of(callee) - {task}:
                    key = (other, task, callee.get("name", ""), symbol)
                    if key not in seen_calls:
                        seen_calls.add(key)
                        edges.add(upstream=other, downstream=task, weight=weights.call)

            # Impact overlap: whoever reads what this task's write surface affects
            # depends on this task.
            for affected in client.impact(symbol):
                for other in owners_of(affected) - {task}:
                    key = (task, other, symbol, affected.get("name", ""))
                    if key not in seen_impacts:
                        seen_impacts.add(key)
                        edges.add(upstream=task, downstream=other, weight=weights.impact)

        metadata[task] = {
            "files": sorted(mapping.files),
            "symbols": sorted(mapping.symbols),
            "source_bytes": _source_bytes(client.repo_root, mapping.files),
            "symbol_count": len(mapping.symbols),
            "fan_in": fan_in,
            "fan_out": fan_out,
            "max_symbol_fan_in": max_symbol_fan_in,
            "max_symbol_fan_out": max_symbol_fan_out,
        }

    return TaskGraph(
        nodes=frozenset(ids),
        affinity=edges.affinity,
        dependencies=edges.dependencies,
        metadata=metadata,
    )


def _source_bytes(repo_root: Path, files: tuple[str, ...]) -> int:
    """Total on-disk size of a task's mapped files; missing files count zero."""
    total = 0
    for file in files:
        path = repo_root / file
        if path.is_file():
            total += path.stat().st_size
    return total
