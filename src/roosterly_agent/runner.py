"""The live run: read every open ticket, decide, send exactly one decision.

Crash safety. The helpdesk is the source of truth, not local state:
  1. Snapshot all open ticket ids first (handling tickets changes the "open" set,
     so paging while handling would skip tickets).
  2. Per ticket: GET it; if it already has a decision, skip it.
  3. Write an "intent" line to state/ledger.jsonl, POST, write the outcome.
  4. On a lost response, HelpdeskClient checks the ticket before any retry.
A crash at any point is fixed by running the same command again: handled
tickets are no longer open, and the ledger shows where it stopped.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent import Decision, TriageAgent, fallback_handoff
from .helpdesk import (AlreadyHandled, HelpdeskClient, HelpdeskError, RetriesExhausted,
                       ValidationFailed, is_handled)
from .llm import LLM
from .redact import redact

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonlFile:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        # Last line of defence: no payment details in any log, ever.
        line = redact(line)[0]
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def log_record(run_id: str, ticket_id: str, decision: Decision, outcome: str, started: float,
               extra: dict | None = None) -> dict:
    p, m = decision.payload, decision.meta
    rec = {
        "ts": _now(), "run": run_id, "ticket": ticket_id, "outcome": outcome,
        "action": decision.action, "language": p.get("language"), "category": p.get("category"),
        "priority": p.get("priority"), "confidence": p.get("confidence"),
        "faq_ids": m.get("faq_ids", []), "model_action": m.get("model_action"),
        "hard_rules": sorted(set(m.get("model_hard_rules", [])) | set(m.get("keyword_hits", {}))),
        "escalated_because": m.get("escalated_because", []),
        "reply_en": (m.get("judge") or {}).get("back_translation"),
        "redacted": m.get("redacted", []), "seconds": round(time.monotonic() - started, 2),
    }
    if extra:
        rec.update(extra)
    return rec


def run(hd: HelpdeskClient, agent: TriageAgent, llm: LLM, *, log_dir: Path, state_dir: Path,
        reset: bool = False, dry_run: bool = False, limit: int | None = None) -> dict:
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")  # the T keeps it from looking like a card number
    tickets_log = JsonlFile(log_dir / f"run-{run_id}{'-dry' if dry_run else ''}.jsonl")
    ledger = JsonlFile(state_dir / "ledger.jsonl")

    if reset:
        log.info("resetting practice tickets: %s", hd.reset())
        ledger.append({"ts": _now(), "run": run_id, "event": "reset"})

    ids = [t["id"] for t in hd.iter_tickets("open")]
    if limit:
        ids = ids[:limit]
    log.info("run %s: %d open tickets%s", run_id, len(ids), " (dry run)" if dry_run else "")
    counts: dict[str, int] = {}

    for i, tid in enumerate(ids, 1):
        started = time.monotonic()
        ticket = hd.get_ticket(tid)
        if is_handled(ticket):
            log.info("[%d/%d] %s already handled, skipping", i, len(ids), tid)
            counts["skipped_already_handled"] = counts.get("skipped_already_handled", 0) + 1
            continue

        try:
            decision = agent.decide(ticket)
        except Exception as exc:  # noqa: BLE001 - any model failure (LLMError, API, network) -> human
            log.exception("%s: triage failed", tid)
            decision = fallback_handoff(ticket, f"{type(exc).__name__}")

        outcome = "dry_run"
        extra: dict = {}
        if not dry_run:
            outcome, extra = _send(hd, ledger, run_id, tid, decision)

        tickets_log.append(log_record(run_id, tid, decision, outcome, started, extra))
        key = f"{decision.action}:{outcome}"
        counts[key] = counts.get(key, 0) + 1
        log.info("[%d/%d] %s -> %s %s %s (%s)", i, len(ids), tid, decision.action,
                 decision.payload.get("category"), decision.payload.get("priority"), outcome)

    remaining = None if dry_run else sum(1 for _ in hd.iter_tickets("open"))
    summary = {"run": run_id, "tickets": len(ids), "counts": counts, "still_open": remaining,
               "helpdesk_calls": dict(hd.stats), "llm": llm.summary(), "log": str(tickets_log.path)}
    tickets_log.path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2),
                                                              encoding="utf-8")
    return summary


def _send(hd: HelpdeskClient, ledger: JsonlFile, run_id: str, tid: str,
          decision: Decision) -> tuple[str, dict]:
    ledger.append({"ts": _now(), "run": run_id, "ticket": tid, "event": "intent",
                   "action": decision.action})
    try:
        outcome = hd.post_decision(tid, decision.action, decision.payload)
        extra: dict = {}
    except AlreadyHandled:
        outcome, extra = "already_handled_409", {}
    except ValidationFailed as exc:
        log.error("%s: 422 %s", tid, exc.detail)
        extra = {"validation_problems": exc.detail}
        outcome = "failed_422"
        if decision.action != "handoff":
            # Nothing was saved; a human is the safe destination.
            fb = fallback_handoff({}, f"helpdesk rejected {decision.action}: {exc.detail}")
            fb.payload.update({k: decision.payload[k] for k in ("language", "category", "priority")})
            try:
                outcome = hd.post_decision(tid, "handoff", fb.payload) + "_as_handoff_after_422"
            except HelpdeskError as exc2:
                extra["fallback_error"] = str(exc2)
    except (RetriesExhausted, HelpdeskError) as exc:
        log.error("%s: giving up for this run: %s", tid, exc)
        outcome, extra = "failed", {"error": str(exc)[:300]}
    ledger.append({"ts": _now(), "run": run_id, "ticket": tid, "event": outcome,
                   "action": decision.action})
    return outcome, extra
