"""Settings, read from environment variables (optionally via a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HELPDESK_URL = "https://nakywgkztvzkihdjnwbc.supabase.co/functions/v1/helpdesk"


@dataclass(frozen=True)
class Settings:
    helpdesk_url: str
    helpdesk_key: str
    candidate_email: str
    candidate_name: str | None
    llm_base_url: str | None
    llm_api_key: str
    llm_json_mode: str
    llm_price_in: float
    llm_price_out: float
    llm_reasoning_effort: str | None
    agent_model: str
    judge_model: str
    min_reply_confidence: float
    faq_path: Path
    log_dir: Path
    state_dir: Path
    eval_dir: Path


def load_settings(require_helpdesk: bool = True, require_llm: bool = True) -> Settings:
    load_dotenv(ROOT / ".env")
    key = os.getenv("HELPDESK_API_KEY", "").strip()
    email = os.getenv("CANDIDATE_EMAIL", "").strip()
    if require_helpdesk and (not key or not email):
        raise SystemExit("Set HELPDESK_API_KEY and CANDIDATE_EMAIL (see .env.example).")
    agent_model = os.getenv("AGENT_MODEL", "").strip()
    if require_llm and not agent_model:
        raise SystemExit("Set AGENT_MODEL (and LLM_BASE_URL / LLM_API_KEY) in .env.")
    return Settings(
        helpdesk_url=os.getenv("HELPDESK_URL", DEFAULT_HELPDESK_URL).rstrip("/"),
        helpdesk_key=key,
        candidate_email=email,
        candidate_name=os.getenv("CANDIDATE_NAME", "").strip() or None,
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip() or None,
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_json_mode=os.getenv("LLM_JSON_MODE", "schema").strip(),
        llm_price_in=float(os.getenv("LLM_PRICE_IN_PER_1M", "0") or 0),
        llm_price_out=float(os.getenv("LLM_PRICE_OUT_PER_1M", "0") or 0),
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip() or None,
        agent_model=agent_model,
        judge_model=os.getenv("JUDGE_MODEL", "").strip() or agent_model,
        min_reply_confidence=float(os.getenv("MIN_REPLY_CONFIDENCE", "0.75")),
        faq_path=ROOT / "knowledge" / "faq.md",
        log_dir=ROOT / "logs",
        state_dir=ROOT / "state",
        eval_dir=ROOT / "eval",
    )
