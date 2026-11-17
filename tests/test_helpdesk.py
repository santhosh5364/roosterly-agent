import httpx
import pytest

from roosterly_agent.helpdesk import AlreadyHandled, HelpdeskClient, RetriesExhausted, ValidationFailed


def client(handler, **kw):
    return HelpdeskClient("https://hd.test", "k", "me@test", transport=httpx.MockTransport(handler),
                          sleep=lambda s: None, **kw)


def test_503_is_retried_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": "service_unavailable"})
        return httpx.Response(200, json={"ok": True})

    assert client(handler).status() == {"ok": True}
    assert len(calls) == 3


def test_429_waits_retry_after():
    slept, calls = [], []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate_limited"})
        return httpx.Response(200, json={"ok": True})

    hd = HelpdeskClient("https://hd.test", "k", "me@test", transport=httpx.MockTransport(handler),
                        sleep=slept.append)
    hd.status()
    assert 2.0 in slept


def test_409_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(409, json={"error": "already_handled"})

    with pytest.raises(AlreadyHandled):
        client(handler).post_decision("T1", "reply", {"x": 1})
    assert len(calls) == 1


def test_422_raises_with_problems():
    def handler(req):
        return httpx.Response(422, json={"error": "validation_failed", "problems": ["draft_reply missing"]})

    with pytest.raises(ValidationFailed) as e:
        client(handler).post_decision("T1", "reply", {})
    assert e.value.detail == ["draft_reply missing"]


def test_lost_response_checks_ticket_instead_of_double_posting():
    posts = []

    def handler(req):
        if req.method == "POST":
            posts.append(req)
            raise httpx.ReadTimeout("response lost", request=req)
        return httpx.Response(200, json={"id": "T1", "status": "handled"})

    assert client(handler).post_decision("T1", "reply", {}) == "confirmed_after_ambiguity"
    assert len(posts) == 1


def test_503_on_post_gives_up_after_max_attempts():
    def handler(req):
        return httpx.Response(503, json={"error": "service_unavailable"})

    with pytest.raises(RetriesExhausted):
        client(handler, max_attempts=3).post_decision("T1", "close", {})


def test_pagination_follows_cursor():
    def handler(req):
        cursor = req.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"tickets": [{"id": "T1"}], "next_cursor": "T1"})
        return httpx.Response(200, json={"tickets": [{"id": "T2"}], "next_cursor": None})

    assert [t["id"] for t in client(handler).iter_tickets()] == ["T1", "T2"]


def test_headers_sent():
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, json={})

    client(handler).status()
    assert seen["authorization"] == "Bearer k"
    assert seen["x-candidate-email"] == "me@test"
