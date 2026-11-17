"""Thin wrapper around any OpenAI-compatible chat API (OpenAI, Gemini, Groq,
OpenRouter, Ollama, ...): one structured JSON call, validated with Pydantic,
plus token and cost accounting so every run can report what it cost.

The provider is chosen in .env (LLM_BASE_URL, LLM_API_KEY, AGENT_MODEL); no code change.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, base_url: str | None, api_key: str, *, json_mode: str = "schema",
                 price_in: float = 0.0, price_out: float = 0.0, reasoning_effort: str | None = None,
                 client: OpenAI | None = None):
        # max_retries: the SDK backs off on 429/5xx itself.
        self.client = client or OpenAI(base_url=base_url or None, api_key=api_key or "none",
                                       max_retries=6, timeout=120.0)
        self.json_mode = json_mode  # "schema" (strict JSON schema) or "object" (plain JSON mode)
        self.price_in, self.price_out = price_in, price_out  # USD per 1M tokens
        # Only for reasoning models that accept it (e.g. gpt-oss); saves tokens on free tiers.
        self.extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        self.usage: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "input": 0, "output": 0})

    def structured(self, *, model: str, system: str, user: str, schema: type[T],
                   max_tokens: int = 3000) -> T:
        json_schema = schema.model_json_schema()
        if self.json_mode == "schema":
            response_format = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": json_schema, "strict": True}}
            sys_prompt = system
        else:
            response_format = {"type": "json_object"}
            sys_prompt = (f"{system}\n\nRespond with one JSON object only, matching this JSON schema:\n"
                          f"{json.dumps(json_schema)}")

        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        for attempt in range(2):  # one repair attempt if the JSON does not validate
            resp = self.client.chat.completions.create(
                model=model, messages=messages, response_format=response_format,
                max_tokens=max_tokens, temperature=0, **self.extra)
            self._track(model, resp.usage)
            choice = resp.choices[0]
            text = choice.message.content or ""
            if choice.finish_reason not in ("stop", None):
                last_error = LLMError(f"finish_reason={choice.finish_reason}")
                continue
            try:
                return schema.model_validate_json(_strip_fences(text))
            except ValidationError as exc:
                last_error = exc
                log.warning("model output failed validation (attempt %d): %s", attempt + 1,
                            str(exc)[:200])
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That JSON was invalid: {str(exc)[:500]}. "
                                                "Return the corrected JSON object only."}]
        raise LLMError(f"no valid structured output: {last_error}")

    def _track(self, model: str, usage) -> None:
        u = self.usage[model]
        u["calls"] += 1
        if usage:
            u["input"] += usage.prompt_tokens or 0
            u["output"] += usage.completion_tokens or 0

    def cost_usd(self) -> float:
        return sum(u["input"] * self.price_in + u["output"] * self.price_out
                   for u in self.usage.values()) / 1e6

    def summary(self) -> dict:
        return {"by_model": dict(self.usage), "cost_usd": round(self.cost_usd(), 5),
                "price_per_1m_tokens_usd": {"input": self.price_in, "output": self.price_out}}


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
