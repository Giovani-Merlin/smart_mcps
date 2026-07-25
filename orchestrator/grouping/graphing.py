"""codegraph affinity adapter: task→region mappings → the TaskGraph U1 consumes.

Affinity edges come from real codegraph relations — shared files, call-graph
proximity, impact overlap — not symbol-name cosine (CoCoder's proxy, needed only
because its code didn't exist at graph time; see docs/research/cocoder-analysis.md
§7 point 3). Directed call and impact relations also become dependency edges:
the caller's task depends on the callee's task, and a task reading what another
task's write surface impacts depends on that task.

Plan-time signals (docs/orchestrator-task-map.md) join those layers for code that
doesn't exist at graph time: prospective files behave as shared-file regions,
``depends_on`` adds directed dependency edges only (never affinity), and matched
``implements``/``consumes`` route tags form a semantic affinity layer normalized
against the structural layer's total mass.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.grouping.partition import Pair, TaskGraph, canonical_pair

# The CLI defaults to 20 results, which would silently truncate hub fan-in counts
# (plan U2); every caller/callee query passes this explicit high limit instead.
CALL_QUERY_LIMIT = 1000
IMPACT_DEPTH = 2
# Weight of one declared task-map depends_on edge. Ordering-only (never affinity),
# so its magnitude matters solely for merge tie-breaking — not a config knob.
DECLARED_DEP_WEIGHT = 1.0

# Full CLI argv → stdout. Injected in tests to replay captured fixture output.
Runner = Callable[[Sequence[str]], str]

_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
# Verified against the live CLI (2026-07-15): a symbol with no exact index match
# prints `ℹ Symbol "<name>" not found` to stdout and exits 0 — no JSON.
_NOT_FOUND = re.compile(r"Symbol \".*\" not found")


class GraphBuildError(Exception):
    """codegraph output could not be turned into a task graph."""


@dataclass(frozen=True)
class TaskMapping:
    """One plan task mapped to code regions (files and symbols).

    The plan-time fields (all defaulted — mapper-produced mappings never set
    them) come from a parsed task map: ``prospective_files`` are plan-declared
    files that don't exist yet, ``depends_on`` names upstream task ids,
    ``slice`` is the vertical-slice must-link label, and ``implements``/
    ``consumes`` are matched route/contract tags.
    """

    task_id: str
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    prospective_files: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    slice: str | None = None
    implements: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeWeights:
    """Configurable weights for the affinity signals (plan R3).

    ``semantic`` is the base weight of one matched route-tag edge; the whole
    semantic layer is then scaled by ``clamp(Σw_struct / Σw_sem,
    semantic_floor, semantic_ceil)`` so it rebalances against the structural
    mass: pure greenfield (Σstruct≈0) floors the scale and semantics dominate by
    default; edit-heavy plans hit the ceiling so semantics refine but never
    override real reference edges.
    """

    shared_file: float = 1.0
    call: float = 2.0
    impact: float = 1.5
    semantic: float = 1.5
    semantic_floor: float = 0.5
    semantic_ceil: float = 3.0


@dataclass
class CodegraphClient:
    """Thin wrapper over the codegraph CLI's JSON output."""

    repo_root: Path
    runner: Runner | None = None
    call_limit: int = CALL_QUERY_LIMIT
    impact_depth: int = IMPACT_DEPTH
    # The CLI is a fresh subprocess per call and its output is stable within a run,
    # so identical queries (a hub symbol mapped by many tasks) are memoized.
    _cache: dict[tuple[str, ...], str] = field(default_factory=dict)

    def callers(self, symbol: str) -> list[dict]:
        args = ["callers", symbol, "-j", "-l", str(self.call_limit)]
        return self._parsed(args, expect_key="callers")

    def callees(self, symbol: str) -> list[dict]:
        args = ["callees", symbol, "-j", "-l", str(self.call_limit)]
        return self._parsed(args, expect_key="callees")

    def impact(self, symbol: str) -> list[dict]:
        args = ["impact", symbol, "-j", "-d", str(self.impact_depth)]
        return self._parsed(args, expect_key="affected")

    def query(self, search: str) -> list[dict]:
        """Symbol search; the CLI returns a JSON *array* of {node, score} entries."""
        args = ["query", search, "-j", "-l", str(self.call_limit)]
        payload = self._json(args)
        if not isinstance(payload, list):
            raise GraphBuildError(f"codegraph query {search!r} output is not a list")
        return payload

    def symbol_exists(self, name: str) -> bool:
        """Exact-name check used to verify mapper output against the index (R2)."""
        return any(entry.get("node", {}).get("name") == name for entry in self.query(name))

    def files_overview(self) -> str:
        """`codegraph files` output, ANSI-stripped — the architecture summary for
        base context and the mapper prompt (the CLI colorizes even when piped)."""
        return _ANSI_ESCAPES.sub("", self._run(["files"]))

    def sync(self) -> None:
        """Blocking `codegraph sync` (R13): refreshes the on-disk index before the
        pipeline's first read of it — grouping against a stale index silently drops
        real symbols and files that exist on disk."""
        self._run(["sync"])

    def _parsed(self, args: Sequence[str], expect_key: str) -> list[dict]:
        payload = self._json(args)
        if not isinstance(payload, dict) or expect_key not in payload:
            raise GraphBuildError(
                f"codegraph {args[0]} {args[1]!r} output is missing the {expect_key!r} key"
            )
        entries = payload[expect_key]
        if not isinstance(entries, list):
            raise GraphBuildError(f"codegraph {args[0]} {args[1]!r} {expect_key!r} is not a list")
        return entries

    def _json(self, args: Sequence[str]) -> object:
        raw = self._run(args)
        if not raw.strip():
            raise GraphBuildError(f"codegraph {args[0]} {args[1]!r} produced empty output")
        if _NOT_FOUND.search(raw):
            # Exit code 0 + plain-text "Symbol ... not found": a legitimate empty
            # result (the index has no exact match), not malformed output.
            return {"callers": [], "callees": [], "affected": []} if args[0] != "query" else []
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GraphBuildError(
                f"codegraph {args[0]} {args[1]!r} produced invalid JSON: {exc}"
            ) from exc

    def _run(self, args: Sequence[str]) -> str:
        key = tuple(args)
        if key in self._cache:
            return self._cache[key]
        if self.runner is not None:
            output = self.runner(args)
        else:
            result = subprocess.run(
                ["codegraph", *args, "-p", str(self.repo_root)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise GraphBuildError(
                    f"codegraph {args[0]} failed ({result.returncode}): {result.stderr.strip()}"
                )
            output = result.stdout
        self._cache[key] = output
        return output


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

    def add_dependency(self, upstream: str, downstream: str, weight: float) -> None:
        """Directed ordering edge with no affinity contribution (task-map depends_on:
        mixing precedence into cohesion produces incoherent groups)."""
        key = (upstream, downstream)
        self.dependencies[key] = self.dependencies.get(key, 0.0) + weight


def build_task_graph(
    mappings: Sequence[TaskMapping],
    client: CodegraphClient,
    weights: EdgeWeights | None = None,
) -> TaskGraph:
    """Query codegraph for every mapped symbol and assemble the weighted task graph.

    Region-less tasks (unmappable plan tasks carried as prose-only nodes, plan U4)
    become isolated nodes; the mapper's prose-affinity fallback adds their edges.
    Prospective files join the shared-file signal (a greenfield pair sharing a
    planned file clusters like an editing pair sharing a real one), ``depends_on``
    adds directed dependency edges only, and matched route tags form the semantic
    affinity layer, normalized last against the structural mass.
    """
    weights = weights or EdgeWeights()
    ids = [m.task_id for m in mappings]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise GraphBuildError(f"duplicate task ids in mappings: {sorted(duplicates)}")

    file_owner: dict[str, set[str]] = {}
    symbol_owner: dict[str, set[str]] = {}
    for mapping in mappings:
        for file in (*mapping.files, *mapping.prospective_files):
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

    # Declared plan ordering (task-map depends_on): the named task is upstream.
    for mapping in sorted(mappings, key=lambda m: m.task_id):
        for upstream in sorted(set(mapping.depends_on)):
            edges.add_dependency(upstream, mapping.task_id, DECLARED_DEP_WEIGHT)

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
            "prospective_files": sorted(mapping.prospective_files),
            "symbols": sorted(mapping.symbols),
            "slice": mapping.slice,
            "implements": sorted(mapping.implements),
            "consumes": sorted(mapping.consumes),
            "source_bytes": source_bytes_of(client.repo_root, mapping.files),
            "symbol_count": len(mapping.symbols),
            "fan_in": fan_in,
            "fan_out": fan_out,
            "max_symbol_fan_in": max_symbol_fan_in,
            "max_symbol_fan_out": max_symbol_fan_out,
        }

    _add_semantic_layer(mappings, edges, weights)

    return TaskGraph(
        nodes=frozenset(ids),
        affinity=edges.affinity,
        dependencies=edges.dependencies,
        metadata=metadata,
    )


def _add_semantic_layer(
    mappings: Sequence[TaskMapping], edges: _EdgeAccumulator, weights: EdgeWeights
) -> None:
    """Matched implements/consumes route tags → symmetric affinity, layer-normalized.

    This is the cross-stack signal codegraph cannot see (no edge between a TS
    ``fetch("/api/x")`` and its Python route). Runs after every structural edge is
    in, because the scale rebalances the semantic layer against the structural
    layer's total mass: ``clamp(Σw_struct / Σw_sem, floor, ceil)`` (multilayer-
    modularity practice — pure greenfield floors it and semantics dominate the
    near-empty structural layer; edit-heavy hits the ceiling so semantics refine
    but never override real reference edges).
    """
    implementers: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    for mapping in mappings:
        for tag in mapping.implements:
            implementers.setdefault(tag, set()).add(mapping.task_id)
        for tag in mapping.consumes:
            consumers.setdefault(tag, set()).add(mapping.task_id)

    semantic: dict[Pair, float] = {}
    for tag in sorted(set(implementers) & set(consumers)):
        for a in sorted(implementers[tag]):
            for b in sorted(consumers[tag]):
                if a == b:
                    continue
                pair = canonical_pair(a, b)
                semantic[pair] = semantic.get(pair, 0.0) + weights.semantic
    if not semantic:
        return

    structural_total = sum(edges.affinity.values())
    semantic_total = sum(semantic.values())
    scale = min(
        max(structural_total / max(semantic_total, 1e-9), weights.semantic_floor),
        weights.semantic_ceil,
    )
    for pair, weight in sorted(semantic.items()):
        edges.affinity[pair] = edges.affinity.get(pair, 0.0) + weight * scale


def source_bytes_of(repo_root: Path, files: Sequence[str]) -> int:
    """Total on-disk size of a set of mapped files; missing files count zero.

    Also used by the pipeline to size a whole group from its *union* of files, so
    a file shared by several member tasks is counted once, not once per task.
    """
    total = 0
    for file in files:
        path = repo_root / file
        if path.is_file():
            total += path.stat().st_size
    return total
