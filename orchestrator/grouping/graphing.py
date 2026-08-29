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

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from orchestrator.grouping.partition import (
    Pair,
    TaskGraph,
    _strongly_connected_components,
    canonical_pair,
)

# The CLI defaults to 20 results, which would silently truncate hub fan-in counts
# (plan U2); every caller/callee query passes this explicit high limit instead.
CALL_QUERY_LIMIT = 1000
IMPACT_DEPTH = 2
# Weight of one declared task-map depends_on edge. Ordering-only (never affinity),
# so its magnitude matters solely for merge tie-breaking — not a config knob.
DECLARED_DEP_WEIGHT = 1.0

# Limit for the one bulk `codegraph query ""` call `logical_export` issues — large
# enough to return every node in a real index (`query`'s `-l` is a hard cap, not a
# page size: there is no pagination), rather than `CALL_QUERY_LIMIT`, which is sized
# for one symbol's fan-in/out, not the whole index.
FULL_EXPORT_LIMIT = 1_000_000

# codegraph subcommands that take the project path positionally and reject `-p`
# (`codegraph sync --help`: `Usage: codegraph sync [options] [path]`). Every other
# command this client issues accepts `-p, --path`.
POSITIONAL_PATH_COMMANDS = frozenset({"sync", "index", "status"})

# Full CLI argv → stdout. Injected in tests to replay captured fixture output.
Runner = Callable[[Sequence[str]], str]

_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")
# Verified against the live CLI (2026-07-15): a symbol with no exact index match
# prints `ℹ Symbol "<name>" not found` to stdout and exits 0 — no JSON.
_NOT_FOUND = re.compile(r"Symbol \".*\" not found")


class GraphBuildError(Exception):
    """codegraph output could not be turned into a task graph."""


