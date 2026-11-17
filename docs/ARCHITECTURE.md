# Architecture

A **workflow agent with guardrails**: code owns the control flow, the LLM makes judgement calls inside it,
and deterministic checks can always turn a reply into a handoff (never the other way round).

## 1. Components

```mermaid
flowchart TB
    subgraph Interface
        CLI["cli.py<br/>agent run | eval | status"]
        CFG["config.py<br/>settings from .env"]
    end

    subgraph Orchestration
        RUN["runner.py<br/>live loop, crash safety,<br/>ledger, one log line per ticket"]
        EVAL["evaluate.py<br/>same pipeline on 10 labelled<br/>examples, scores vs labels"]
    end

    subgraph Decision["Decision (no helpdesk calls)"]
        AG["agent.py<br/>decide(ticket) -> Decision"]
    end

    subgraph Safety["Safety (plain code, no AI)"]
        RED["redact.py<br/>IBAN / card removal"]
        RUL["rules.py<br/>6 hard rules nl/en/de,<br/>priority floors, empty check"]
        VER["verify.py<br/>language, menus, links,<br/>payment details"]
    end

    subgraph AI
        LLM["llm.py<br/>OpenAI-compatible client,<br/>JSON schema, repair, cost"]
        PR["prompts.py<br/>triage + judge prompts,<br/>output schemas"]
        FAQ[("knowledge/faq.md<br/>only allowed knowledge")]
    end

    subgraph Integration
        HD["helpdesk.py<br/>rate limit, 503/429 retry,<br/>409 stop, lost-response check"]
    end

    API[["Roosterly helpdesk API"]]
    PROV[["LLM provider<br/>(Groq: gpt-oss-120b)"]]
    LOGS[("logs/*.jsonl<br/>state/ledger.jsonl<br/>eval/results/")]

    CLI --> CFG
    CLI --> RUN
    CLI --> EVAL
    RUN --> AG
    EVAL --> AG
    AG --> RED
    AG --> RUL
    AG --> VER
    AG --> LLM
    LLM --> PR
    PR --> FAQ
    VER --> FAQ
    LLM --> PROV
    RUN --> HD
    EVAL --> HD
    HD --> API
    RUN --> LOGS
    EVAL --> LOGS
```

| Layer | Files | Responsibility |
|---|---|---|
| Interface | `cli.py`, `config.py` | One command; secrets and model choice from environment variables |
| Orchestration | `runner.py`, `evaluate.py` | Loop over tickets, crash recovery, logging, scoring |
| Decision | `agent.py` | The per-ticket pipeline; pure logic, so the eval exercises the production path |
| Safety | `redact.py`, `rules.py`, `verify.py` | Deterministic gates that can overrule the model |
| AI | `llm.py`, `prompts.py`, `knowledge/faq.md` | Structured model calls, provider-agnostic |
| Integration | `helpdesk.py` | Every reliability rule for the helpdesk API in one place |

## 2. Per-ticket decision flow

```mermaid
flowchart TD
    A([Ticket]) --> B["Redact IBANs and card numbers"]
    B --> C{Any letters in<br/>subject or body?}
    C -- no --> CLOSE1([CLOSE<br/>other / P4])
    C -- yes --> D["Detect language<br/>if confidence >= 0.9"]
    D --> E["LLM triage<br/>FAQ + rules + ticket as untrusted data<br/>-> language, category, priority, action,<br/>hard_rules, faq_ids, draft, note, confidence"]
    E --> F{Detector sure and<br/>disagrees with model?}
    F -- yes --> F2[Use detected language]
    F -- no --> G
    F2 --> G{Keyword guard OR model flags<br/>refund, cancellation, GDPR,<br/>security, legal, churn?}
    G -- yes --> HO1(["HANDOFF<br/>priority floor P1/P2,<br/>refund = billing"])
    G -- no --> H{Model action}
    H -- handoff --> HO2([HANDOFF<br/>English note])
    H -- close --> I{"confidence >= 0.8?"}
    I -- yes --> CLOSE2([CLOSE])
    I -- no --> HO3([HANDOFF])
    H -- reply --> J{"confidence >= 0.75<br/>and cites F01-F12?"}
    J -- no --> HO4([HANDOFF])
    J -- yes --> K{"Code checks:<br/>reply language,<br/>menu names + links exist in FAQ,<br/>no payment details"}
    K -- fail --> HO5([HANDOFF])
    K -- pass --> L["LLM judge<br/>back-translate to English,<br/>list unsupported claims,<br/>rule violations, answers all?"]
    L -- fail --> HO6([HANDOFF])
    L -- pass --> R([REPLY<br/>in customer's language])

    style R fill:#d4edda,stroke:#28a745
    style CLOSE1 fill:#e2e3e5
    style CLOSE2 fill:#e2e3e5
    style HO1 fill:#fff3cd,stroke:#d39e00
    style HO2 fill:#fff3cd,stroke:#d39e00
    style HO3 fill:#fff3cd,stroke:#d39e00
    style HO4 fill:#fff3cd,stroke:#d39e00
    style HO5 fill:#fff3cd,stroke:#d39e00
    style HO6 fill:#fff3cd,stroke:#d39e00
```

