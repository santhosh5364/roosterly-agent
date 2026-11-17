"""End-to-end flow against a simulated helpdesk that misbehaves like the real one
(random 503s, 429s, 409 on double answers) and a scripted fake model.
Checks: every ticket gets exactly one decision, hard rules win over the model,
a crash mid-run is recovered by simply running again, logs have no payment details."""

import json
import random
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from roosterly_agent.agent import TriageAgent
from roosterly_agent.helpdesk import HelpdeskClient
from roosterly_agent.llm import LLM
from roosterly_agent.runner import run

FAQ = Path(__file__).resolve().parents[1] / "knowledge" / "faq.md"

TICKETS = {
    "T001": {"subject": "Nieuwe medewerker", "body": "Hoe voeg ik een nieuwe medewerker toe? Groet, Anouk"},
    "T002": {"subject": "Terugbetaling", "body": "Ik wil mijn geld terug. IBAN NL91 ABNA 0417 1643 00."},
    "T003": {"subject": "", "body": "   "},
    "T004": {"subject": "SEO aanbieding", "body": "Wij bieden goedkope backlinks aan voor uw website!"},
    "T005": {"subject": "App crasht", "body": "Sinds vanochtend crasht de app bij het inklokken."},
}


class FakeHelpdesk:
    def __init__(self, seed=1, fail_rate=0.25):
        self.rng = random.Random(seed)
        self.fail_rate = fail_rate
        self.decisions: dict[str, list] = {}
        self.posts = 0
        self.conflicts = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        roll = self.rng.random()
        if roll < 0.05:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate_limited"})
        if roll < self.fail_rate and (path.startswith("/tickets") and req.method in ("GET", "POST")):
            return httpx.Response(503, json={"error": "service_unavailable"})
        if path == "/practice/reset":
            self.decisions.clear()
            return httpx.Response(200, json={"reset": True})
        if path == "/tickets":
            open_ids = sorted(t for t in TICKETS if t not in self.decisions)
            cursor = req.url.params.get("cursor")
            start = open_ids.index(cursor) + 1 if cursor in open_ids else 0
            page = open_ids[start:start + 2]
            nxt = page[-1] if start + 2 < len(open_ids) else None
            return httpx.Response(200, json={"tickets": [{"id": i, **TICKETS[i]} for i in page],
                                             "next_cursor": nxt})
        parts = path.strip("/").split("/")
        tid = parts[1]
        if req.method == "GET":
            status = "handled" if tid in self.decisions else "open"
            return httpx.Response(200, json={"id": tid, "customer_plan": "Basis", "channel": "email",
                                             **TICKETS[tid], "status": status})
        action = parts[2]
        if tid in self.decisions:
            self.conflicts += 1
            return httpx.Response(409, json={"error": "already_handled"})
        self.posts += 1
        self.decisions.setdefault(tid, []).append((action, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})


GOOD_REPLY = ("Hoi Anouk,\n\nGa naar Team → Medewerker toevoegen, vul naam, e-mailadres en contracturen in "
              "en klik op Uitnodigen. Je medewerker krijgt daarna een e-mail om de app te installeren.\n\n"
              "Met vriendelijke groet,\nTeam Roosterly")


def triage_for(user: str) -> dict:
    base = {"english_summary": "summary", "language": "nl", "hard_rules": [], "faq_ids": [],
            "draft_reply": "", "handoff_note": "Customer needs help.", "confidence": 0.9,
            "reason": "Because."}
    if "medewerker" in user:
        return {**base, "category": "question", "priority": "P4", "action": "reply",
                "faq_ids": ["F02"], "draft_reply": GOOD_REPLY}
    if "geld terug" in user:
        # The model gets it WRONG on purpose: the keyword guard must overrule it.
        return {**base, "category": "billing", "priority": "P4", "action": "reply",
                "faq_ids": ["F06"], "draft_reply": "Hoi, je krijgt je geld terug."}
    if "backlinks" in user:
        return {**base, "category": "other", "priority": "P4", "action": "close", "confidence": 0.95}
    return {**base, "category": "bug", "priority": "P2", "action": "handoff"}


