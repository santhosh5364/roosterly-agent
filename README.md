# Roosterly support-ticket agent

Handles every open helpdesk ticket once: a **reply** in the customer's language from the help centre, a **handoff** with an
English note, or **close** for spam/empty. Architecture and flowcharts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How to run

```bash
uv sync                           # Python 3.11+
cp .env.example .env              # fill HELPDESK_API_KEY, CANDIDATE_EMAIL, LLM_API_KEY
uv run agent status               # connection check
uv run agent eval --label try     # score on the 10 labelled examples (posts nothing)
uv run agent run --dry-run        # decide all open tickets, post nothing
uv run agent run --reset          # final run: re-open all 30, handle each once
uv run pytest                     # offline tests: flaky helpdesk, crash, outage, safety
```

## Decisions

1. **The model proposes, code decides.** A keyword guard (nl/en/de) for the six "always a human" rules overrides the model.
   A reply is sent only if confidence ≥ 0.75, it cites an FAQ entry, and it passes the Dutch checks below; any failure becomes a handoff.
2. **Dutch: decide in Dutch, verify in English.** No translate-first step (one call, keeps tone). I check a reply without reading
   Dutch: language detection; every menu name, quoted label and link must exist in `faq.md`; a judge call back-translates
   the reply (`reply_en` in the log) and lists claims the FAQ does not support.
3. **Open model, any provider.** `openai/gpt-oss-120b` on Groq's free tier via an OpenAI-compatible client; a local Ollama model
   works by editing `.env` (for clients who need data on-premises).

**AI tools:** built with Claude Code (design, code, tests, eval analysis); I ran and reviewed every step.
**Assumptions:** unsure = confidence < 0.75 or any failed check; upgrading to Plus is answerable (F05), downgrading is a contract
change; payment details are also removed from handoff notes; prompt-injection attempts are handed off.

## Reliability

- **503**: backoff 0.5–8 s with jitter, 6 attempts (nothing was saved). **429**: 4 req/s client limit, then wait `Retry-After`.
- **409**: never retried. Avoided by GETting each ticket right before posting. **Lost response**: GET the ticket, retry only if open.
- **Crash**: the helpdesk is the source of truth; rerun the same command. Ids are snapshotted first (paging while handling skips
  tickets); `state/ledger.jsonl` records intent and outcome. **Model outage/quota**: the run stops, tickets stay open.
- **Security**: keys only from env (`.env` ignored, `.env.example` committed); IBANs/cards removed before the model, the reply,
  the note and the log; ticket text is treated as data; one log line per ticket in `logs/`, without the ticket body.

## Eval and fix

Same pipeline on the 10 examples from `GET /examples`, scored against Nordavo's labels (`eval/results/history.md`).

| run | lang | category | priority | action | unsafe replies | FAQ tickets escalated |
|---|---|---|---|---|---|---|
| baseline | 90% | 70% | 80% | 80% | 0 | 2/6 |
| fix 1 | 80% | 70% | 80% | 80% | 0 | 2/6 |
| fix 2 | 100% | 100% | 80% | 100% | 0 | 0/6 |

**Fixed: answerable tickets sent to a human.** E05 (feature idea) and E10 (maintenance) are answered by F10/F11, but were escalated.
Fix 1 (prompts: pointing to the roadmap/status page is an answer) fixed E10, but exposed the model labelling English tickets as
Dutch (E01 became a Dutch reply with an invented menu, caught by the menu check). Fix 2 added a code-level language guard and an
explicit F10 rule. Caveat: 10 examples are dev and test set at once.

**Cost per 100 tickets:** fix-2 eval used 34,054 input + 4,332 output tokens for 10 tickets. At Groq's list price ($0.15/$0.60 per 1M):
100 × (3,405 × 0.15 + 433 × 0.60) / 1M = $0.051 + $0.026 = **≈ $0.08**. Actual spend: €0 (free tier).

## What I would improve

**Next:** ~100 tickets labelled by the support team with a held-out split, then calibrate priority (the model over-uses P2).
**Brief change:** P2 says "money or the contract is involved", yet a VAT-number how-to is P4 in your labels. I would define P2 as
"money at stake" (disputed charge, refund, cancellation) so agent and team triage alike.
