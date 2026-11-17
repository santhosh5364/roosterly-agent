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
