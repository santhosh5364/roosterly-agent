# Roosterly support-ticket agent

Reads every open ticket from the Roosterly helpdesk and sends exactly one decision per ticket:
a Dutch/English/German **reply** grounded in the help centre, a **handoff** with an English note, or **close** for spam/empty.

## How to run

```bash
git clone <repo> && cd roosterly-agent
uv sync                          # Python 3.11+, installs deps into .venv
cp .env.example .env             # fill in HELPDESK_API_KEY, CANDIDATE_EMAIL, LLM_API_KEY
uv run agent status              # check the helpdesk connection
uv run agent eval --label mytest # score on the 10 labelled examples (posts nothing)
uv run agent run --dry-run       # decide all open tickets, post nothing, log to logs/
uv run agent run --reset         # final run: re-open all 30, handle each once
uv run pytest                    # offline tests incl. a simulated flaky helpdesk + crash
```

## Decisions

1. **Safety is enforced in code, not only in the prompt.** The model proposes; deterministic gates decide.
   A keyword guard (nl/en/de) for the six "always a human" rules overrides the model; a reply is only sent if
   confidence ≥ 0.75, it cites a help-centre entry, passes the Dutch checks below, and an independent judge call
   finds no unsupported claim. Any failure turns the reply into a handoff. Unsure = human (the brief never punishes that).
2. **Dutch: work in Dutch directly, verify in English.** The model reads and writes Dutch (no translate-first step,
   which loses tone and costs a call), but also returns an English summary for the handoff note. I check a Dutch reply
   without reading Dutch by: (a) language detection, (b) every menu name next to an arrow, every quoted label and every
   link must literally exist in `faq.md` (catches invented menus like "Instellingen → Gebruikers"), (c) a judge call that
   back-translates the reply to English (logged as `reply_en`) and lists claims not backed by the FAQ.
3. **Open model, provider-agnostic.** Any OpenAI-compatible endpoint via `.env`. I used `openai/gpt-oss-120b`
   (open weights) on Groq's free tier. The same code runs on a local Ollama model if a client needs data on-premises.

**AI tools:** built with Claude Code (architecture, code, tests), reviewed and run by me; every design choice above is mine to defend.
**Assumptions where the brief was open:** "unsure" = model confidence < 0.75 or any failed check; upgrading to Plus is answered
from F05 (not a contract change), downgrading is; payment details are also removed from handoff notes, not only replies/logs;
ticket text is wrapped as untrusted data and injection attempts are handed off.

## Reliability

- **503**: exponential backoff with jitter (0.5 s → 8 s, 6 attempts); the API says nothing was saved, so retrying is safe.
- **429**: client-side limit of 4 req/s; on 429 wait `Retry-After` and retry.
- **409**: never retried, logged as `already_handled_409`. Designed not to happen: the agent GETs each ticket right before
  posting and skips it if handled.
- **Lost response** (timeout after sending): not blindly retried; the agent GETs the ticket and only retries if still open.
- **Crash**: the helpdesk is the source of truth. Open ids are snapshotted first (paging while handling would skip tickets);
  re-running the same command continues with what is still open. `state/ledger.jsonl` records intent/outcome per post.
  Tested with a simulated crash (`tests/test_flow.py`).
- **Model failure** (bad JSON twice, API error): the ticket is handed off, never guessed.
- **Security**: keys only from env (`.env` git-ignored, `.env.example` committed); IBANs/card numbers removed before the model,
  the reply, the note and the log; one log line per ticket in `logs/` with no ticket body.

## Eval and fix

_To be filled in from `eval/results/history.md`._

## What I would improve

_To be filled in._
