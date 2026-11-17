"""Eval on the 10 labelled examples from GET /examples.

Labels come from Nordavo (the API), never from the model under test. The agent
runs in decision-only mode: nothing is posted. Scores:
  * safety: gold says handoff but agent replied (critical), and replies the judge rejected;
  * per-field accuracy: language, category, priority, action;
  * over-escalation: gold says reply but agent handed off (costs the team time).
Results go to eval/results/<label>-<timestamp>.json and a row in eval/results/history.md.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agent import TriageAgent, fallback_handoff
from .helpdesk import HelpdeskClient
from .llm import LLM

FIELDS = ("language", "category", "priority", "action")


def load_examples(hd: HelpdeskClient | None, cache: Path, refresh: bool = False) -> list[dict]:
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    if hd is None:
        raise SystemExit("No cached examples; helpdesk credentials needed to fetch them.")
    examples = hd.examples()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding="utf-8")
    return examples


def evaluate(agent: TriageAgent, llm: LLM, examples: list[dict], eval_dir: Path,
             label: str) -> dict:
    rows = []
    for ex in examples:
        gold = ex["label"]
        try:
            decision = agent.decide(ex)
        except Exception as exc:  # noqa: BLE001 - same safe fallback as the live run
            decision = fallback_handoff(ex, type(exc).__name__)
        pred = {**{k: decision.payload.get(k) for k in FIELDS[:3]}, "action": decision.action}
        row = {
            "id": ex["id"],
            "gold": {k: gold.get(k) for k in FIELDS},
            "pred": pred,
            "correct": {k: pred[k] == gold.get(k) for k in FIELDS},
            "unsafe_reply": gold.get("action") == "handoff" and decision.action == "reply",
            "over_escalation": gold.get("action") == "reply" and decision.action == "handoff",
            "escalated_because": decision.meta.get("escalated_because", []),
            "model_said": {"action": decision.meta.get("model_action"),
                           "faq_ids": decision.meta.get("faq_ids"),
                           "deterministic_check": decision.meta.get("deterministic_check"),
                           "judge": decision.meta.get("judge")},
            "reply_en": (decision.meta.get("judge") or {}).get("back_translation"),
            "gold_reason": gold.get("reason"),
            "agent_reason": decision.payload.get("reason"),
        }
        rows.append(row)
        marks = " ".join(f"{k[:4]}:{'ok' if row['correct'][k] else 'X'}" for k in FIELDS)
        print(f"  {ex['id']}: {marks}  gold={row['gold']['action']}/{row['gold']['category']}/"
              f"{row['gold']['priority']}  pred={pred['action']}/{pred['category']}/{pred['priority']}"
              + ("  UNSAFE" if row["unsafe_reply"] else ""))

    n = len(rows)
    acc = {k: sum(r["correct"][k] for r in rows) / n for k in FIELDS}
    gold_replies = sum(r["gold"]["action"] == "reply" for r in rows)
    result = {
        "label": label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "n": n,
        "accuracy": acc,
        "all_fields_correct": sum(all(r["correct"].values()) for r in rows) / n,
        "unsafe_replies": sum(r["unsafe_reply"] for r in rows),
        "over_escalations": f"{sum(r['over_escalation'] for r in rows)}/{gold_replies}",
        "llm": llm.summary(),
        "cost_per_100_tickets_usd": round(llm.cost_usd() / n * 100, 4),
        "rows": rows,
    }
    out = eval_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{label}-{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    history = out / "history.md"
    if not history.exists():
        history.write_text("| when | label | lang | cat | prio | action | all | unsafe | over-esc | $/100 |\n"
                           "|---|---|---|---|---|---|---|---|---|---|\n", encoding="utf-8")
    with history.open("a", encoding="utf-8") as f:
        f.write(f"| {result['timestamp']} | {label} | {acc['language']:.0%} | {acc['category']:.0%} | "
                f"{acc['priority']:.0%} | {acc['action']:.0%} | {result['all_fields_correct']:.0%} | "
                f"{result['unsafe_replies']} | {result['over_escalations']} | "
                f"{result['cost_per_100_tickets_usd']} |\n")
    return result
