"""Turns one ticket into one decision. Pure decision logic: no helpdesk calls here,
so the same code path is used by the live run and by the eval.

Pipeline:
  redact -> empty? close -> LLM triage -> hard-rule guard -> reply gates
  (confidence, cited FAQ, deterministic checks, judge) -> decision
Every gate fails safe: a doubtful reply becomes a handoff, never the other way round.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLM
from .prompts import JUDGE_SYSTEM, TRIAGE_SYSTEM, ReplyCheck, Triage, format_ticket
from .redact import redact
from .rules import PRIORITY_FLOOR, is_empty, match_hard_rules, most_urgent
from .verify import detect_language, deterministic_checks

log = logging.getLogger(__name__)

FAQ_IDS = {f"F{i:02d}" for i in range(1, 13)}
CLOSE_MIN_CONFIDENCE = 0.8


@dataclass
class Decision:
    action: str
    payload: dict
    meta: dict = field(default_factory=dict)


class TriageAgent:
    def __init__(self, llm: LLM, faq_path: Path, agent_model: str, judge_model: str,
                 min_reply_confidence: float = 0.75):
        self.llm = llm
        self.faq = faq_path.read_text(encoding="utf-8")
        self.agent_model = agent_model
        self.judge_model = judge_model
        self.min_reply_confidence = min_reply_confidence
        self.triage_system = TRIAGE_SYSTEM.replace("{faq}", self.faq)
        self.judge_system = JUDGE_SYSTEM.replace("{faq}", self.faq)

    # ------------------------------------------------------------------ public
    def decide(self, ticket: dict) -> Decision:
        subject, red_s = redact(ticket.get("subject", ""))
        body, red_b = redact(ticket.get("body", ""))
        meta: dict = {"redacted": sorted(set(red_s + red_b)), "escalated_because": []}

        if is_empty(subject, body):
            return Decision("close", {
                "language": "nl", "category": "other", "priority": "P4",
                "reason": "Empty ticket: no subject or message text to act on.",
                "confidence": 0.99,
            }, {**meta, "path": "empty"})

        t = self.llm.structured(model=self.agent_model, system=self.triage_system,
                                user=format_ticket(ticket, subject, body), schema=Triage)
        meta.update({"model_action": t.action, "model_hard_rules": list(t.hard_rules),
                     "faq_ids": t.faq_ids, "english_summary": t.english_summary})

        action, priority, category = t.action, t.priority, t.category
        confidence = max(0.0, min(1.0, t.confidence))

        # Hard rules: keyword guard OR model flag -> always a human.
        keyword_hits = match_hard_rules(f"{subject}\n{body}")
        rules = sorted(set(keyword_hits) | set(t.hard_rules))
        meta["keyword_hits"] = keyword_hits
        if rules:
            if action != "handoff":
                meta["escalated_because"].append(f"hard rule: {', '.join(rules)}")
            action = "handoff"
            for r in rules:
                priority = most_urgent(priority, PRIORITY_FLOOR[r])
            if "refund_or_disputed_charge" in rules:
                category = "billing"

        if action == "close" and confidence < CLOSE_MIN_CONFIDENCE:
            meta["escalated_because"].append(f"close confidence {confidence:.2f} too low")
            action = "handoff"

        if action == "reply":
            problems = self._reply_problems(t, confidence, subject, body, meta)
            if problems:
                meta["escalated_because"].extend(problems)
                action = "handoff"

        reason = t.reason.strip()
        if meta["escalated_because"]:
            reason = f"{reason} Escalated to a human: {meta['escalated_because'][0]}."
        payload = {
            "language": t.language,
            "category": category,
            "priority": priority,
            "reason": redact(reason)[0][:600],
            "confidence": round(confidence, 2),
        }
        if action == "reply":
            payload["draft_reply"] = redact(t.draft_reply)[0]
        elif action == "handoff":
            payload["handoff_note"] = self._handoff_note(t, meta)
        return Decision(action, payload, {**meta, "path": "llm"})

    # ----------------------------------------------------------------- helpers
    def _reply_problems(self, t: Triage, confidence: float, subject: str, body: str,
                        meta: dict) -> list[str]:
        problems: list[str] = []
        if confidence < self.min_reply_confidence:
            problems.append(f"reply confidence {confidence:.2f} < {self.min_reply_confidence}")
        cited = [f for f in t.faq_ids if f in FAQ_IDS]
        if not cited:
            problems.append("reply cites no help-centre entry")
        det = deterministic_checks(t.draft_reply, t.language, self.faq)
        meta["deterministic_check"] = det.issues
        problems.extend(det.issues)
        if problems:
            return problems  # don't pay for a judge call on a reply we already reject

        check = self.llm.structured(
            model=self.judge_model, system=self.judge_system, schema=ReplyCheck,
            user=(f"{format_ticket({}, subject, body)}\n\n"
                  f"Customer language: {t.language}\nCited help-centre entries: {', '.join(cited)}\n\n"
                  f"<draft_reply>\n{t.draft_reply}\n</draft_reply>"))
        meta["judge"] = {"verdict": check.verdict, "back_translation": check.back_translation,
                         "unsupported_claims": check.unsupported_claims,
                         "rule_violations": check.rule_violations, "notes": check.notes}
        if check.verdict != "pass":
            problems.append(f"reply check failed: {check.notes}")
        if check.unsupported_claims:
            problems.append(f"unsupported claims: {'; '.join(check.unsupported_claims)[:200]}")
        if check.rule_violations:
            problems.append(f"rule violations: {'; '.join(check.rule_violations)[:200]}")
        if not check.answers_every_question:
            problems.append("reply does not answer every question")
        if check.reply_language != t.language:
            problems.append(f"judge sees reply language {check.reply_language}")
        return problems

    @staticmethod
    def _handoff_note(t: Triage, meta: dict) -> str:
        note = t.handoff_note.strip() or t.english_summary.strip()
        if meta["escalated_because"]:
            note += f" [Agent: {meta['escalated_because'][0]}]"
        if meta["redacted"]:
            note += " [Payment details were removed from this ticket by the agent.]"
        return redact(note)[0][:1500]


def fallback_handoff(ticket: dict, why: str) -> Decision:
    """Used when the model call itself fails: never leave a ticket unanswered or guess."""
    lang = detect_language(f"{ticket.get('subject', '')} {ticket.get('body', '')}")
    return Decision("handoff", {
        "language": lang if lang in ("nl", "en", "de") else "nl",
        "category": "other", "priority": "P3",
        "reason": f"Automatic triage failed ({why}); a human should review this ticket.",
        "handoff_note": f"The agent could not triage this ticket ({why}). Please read and handle it manually.",
        "confidence": 0.0,
    }, {"path": "fallback", "escalated_because": [why]})