def _canonical_list(items: Sequence[dict]) -> list[dict]:
    """Sort a list of dicts by their own canonical JSON — order-independent
    without requiring every entry to share a natural sort key."""
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def index_fingerprint(export: dict) -> str:
    """sha256 of a canonical logical export of the index's *content* (plan U5):
    sorted symbol ids, sorted file paths and sorted edges, not
    ``codegraph status -j``'s operational counters (queue depth, uptime, cache
    size). The old fingerprint hashed exactly those counters and churned three
    times in fifteen minutes at one commit while `sync` reported "already up to
    date" — a stale-vs-synced distinction the counters conflate with real
    content, which the new export sidesteps entirely.

    ``export`` is the shape ``CodegraphClient.logical_export`` returns:
    ``{"symbols": [...], "files": [...], "edges": [...]}``. Each list is
    re-sorted here (by each entry's own canonical JSON) rather than trusted to
    already be sorted, so a caller's list order never changes the fingerprint.
    """
    canonical = {
        "symbols": _canonical_list(export.get("symbols", [])),
        "files": sorted(export.get("files", [])),
        "edges": _canonical_list(export.get("edges", [])),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskMapping:
    """One plan task mapped to code regions (files and symbols).

    The plan-time fields (all defaulted — mapper-produced mappings never set
    them) come from a parsed task map: ``prospective_files`` are plan-declared
    files that don't exist yet, ``size_hints`` prices a subset of those by
    declared class (small/medium/large) instead of the flat per-file allowance
    (plan U7), ``depends_on`` names upstream task ids, ``slice`` is the
    vertical-slice must-link label, and ``implements``/``consumes`` are matched
    route/contract tags.
    """

    task_id: str
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    prospective_files: tuple[str, ...] = ()
    size_hints: tuple[tuple[str, str], ...] = ()
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

    def status(self, *, fresh: bool = False) -> dict:
        """`codegraph status -j`, parsed. ``fresh`` bypasses the per-argv cache
        (plan U6): the quiescence handshake polls this repeatedly and needs a
        live read each time, not the first subprocess's memoized output."""
        payload = self._json(["status", "-j"], cache=not fresh)
        if not isinstance(payload, dict):
            raise GraphBuildError("codegraph status output is not a JSON object")
        return payload

    def logical_export(self) -> dict[str, list]:
        """Canonical logical export of the index's *content* (plan U5): sorted
        symbol ids, file paths and edges, computed from one fresh bulk
        `codegraph query ""` call — always uncached, since every caller (the
        quiescence handshake) needs a live read every time, not a memoized one.

        No CLI command exports the whole edge graph in bulk (walking
        callers/callees per symbol would be O(symbols) subprocess calls on
        every poll — the opposite of "cheaply"), so import-kind nodes, the
        only edges the bulk query surface exposes for free, stand in for the
        edge layer; every other node kind is a symbol. This is the residual
        this plan accepts explicitly: index-stable, not a full structural
        content hash.
        """
        payload = self._json(
            ["query", "", "-j", "-l", str(FULL_EXPORT_LIMIT)],
            cache=False,
        )
        if not isinstance(payload, list):
            raise GraphBuildError("codegraph query '' output is not a list")
        symbols: list[dict] = []
        edges: list[dict] = []
        files: set[str] = set()
        for entry in payload:
            node = entry.get("node", {}) if isinstance(entry, dict) else {}
            file_path = node.get("filePath", "")
            if file_path:
                files.add(file_path)
            if node.get("kind") == "import":
                edges.append({"filePath": file_path, "target": node.get("name", "")})
            else:
                symbols.append(
                    {
                        "id": node.get("id", ""),
                        "kind": node.get("kind", ""),
                        "name": node.get("name", ""),
                        "qualifiedName": node.get("qualifiedName", ""),
                        "filePath": file_path,
                        "signature": node.get("signature", ""),
                    }
                )
        return {"symbols": symbols, "files": sorted(files), "edges": edges}

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

    def _json(self, args: Sequence[str], *, cache: bool = True) -> object:
        raw = self._run(args, cache=cache)
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

    def _argv(self, args: Sequence[str]) -> list[str]:
        """Full command line for one codegraph call.

        The CLI is not uniform about how it takes the project path: the query
        commands (``query``/``files``/``callers``/``callees``/``impact``) accept
        ``-p, --path``, while the index-maintenance commands take it as a
        positional argument and reject ``-p`` outright — ``codegraph sync -p
        <repo>`` exits 1 with ``unknown option '-p'``. Passing ``-p`` to every
        command made ``client.sync()`` — and with it every real ``group``
        invocation — fail; the injected-runner test seam hid it, because no test
        ever built this argv.
        """
        if args[0] in POSITIONAL_PATH_COMMANDS:
            return ["codegraph", *args, str(self.repo_root)]
        return ["codegraph", *args, "-p", str(self.repo_root)]

    def _run(self, args: Sequence[str], *, cache: bool = True) -> str:
        key = tuple(args)
        if cache and key in self._cache:
            return self._cache[key]
        if self.runner is not None:
            output = self.runner(args)
        else:
            result = subprocess.run(
                self._argv(args),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise GraphBuildError(
                    f"codegraph {args[0]} failed ({result.returncode}): {result.stderr.strip()}"
                )
            output = result.stdout
        if cache:
            self._cache[key] = output
        return output


class QuiescenceRecorder(Protocol):
    """Structural type for the optional quiescence trace hook (plan U6).

    This module never imports ``orchestrator.grouping.trace`` — a caller
    passes anything satisfying this shape, mirroring ``partition.py``'s
    ``PartitionRecorder`` seam and keeping the dependency one-directional.
    """

    def record_index_observation(self, fingerprint: str, status: dict) -> None: ...


# Defaults chosen so a healthy, already-settled index costs no real wall-clock
# time in the common case (a fresh subprocess-backed client still pays three
# real `codegraph` invocations, but a fake/injected runner in tests pays
# ``time.sleep(0.05)`` twice, not a hand-tuned production interval) while still
# detecting the drift this plan exists to catch: three reads that must agree,
# a short gap between them, and a ceiling so a genuinely moving index fails
# fast rather than hanging the whole grouping run.
DEFAULT_QUIESCENCE_MIN_STABLE_READS = 3
DEFAULT_QUIESCENCE_INTERVAL_S = 0.05
DEFAULT_QUIESCENCE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class IndexObservation:
    """One quiescence-poll read: the fingerprint plus the full `status -j`
    payload behind it (plan U6), kept together so a drift is diagnosable from
    the trace alone rather than needing a separate investigation."""

    fingerprint: str
    status: dict


def await_index_quiescence(
    client: CodegraphClient,
    *,
    min_stable_reads: int = DEFAULT_QUIESCENCE_MIN_STABLE_READS,
    interval_s: float = DEFAULT_QUIESCENCE_INTERVAL_S,
    timeout_s: float = DEFAULT_QUIESCENCE_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    recorder: QuiescenceRecorder | None = None,
) -> str:
    """Poll ``index_fingerprint(client.logical_export())`` until it repeats
    ``min_stable_reads`` times in a row (plan U6): a fingerprint that churned
    three times in fifteen minutes at one commit while `sync` reported
    "already up to date" is proof the index can still be moving right after a
    `sync` call returns, so partitioning against the very next read is not
    safe.

    Every read is handed to ``recorder`` (fingerprint plus the full
    `status -j` payload) so a drift is attributable after the fact from the
    trace alone. Raises ``GraphBuildError`` naming every distinct fingerprint
    observed if the index never settles before ``timeout_s`` — proceeding on
    a moving index is exactly the failure this handshake exists to prevent.
    """
    if min_stable_reads < 1:
        raise ValueError("min_stable_reads must be at least 1")
    deadline = now() + timeout_s
    observations: list[IndexObservation] = []
    streak_value: str | None = None
    streak_count = 0
    while True:
        status_payload = client.status(fresh=True)
        fingerprint = index_fingerprint(client.logical_export())
        observations.append(IndexObservation(fingerprint=fingerprint, status=status_payload))
        if recorder is not None:
            recorder.record_index_observation(fingerprint, status_payload)
        if fingerprint == streak_value:
            streak_count += 1
        else:
            streak_value = fingerprint
            streak_count = 1
        if streak_count >= min_stable_reads:
            return fingerprint
        if now() >= deadline:
            distinct = sorted({o.fingerprint for o in observations})
            raise GraphBuildError(
                "codegraph index did not settle within "
                f"{timeout_s}s ({len(observations)} read(s)): observed "
                f"{len(distinct)} distinct fingerprint(s): {distinct}"
            )
        sleep(interval_s)


# Cap on how many contributions one edge keeps in the provenance sidecar (plan P2).
# A saturated graph can put hundreds of contributions on a single pair; the cap keeps
# edge-provenance.json bounded, and the ledger's counters report the true total rather
# than dropping silently.
MAX_CONTRIBUTIONS_PER_EDGE = 20

# Signal kinds a contribution can carry. Kept as plain strings (the sidecar is data,
# not an API), enumerated here so readers of the artifact have one list to consult.
SIGNAL_SHARED_FILE = "shared_file"
SIGNAL_CALL = "call"
SIGNAL_IMPACT = "impact"
SIGNAL_DECLARED_DEPENDS_ON = "declared_depends_on"
SIGNAL_SEMANTIC = "semantic"
SIGNAL_PROSE_NEIGHBOR = "prose_neighbor"


@dataclass(frozen=True)
class EdgeContribution:
    """One signal's justification for one edge, with the weight it actually added.

    ``scaled_weight`` is the number that reached the weight map — for the semantic
    layer that is the post-scale value, not the base weight — so a ledger's
    contributions sum back to the edge's weight exactly.
    """

    kind: str
    declared: bool
    scaled_weight: float
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": self.kind,
            "declared": self.declared,
            "scaled_weight": self.scaled_weight,
        }
        if self.files:
            record["files"] = list(self.files)
        if self.symbols:
            record["symbols"] = list(self.symbols)
        if self.detail:
            record["detail"] = dict(self.detail)
        return record


@dataclass
class EdgeLedger:
    """Per-edge provenance mirroring one entry of a weight map.

    ``total_weight`` is accumulated by the same additions in the same order as the
    weight map itself, so it is bit-for-bit the weight the partitioner sees.
    ``truncated_weight`` carries whatever the cap kept out, which is what makes the
    sum identity hold even for a capped edge.
    """

    contributions: list[EdgeContribution] = field(default_factory=list)
    total_contributions: int = 0
    total_weight: float = 0.0
    truncated_weight: float = 0.0

    def add(self, contribution: EdgeContribution, cap: int) -> None:
        self.total_contributions += 1
        self.total_weight += contribution.scaled_weight
        if len(self.contributions) < cap:
            self.contributions.append(contribution)
        else:
            self.truncated_weight += contribution.scaled_weight

    @property
    def recorded_contributions(self) -> int:
        return len(self.contributions)

    @property
    def truncated_contributions(self) -> int:
        return self.total_contributions - len(self.contributions)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_weight": self.total_weight,
            "total_contributions": self.total_contributions,
            "recorded_contributions": self.recorded_contributions,
            "truncated_contributions": self.truncated_contributions,
            "truncated_weight": self.truncated_weight,
            "contributions": [c.as_dict() for c in self.contributions],
        }


