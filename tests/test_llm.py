from types import SimpleNamespace

import pytest

from roosterly_agent.llm import LLM, LLMError
from roosterly_agent.prompts import ReplyCheck

GOOD = ('{"back_translation": "Hi", "reply_language": "nl", "answers_every_question": true, '
        '"unsupported_claims": [], "rule_violations": [], "verdict": "pass", "notes": "ok"}')


class FakeClient:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        text = self.outputs.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200))


def test_parses_and_counts_cost():
    llm = LLM(None, "k", price_in=1.0, price_out=2.0, client=FakeClient([GOOD]))
    out = llm.structured(model="m", system="s", user="u", schema=ReplyCheck)
    assert out.verdict == "pass"
    assert llm.cost_usd() == pytest.approx((1000 * 1.0 + 200 * 2.0) / 1e6)


def test_repairs_invalid_json_once():
    fake = FakeClient(['{"verdict": "pass"}', "```json\n" + GOOD + "\n```"])
    out = LLM(None, "k", client=fake).structured(model="m", system="s", user="u", schema=ReplyCheck)
    assert out.notes == "ok" and len(fake.calls) == 2


def test_gives_up_after_two_invalid():
    fake = FakeClient(["nope", "still nope"])
    with pytest.raises(LLMError):
        LLM(None, "k", client=fake).structured(model="m", system="s", user="u", schema=ReplyCheck)


def _rate_limit(msg="Rate limit reached ... tokens per minute (TPM)"):
    import httpx
    import openai
    resp = httpx.Response(429, headers={"retry-after": "7"},
                          request=httpx.Request("POST", "https://llm.test"))
    return openai.RateLimitError(msg, response=resp, body=None)


def test_waits_out_per_minute_rate_limit():
    fake = FakeClient([GOOD])
    calls = {"n": 0}
    real = fake.chat.completions.create

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _rate_limit()
        return real(**kw)

    fake.chat.completions.create = flaky
    slept = []
    out = LLM(None, "k", client=fake, sleep=slept.append).structured(
        model="m", system="s", user="u", schema=ReplyCheck)
    assert out.verdict == "pass" and slept == [7.0, 7.0]


def test_daily_quota_is_not_waited_out():
    import openai
    fake = FakeClient([GOOD])

    def daily(**kw):
        raise _rate_limit("Rate limit reached ... tokens per day (TPD)")

    fake.chat.completions.create = daily
    with pytest.raises(openai.RateLimitError):
        LLM(None, "k", client=fake, sleep=lambda s: None).structured(
            model="m", system="s", user="u", schema=ReplyCheck)
