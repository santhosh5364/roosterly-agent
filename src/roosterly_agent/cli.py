"""Command line: `agent run`, `agent eval`, `agent status`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .agent import TriageAgent
from .config import load_settings
from .evaluate import evaluate, load_examples
from .helpdesk import HelpdeskClient
from .llm import LLM
from .runner import run


def _helpdesk(s) -> HelpdeskClient:
    return HelpdeskClient(s.helpdesk_url, s.helpdesk_key, s.candidate_email, s.candidate_name)


def _agent(s, llm: LLM) -> TriageAgent:
    return TriageAgent(llm, s.faq_path, s.agent_model, s.judge_model, s.min_reply_confidence)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(prog="agent", description="Roosterly support-ticket agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="handle all open tickets")
    p_run.add_argument("--reset", action="store_true", help="POST /practice/reset first")
    p_run.add_argument("--dry-run", action="store_true", help="decide and log, but post nothing")
    p_run.add_argument("--limit", type=int, help="only the first N open tickets")

    p_eval = sub.add_parser("eval", help="score the agent on the 10 labelled examples")
    p_eval.add_argument("--label", default="eval", help="name for this eval run, e.g. before/after")
    p_eval.add_argument("--refresh", action="store_true", help="re-download the examples")

    sub.add_parser("status", help="show helpdesk status")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    s = load_settings(require_llm=args.cmd != "status")
    if args.cmd == "status":
        print(json.dumps(_helpdesk(s).status(), indent=2))
        return 0

    llm = LLM(s.llm_base_url, s.llm_api_key, json_mode=s.llm_json_mode,
              price_in=s.llm_price_in, price_out=s.llm_price_out,
              reasoning_effort=s.llm_reasoning_effort)
    agent = _agent(s, llm)
    if args.cmd == "run":
        summary = run(_helpdesk(s), agent, llm, log_dir=s.log_dir, state_dir=s.state_dir,
                      reset=args.reset, dry_run=args.dry_run, limit=args.limit)
        print(json.dumps(summary, indent=2))
        return 0 if not summary.get("still_open") else 1

    if args.cmd == "eval":
        examples = load_examples(_helpdesk(s), s.eval_dir / "examples.json", args.refresh)
        result = evaluate(agent, llm, examples, s.eval_dir, args.label)
        print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