@dataclass(frozen=True)
class WithdrawnEdge:
    """An inferred precedence edge ``_drop_inferred_cycles`` took back.

    Structured rather than prose-in-a-flag-string: the flags stay as they were (they
    are human-facing and byte-stable in the trace), while this record carries the
    endpoints, why, what weight the affinity half kept, and the contributions that
    proposed the edge in the first place.
    """

    upstream: str
    downstream: str
    reason: str
    weight: float
    cycle_members: tuple[str, ...] = ()
    contributions: tuple[EdgeContribution, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "inferred_precedence",
            "upstream": self.upstream,
            "downstream": self.downstream,
            "reason": self.reason,
            "weight": self.weight,
            "cycle_members": list(self.cycle_members),
            "contributions": [c.as_dict() for c in self.contributions],
        }


@dataclass
class EdgeProvenance:
    """Two ledgers mirroring ``_EdgeAccumulator``'s two weight maps, plus withdrawals.

    Purely observational (plan P2): nothing here is ever read back into a grouping
    decision, and the weight arithmetic is untouched by its presence.
    """

    max_contributions_per_edge: int = MAX_CONTRIBUTIONS_PER_EDGE
    affinity: dict[Pair, EdgeLedger] = field(default_factory=dict)
    dependencies: dict[Pair, EdgeLedger] = field(default_factory=dict)
    withdrawn: list[WithdrawnEdge] = field(default_factory=list)

    def record_affinity(self, pair: Pair, contribution: EdgeContribution) -> None:
        ledger = self.affinity.setdefault(pair, EdgeLedger())
        ledger.add(contribution, self.max_contributions_per_edge)

    def record_dependency(self, key: Pair, contribution: EdgeContribution) -> None:
        ledger = self.dependencies.setdefault(key, EdgeLedger())
        ledger.add(contribution, self.max_contributions_per_edge)

    def dependency_kinds(self, key: Pair) -> tuple[str, ...]:
        """Signal kinds recorded for one dependency edge, e.g. ``("call",
        "impact")`` — empty if the edge carries no ledger at all."""
        ledger = self.dependencies.get(key)
        if ledger is None:
            return ()
        return tuple(sorted({c.kind for c in ledger.contributions}))

    def dependency_is_declared(self, key: Pair) -> bool:
        """Whether ``key`` carries at least one declared (task-map
        ``depends_on``) contribution, as opposed to being wholly inferred from
        codegraph relations — the same per-edge ledger
        ``edge_provenance_document`` already walks, exposed here as a reusable
        predicate (plan U9/U10) rather than re-derived error-message text.
        Declared and inferred contributions can coexist on one edge (a
        depends_on pair that also happens to call each other); any declared
        contribution is enough to call the edge declared.
        """
        ledger = self.dependencies.get(key)
        if ledger is None:
            return False
        return any(c.declared for c in ledger.contributions)

    def withdraw(self, key: Pair, reason: str, cycle_members: Sequence[str] = ()) -> None:
        ledger = self.dependencies.pop(key, EdgeLedger())
        self.withdrawn.append(
            WithdrawnEdge(
                upstream=key[0],
                downstream=key[1],
                reason=reason,
                weight=ledger.total_weight,
                cycle_members=tuple(cycle_members),
                contributions=tuple(ledger.contributions),
            )
        )