class FakeLLMClient:
    def __init__(self, crash_on: str | None = None):
        self.crash_on = crash_on
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        user = kw["messages"][-1]["content"]
        if self.crash_on and self.crash_on in user:
            raise KeyboardInterrupt("simulated crash")  # not caught by the agent: the process dies
        if "<draft_reply>" in user:
            out = {"back_translation": "Hi Anouk, go to Team → Add employee ...", "reply_language": "nl",
                   "answers_every_question": True, "unsupported_claims": [], "rule_violations": [],
                   "verdict": "pass", "notes": "Accurate per F02."}
        else:
            out = triage_for(user)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(out)), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=3000, completion_tokens=400))


def make(fake: FakeHelpdesk, llm_client, tmp_path):
    hd = HelpdeskClient("https://hd.test", "k", "me@test", transport=httpx.MockTransport(fake.handler),
                        sleep=lambda s: None)
    llm = LLM(None, "k", price_in=0.59, price_out=0.79, client=llm_client)
    agent = TriageAgent(llm, FAQ, "m", "m", 0.75)
    return hd, agent, llm


def test_full_run_one_decision_per_ticket(tmp_path):
    fake = FakeHelpdesk()
    hd, agent, llm = make(fake, FakeLLMClient(), tmp_path)
    summary = run(hd, agent, llm, log_dir=tmp_path / "logs", state_dir=tmp_path / "state", reset=True)

    assert summary["still_open"] == 0
    assert set(fake.decisions) == set(TICKETS)
    assert all(len(d) == 1 for d in fake.decisions.values())      # never two answers
    assert fake.conflicts == 0
    actions = {t: d[0][0] for t, d in fake.decisions.items()}
    assert actions == {"T001": "reply", "T002": "handoff", "T003": "close", "T004": "close", "T005": "handoff"}

    refund = fake.decisions["T002"][0][1]
    assert refund["priority"] == "P2" and refund["category"] == "billing"
    assert "draft_reply" not in refund and refund["handoff_note"]

    log_lines = Path(summary["log"]).read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == len(TICKETS)                          # one line per ticket
    assert "0417" not in Path(summary["log"]).read_text(encoding="utf-8")
    assert summary["helpdesk_calls"].get("503", 0) > 0              # chaos actually happened


def test_crash_then_rerun_finishes_without_double_answers(tmp_path):
    fake = FakeHelpdesk(seed=7)
    hd, agent, llm = make(fake, FakeLLMClient(crash_on="crasht"), tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run(hd, agent, llm, log_dir=tmp_path / "logs", state_dir=tmp_path / "state", reset=True)
    handled_before = set(fake.decisions)
    assert handled_before and handled_before != set(TICKETS)

    hd2, agent2, llm2 = make(fake, FakeLLMClient(), tmp_path)       # "restart the process"
    summary = run(hd2, agent2, llm2, log_dir=tmp_path / "logs", state_dir=tmp_path / "state")
    assert summary["still_open"] == 0
    assert set(fake.decisions) == set(TICKETS)
    assert all(len(d) == 1 for d in fake.decisions.values())
    assert fake.conflicts == 0
    assert summary["tickets"] == len(TICKETS) - len(handled_before)


def test_run_id_survives_redaction(tmp_path):
    fake = FakeHelpdesk(fail_rate=0)
    hd, agent, llm = make(fake, FakeLLMClient(), tmp_path)
    summary = run(hd, agent, llm, log_dir=tmp_path / "logs", state_dir=tmp_path / "state", limit=1)
    line = json.loads(Path(summary["log"]).read_text(encoding="utf-8").splitlines()[0])
    assert line["run"] == summary["run"]


class QuotaExhaustedClient(FakeLLMClient):
    """Provider dies after the first ticket (e.g. daily quota)."""

    def __init__(self):
        super().__init__()
        self.n = 0

    def _create(self, **kw):
        self.n += 1
        if self.n > 2:  # ticket 1 = triage + judge
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://llm.test"))
        return super()._create(**kw)


def test_model_outage_stops_run_and_leaves_tickets_open(tmp_path):
    fake = FakeHelpdesk(fail_rate=0)
    client = QuotaExhaustedClient()
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=client._create))
    hd, agent, llm = make(fake, client, tmp_path)
    summary = run(hd, agent, llm, log_dir=tmp_path / "logs", state_dir=tmp_path / "state", reset=True)
    assert summary["counts"].get("stopped_model_unavailable", 0) >= 1
    assert summary["still_open"] >= 1
    # Nothing content-free was posted: every posted decision came from a real triage.
    for decisions in fake.decisions.values():
        assert "Automatic triage failed" not in decisions[0][1]["reason"]
