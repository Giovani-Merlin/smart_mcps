"""Durable record of the grouping engine's own LLM calls (mapper, speccer).

The partition a run is built on is decided by two LLM calls, and until now neither
left anything behind: the runner read ``envelope["result"]`` and dropped the
session id, the usage, and the reasoning. An operator asking "why did the grouper
put task X in group Y" had the answer nowhere on disk.

This module writes that answer next to the grouping it produced. It is inert by
contract — nothing written here is ever read back into a grouping decision, so a
failure to record can never change a partition. Every write is best-effort for the
same reason: losing the audit trail is bad, losing the run is worse.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from orchestrator.grouping.llm import LlmCallMeta, transcript_path_for

SCHEMA_VERSION = 1
INDEX_NAME = "calls.json"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class JsonlCallRecorder:
    """Writes one record per attempt into ``<grouping_dir>/llm/``.

    Failed and repaired attempts are recorded too. The previous behaviour kept
    only the last raw text, and only when the call failed outright, so a call that
    succeeded on retry left no trace of what it got wrong the first time — exactly
    the case an operator most wants to see.
    """

    def __init__(
        self,
        grouping_dir: Path,
        *,
        grouping_run_id: str,
        transcript_root: Path | None = None,
    ) -> None:
        self.dir = grouping_dir / "llm"
        self.grouping_run_id = grouping_run_id
        self.transcript_root = transcript_root
        self.calls: list[dict] = []
        self._seq = 0
        # Restart-safe: a driver restart used to reset _seq to 0, so the resumed
        # process's first call overwrote 01-*.{request,raw}.txt and rewrote
        # calls.json down to one entry — g1's rewrite record on r20260830-163212
        # was destroyed exactly this way. Loading the existing index (or, when
        # it is unreadable, scanning the NN-*.request.txt filenames) makes
        # _write_index append across restarts instead of clobbering.
        try:
            self._load_existing()
        except Exception:  # noqa: BLE001 — inert by contract, like every write here
            self.calls = []
            try:
                self._seq = self._seq_from_filenames()
            except OSError:
                self._seq = 0

    def _load_existing(self) -> None:
        index = self.dir / INDEX_NAME
        if index.is_file():
            payload = json.loads(index.read_text())
            calls = payload.get("calls")
            if isinstance(calls, list):
                self.calls = [call for call in calls if isinstance(call, dict)]
            produced = payload.get("produced")
            if produced is not None:
                # Keep the outputs join a previous process recorded — writing
                # the index without it would clobber the join to null.
                self._produced = produced
        seqs = [call.get("seq") for call in self.calls]
        self._seq = max(
            (seq for seq in seqs if isinstance(seq, int)),
            default=self._seq_from_filenames(),
        )

    def _seq_from_filenames(self) -> int:
        """Highest ``NN-…`` prefix among recorded request files — the fallback
        when ``calls.json`` is missing or unreadable but attempts left files."""
        if not self.dir.is_dir():
            return 0
        best = 0
        for path in self.dir.glob("*.request.txt"):
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                best = max(best, int(head))
        return best

    def record_call(
        self,
        *,
        stage: str,
        attempt: int,
        prompt: str,
        schema: dict,
        raw: str,
        meta: LlmCallMeta | None,
        error: str | None,
    ) -> None:
        try:
            self._record(stage, attempt, prompt, schema, raw, meta, error)
        except OSError:
            pass  # inert by contract: never fail a grouping over an audit write

    def _record(
        self,
        stage: str,
        attempt: int,
        prompt: str,
        schema: dict,
        raw: str,
        meta: LlmCallMeta | None,
        error: str | None,
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        base = f"{self._seq:02d}-{stage}-a{attempt}"
        prompt_file = self.dir / f"{base}.request.txt"
        raw_file = self.dir / f"{base}.raw.txt"
        prompt_file.write_text(prompt)
        raw_file.write_text(str(raw))

        session_id = meta.session_id if meta else None
        transcript = transcript_path_for(session_id, self.transcript_root) if session_id else None
        record = {
            "seq": self._seq,
            "recorded_at": _now(),
            # gen_ai.* naming per the OpenTelemetry GenAI conventions, so this can
            # be replayed into an OTel backend later without reshaping. No OTel
            # dependency is taken.
            "gen_ai.operation.name": stage,
            "gen_ai.request.model": meta.model if meta else None,
            "attempt": attempt,
            "status": {"code": "error" if error else "ok"},
            "error": error,
            "claude.session_id": session_id,
            "claude.transcript_path": str(transcript) if transcript else None,
            "gen_ai.usage.input_tokens": meta.input_tokens if meta else 0,
            "gen_ai.usage.output_tokens": meta.output_tokens if meta else 0,
            "claude.usage.cache_read_tokens": meta.cache_read_tokens if meta else 0,
            "claude.usage.cache_creation_tokens": meta.cache_creation_tokens if meta else 0,
            "duration_ms": meta.duration_ms if meta else None,
            "schema_title": schema.get("title"),
            "request_file": prompt_file.name,
            "raw_file": raw_file.name,
        }
        self.calls.append(record)
        self._write_index()

    def link_outputs(self, *, task_ids: list[str], group_ids: list[str]) -> None:
        """Join the recorded calls to what they produced.

        ``groups.json`` is deliberately not stamped with a run id — a timestamped
        field there would break ``serialize_grouping``'s determinism contract, and
        determinism is what makes two groupings diffable. The join therefore lives
        on this side only.
        """
        self._produced = {"task_ids": sorted(task_ids), "group_ids": sorted(group_ids)}
        try:
            self._write_index()
        except OSError:
            pass

    def _write_index(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "grouping_run_id": self.grouping_run_id,
            "produced": getattr(self, "_produced", None),
            "calls": self.calls,
        }
        (self.dir / INDEX_NAME).write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