@dataclass
class _EdgeAccumulator:
    affinity: dict[Pair, float] = field(default_factory=dict)
    dependencies: dict[Pair, float] = field(default_factory=dict)
    # Directed pairs that came from a plan's declared depends_on rather than from a
    # codegraph relation. Only these are precedence by *statement*; everything else
    # is precedence by *inference* and is subject to the acyclicity filter below.
    declared: set[Pair] = field(default_factory=set)
    # Observational ledgers: written alongside every weight update, never read back.
    provenance: EdgeProvenance = field(default_factory=EdgeProvenance)

    def add(
        self,
        upstream: str,
        downstream: str,
        weight: float,
        kind: str = SIGNAL_CALL,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        detail: dict[str, object] | None = None,
    ) -> None:
        """Derived structural relation: cohesion *and* provisional precedence.

        The affinity half is unconditional — a call or impact relation is real
        coupling however the pair is ordered. The dependency half is a hypothesis
        that ``_drop_inferred_cycles`` may withdraw, which is why the weight is
        recorded in both maps: dropping the directed edge later costs no affinity.
        """
        pair = canonical_pair(upstream, downstream)
        self.affinity[pair] = self.affinity.get(pair, 0.0) + weight
        key = (upstream, downstream)
        self.dependencies[key] = self.dependencies.get(key, 0.0) + weight
        contribution = EdgeContribution(
            kind=kind,
            declared=False,
            scaled_weight=weight,
            files=tuple(files),
            symbols=tuple(symbols),
            detail=dict(detail or {}),
        )
        self.provenance.record_affinity(pair, contribution)
        self.provenance.record_dependency(key, contribution)

    def add_symmetric(
        self,
        a: str,
        b: str,
        weight: float,
        kind: str = SIGNAL_SHARED_FILE,
        files: Sequence[str] = (),
        symbols: Sequence[str] = (),
        detail: dict[str, object] | None = None,
    ) -> None:
        pair = canonical_pair(a, b)
        self.affinity[pair] = self.affinity.get(pair, 0.0) + weight
        self.provenance.record_affinity(
            pair,
            EdgeContribution(
                kind=kind,
                declared=False,
                scaled_weight=weight,
                files=tuple(files),
                symbols=tuple(symbols),
                detail=dict(detail or {}),
            ),
        )

    def add_dependency(self, upstream: str, downstream: str, weight: float) -> None:
        """Directed ordering edge with no affinity contribution (task-map depends_on:
        mixing precedence into cohesion produces incoherent groups)."""
        key = (upstream, downstream)
        self.dependencies[key] = self.dependencies.get(key, 0.0) + weight
        self.declared.add(key)
        self.provenance.record_dependency(
            key,
            EdgeContribution(
                kind=SIGNAL_DECLARED_DEPENDS_ON,
                declared=True,
                scaled_weight=weight,
                detail={"source": "task_map"},
            ),
        )