Every gate fails safe: doubt moves a ticket towards a human. A failed model call for one ticket is a
handoff; a provider outage stops the run (section 4).

## 3. Run flow and helpdesk reliability

```mermaid
sequenceDiagram
    autonumber
    participant R as runner.py
    participant H as helpdesk.py
    participant API as Helpdesk API
    participant A as agent.py
    participant L as ledger / logs

    opt --reset (final run)
        R->>H: reset()
        H->>API: POST /practice/reset
    end
    R->>H: iter_tickets("open")
    loop every page until next_cursor is null
        H->>API: GET /tickets?status=open&cursor=..
    end
    Note over R: Snapshot all ids first: handling tickets<br/>changes the open set while paging

    loop each ticket id
        R->>H: get_ticket(id)
        H->>API: GET /tickets/{id}
        alt already handled
            R->>L: skip (no second answer)
        else open
            R->>A: decide(ticket)
            A-->>R: Decision(action, payload)
            R->>L: ledger: intent
            R->>H: post_decision(id, action, payload)
            H->>API: POST /tickets/{id}/{action}
            alt 200
                API-->>H: ok
            else 503 (nothing saved)
                H->>H: backoff 0.5s..8s + jitter, retry
            else 429
                H->>H: sleep Retry-After, retry
            else timeout / unknown 5xx (maybe saved)
                H->>API: GET /tickets/{id}
                Note over H: retry only if still open
            else 409 already_handled
                Note over H: never retried
            else 422 validation_failed
                H->>API: POST /tickets/{id}/handoff (fallback)
            end
            R->>L: ledger: outcome + one log line
        end
    end
    R->>H: count open tickets (should be 0)
```

All calls pass a client-side limiter of 4 requests/second (the API allows 5).

## 4. Failure handling

```mermaid
flowchart LR
    subgraph Helpdesk
        E1[503] --> X1[Backoff and retry, max 6]
        E2[429] --> X2[Wait Retry-After]
        E3[409] --> X3[Stop: already answered]
        E4[Lost response] --> X4[GET ticket, retry only if open]
        E5[422] --> X5[Fall back to handoff]
    end
    subgraph Model
        M1[Invalid JSON] --> Y1[One repair prompt]
        Y1 -- still invalid --> Y2[Handoff for this ticket]
        M2[Quota / outage / auth<br/>after SDK retries] --> Y3[Stop run,<br/>tickets stay open]
    end
    subgraph Process
        P1[Crash or Ctrl+C] --> Z1[Run same command again:<br/>handled tickets are not open,<br/>ledger shows last intent]
    end
```

## 5. Data and security

```mermaid
flowchart LR
    T[Ticket text] --> RD[redact.py]
    RD --> LLMIN[Model input<br/>wrapped in ticket tags,<br/>treated as data]
    LLMIN --> OUT[Reply / note]
    OUT --> RD2[redact again]
    RD2 --> API[Helpdesk]
    RD2 --> LOG[Log line<br/>no ticket body,<br/>redacted once more on write]
    ENV[.env, git-ignored] --> KEYS[Helpdesk key,<br/>LLM key]
```

- Keys only from environment variables; `.env.example` documents them.
- Payment details are removed before the model, from the reply and note, and again when a log line is written.
- Ticket text is wrapped in `<ticket>` tags and the prompt says it is data, never instructions; injection attempts are handed off.
- Logs hold decisions, reasons and the English back-translation of replies, not the customer's message.

## 6. Design decisions

| Decision | Alternative | Why |
|---|---|---|
| Fixed workflow, code-controlled | Tool-calling agent that decides its own steps | Predictable, testable, easy to explain; the task is one decision per ticket |
| Work in Dutch, verify in English | Translate first, decide in English | One call instead of two, keeps Dutch tone; the judge's back-translation lets a non-Dutch reader check replies |
| Keyword guard on top of the model | Trust the model's rule flags | A missed refund or legal threat reaching an auto-reply is the costliest error; false positives only cost a human a minute |
| Menu/link check against `faq.md` | Only the LLM judge | Catches invented menus cheaply and deterministically (caught one in eval) |
| Helpdesk is the source of truth | Local database of handled tickets | A reset or a manual change on the server can never desync; re-running is always safe |
| OpenAI-compatible client | One vendor SDK | Same code runs on Groq, OpenAI, Gemini or a local Ollama model |