def _drop_inferred_cycles(edges: _EdgeAccumulator) -> list[str]:
    """Withdraw inferred precedence until ``dependencies`` is a DAG. Returns flags.

    A codegraph reference is *coupling*, not *ordering*: it says how code
    references code today, while a task dependency is a claim about which edit
    must land first. Reading every ``callers``/``callees``/``impact`` relation as
    precedence saturates the graph — measured on a real 8-task plan, 52 of 56
    possible directed edges, one SCC containing every task, so the only acyclic
    partition was the degenerate single group at 3.8x the budget cap
    (docs/orchestrator-grouping.md, limitation 4).

    Two withdrawals, both affinity-preserving because ``_EdgeAccumulator.add``
    already banked the weight symmetrically:

    1. **Mutual pairs.** ``a -> b`` and ``b -> a`` both inferred means the two
       tasks reference each other — coupling with no ordering. Drop both.
    2. **Residual SCCs.** Dropping mutual pairs cannot break a longer cycle
       (a -> b -> c -> a has no mutual pair), so whatever still cycles has its
       inferred edges dropped as well.

    Declared ``depends_on`` edges are never withdrawn — they are the plan's own
    statement of intent. They are validated acyclic at parse time
    (``plan_reader._check_acyclic``), so an SCC that survives step 2 means
    declared edges alone are cyclic, which is a caller bug rather than a plan the
    partitioner should silently repair.
    """
    flags: list[str] = []
    inferred = {key for key in edges.dependencies if key not in edges.declared}

    mutual = sorted(
        key for key in inferred if (key[1], key[0]) in edges.dependencies and key[1] != key[0]
    )
    withdrawn = {key for key in mutual if key in inferred}
    # Why each edge went, for the provenance sidecar; the flag strings below are
    # left exactly as they were (they are human-facing and byte-stable in the trace).
    withdrawal_reason: dict[Pair, str] = dict.fromkeys(withdrawn, "mutual_reference")
    withdrawal_cycle: dict[Pair, tuple[str, ...]] = {}
    if withdrawn:
        flags.append(
            f"graph: withdrew {len(withdrawn)} inferred precedence edge(s) between "
            f"{len({canonical_pair(*k) for k in withdrawn})} mutually-referencing task "
            "pair(s) — kept as affinity (a reference is coupling, not ordering)"
        )

    remaining = {k: w for k, w in edges.dependencies.items() if k not in withdrawn}
    adjacency: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for up, down in remaining:
        adjacency.setdefault(up, set()).add(down)
        nodes.update((up, down))
    residual = [c for c in _strongly_connected_components(adjacency, nodes) if len(c) > 1]
    for component in residual:
        members = set(component)
        internal = {
            key
            for key in remaining
            if key[0] in members and key[1] in members and key not in edges.declared
        }
        if not internal:
            raise GraphBuildError(
                "declared depends_on edges form a cycle among tasks "
                f"{sorted(members)} — the task map's own ordering is not a DAG"
            )
        withdrawn |= internal
        for key in internal:
            withdrawal_reason[key] = "reference_cycle"
            withdrawal_cycle[key] = tuple(sorted(members))
        flags.append(
            f"graph: withdrew {len(internal)} inferred precedence edge(s) inside a "
            f"{len(members)}-task reference cycle {sorted(members)} — kept as affinity"
        )

    for key in sorted(withdrawn):
        edges.dependencies.pop(key, None)
        edges.provenance.withdraw(
            key,
            reason=withdrawal_reason.get(key, "inferred_cycle"),
            cycle_members=withdrawal_cycle.get(key, ()),
        )
    return flags


def build_task_graph(
    mappings: Sequence[TaskMapping],
    client: CodegraphClient,
    weights: EdgeWeights | None = None,
    flags: list[str] | None = None,
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
    for file, owners in sorted(file_owner.items()):
        owner_list = sorted(owners)
        for i, a in enumerate(owner_list):
            for b in owner_list[i + 1 :]:
                edges.add_symmetric(
                    a,
                    b,
                    weights.shared_file,
                    kind=SIGNAL_SHARED_FILE,
                    files=(file,),
                )

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
                        edges.add(
                            upstream=task,
                            downstream=other,
                            weight=weights.call,
                            kind=SIGNAL_CALL,
                            files=tuple(f for f in (caller.get("filePath", ""),) if f),
                            symbols=(symbol, caller.get("name", "")),
                            detail={"relation": "caller", "of": symbol},
                        )
            for callee in callees:
                for other in owners_of(callee) - {task}:
                    key = (other, task, callee.get("name", ""), symbol)
                    if key not in seen_calls:
                        seen_calls.add(key)
                        edges.add(
                            upstream=other,
                            downstream=task,
                            weight=weights.call,
                            kind=SIGNAL_CALL,
                            files=tuple(f for f in (callee.get("filePath", ""),) if f),
                            symbols=(symbol, callee.get("name", "")),
                            detail={"relation": "callee", "of": symbol},
                        )

            # Impact overlap: whoever reads what this task's write surface affects
            # depends on this task.
            for affected in client.impact(symbol):
                for other in owners_of(affected) - {task}:
                    key = (task, other, symbol, affected.get("name", ""))
                    if key not in seen_impacts:
                        seen_impacts.add(key)
                        edges.add(
                            upstream=task,
                            downstream=other,
                            weight=weights.impact,
                            kind=SIGNAL_IMPACT,
                            files=tuple(f for f in (affected.get("filePath", ""),) if f),
                            symbols=(symbol, affected.get("name", "")),
                            detail={"relation": "impact", "of": symbol},
                        )

        metadata[task] = {
            "files": sorted(mapping.files),
            "prospective_files": sorted(mapping.prospective_files),
            "size_hints": dict(mapping.size_hints),
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
    # Last, so it sees every inferred edge at once: a pair can become mutual through
    # two different symbols, and an SCC can close through a task whose own symbols
    # were queried earlier in the loop above.
    cycle_flags = _drop_inferred_cycles(edges)
    if flags is not None:
        flags.extend(cycle_flags)

    graph = TaskGraph(
        nodes=frozenset(ids),
        affinity=edges.affinity,
        dependencies=edges.dependencies,
        metadata=metadata,
        provenance=edges.provenance,
    )
    graph.assert_acyclic_dependencies()  # builder-output contract; see limitation 4
    return graph


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
    tags_by_pair: dict[Pair, list[str]] = {}
    for tag in sorted(set(implementers) & set(consumers)):
        for a in sorted(implementers[tag]):
            for b in sorted(consumers[tag]):
                if a == b:
                    continue
                pair = canonical_pair(a, b)
                semantic[pair] = semantic.get(pair, 0.0) + weights.semantic
                tags_by_pair.setdefault(pair, []).append(tag)
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
        # One contribution per pair rather than per tag: the layer scales the pair's
        # summed weight, and splitting it per tag would not re-sum to the same float.
        edges.provenance.record_affinity(
            pair,
            EdgeContribution(
                kind=SIGNAL_SEMANTIC,
                declared=True,
                scaled_weight=weight * scale,
                detail={
                    "tags": sorted(tags_by_pair.get(pair, ())),
                    "base_weight": weight,
                    "layer_scale": scale,
                },
            ),
        )


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
