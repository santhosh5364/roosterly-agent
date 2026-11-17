"""Client for the Roosterly helpdesk API.

Reliability rules, all in one place:
  * Client-side rate limit (default 4 req/s) so we stay under the 5 req/s cap.
  * 429  -> sleep `Retry-After`, then retry the same call.
  * 503  -> "nothing was saved": exponential backoff with jitter, then retry.
  * 409  -> AlreadyHandled. Never retried: the customer already has an answer.
  * A write whose outcome is unknown (timeout after sending, other 5xx) is NOT
    blindly retried. We GET the ticket first and only retry if it is still open.
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter
from typing import Any, Callable, Iterator

import httpx

log = logging.getLogger(__name__)

ACTIONS = ("reply", "handoff", "close")


class HelpdeskError(Exception):
    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(f"HTTP {status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class AlreadyHandled(HelpdeskError):
    """409: the ticket already has a decision. Must not be retried."""


class ValidationFailed(HelpdeskError):
    """422: our body was rejected; `detail` lists the problems. Nothing was saved."""


class NotFound(HelpdeskError):
    pass


class Unauthorized(HelpdeskError):
    pass


class AmbiguousWrite(Exception):
    """A write may or may not have been applied (e.g. response lost)."""


class RetriesExhausted(Exception):
    pass


class RateLimiter:
    """Spaces calls at least 1/max_per_second apart (single-threaded agent)."""

    def __init__(self, max_per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / max_per_second
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


def is_handled(ticket: dict) -> bool:
    """True if the helpdesk says this ticket already has a decision."""
    status = str(ticket.get("status") or "").lower()
    if status and status != "open":
        return True
    for key in ("handled", "handling", "decision", "handled_at", "action"):
        if ticket.get(key):
            return True
    return False


def _items(page: Any) -> list[dict]:
    if isinstance(page, list):
        return page
    for key in ("tickets", "items", "data", "examples", "results"):
        if isinstance(page.get(key), list):
            return page[key]
    raise ValueError(f"Unexpected list response shape: keys={list(page)}")


class HelpdeskClient:
    def __init__(self, base_url: str, api_key: str, email: str, name: str | None = None, *,
                 max_rps: float = 4.0, max_attempts: int = 6, timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Candidate-Email": email}
        self._name = name  # sent once, on the first request
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.limiter = RateLimiter(max_rps, sleep=sleep)
        self.stats: Counter[str] = Counter()

    # ---------------------------------------------------------------- transport
    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._name:
            headers["X-Candidate-Name"] = self._name
            self._name = None
        return headers

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25)
        log.info("retrying in %.2fs (%s)", delay, why)
        self.sleep(delay)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            return max(0.0, float(resp.headers.get("Retry-After", "1")))
        except ValueError:
            return 1.0

    @staticmethod
    def _error(resp: httpx.Response) -> HelpdeskError:
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text[:200]}
        code = str(body.get("error", "error")) if isinstance(body, dict) else "error"
        detail = body.get("problems") or body.get("message") if isinstance(body, dict) else body
        cls = {409: AlreadyHandled, 422: ValidationFailed, 404: NotFound, 401: Unauthorized}.get(
            resp.status_code, HelpdeskError)
        return cls(resp.status_code, code, detail)

    def _send(self, method: str, path: str, *, params: dict | None = None,
              body: dict | None = None, write: bool = False) -> httpx.Response:
        attempt = 0          # counts 503 / network failures
        throttled = 0        # counts 429s separately: they are expected, not failures
        while True:
            self.limiter.wait()
            self.stats["calls"] += 1
            try:
                resp = self._http.request(method, path, params=params, json=body,
                                          headers=self._request_headers())
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Never reached the server: always safe to retry.
                attempt += 1
                self.stats["connect_error"] += 1
                if attempt >= self.max_attempts:
                    raise RetriesExhausted(f"{method} {path}: {exc!r}") from exc
                self._backoff(attempt, f"connect error {type(exc).__name__}")
                continue
            except httpx.TransportError as exc:
                # The request may have reached the server.
                self.stats["transport_error"] += 1
                if write:
                    raise AmbiguousWrite(f"{method} {path}: {exc!r}") from exc
                attempt += 1
                if attempt >= self.max_attempts:
                    raise RetriesExhausted(f"{method} {path}: {exc!r}") from exc
                self._backoff(attempt, f"transport error {type(exc).__name__}")
                continue

            status = resp.status_code
            if status == 429:
                throttled += 1
                self.stats["429"] += 1
                if throttled > 3 * self.max_attempts:
                    raise RetriesExhausted(f"{method} {path}: still rate limited")
                wait = self._retry_after(resp)
                log.info("429 rate limited, waiting %.2fs", wait)
                self.sleep(wait)
                continue
            if status == 503:
                attempt += 1
                self.stats["503"] += 1
                if attempt >= self.max_attempts:
                    raise RetriesExhausted(f"{method} {path}: 503 x{attempt}")
                self._backoff(attempt, "503 service_unavailable")
                continue
            if status >= 500:
                self.stats[f"{status}"] += 1
                if write:
                    raise AmbiguousWrite(f"{method} {path}: HTTP {status}")
                attempt += 1
                if attempt >= self.max_attempts:
                    raise RetriesExhausted(f"{method} {path}: HTTP {status}")
                self._backoff(attempt, f"HTTP {status}")
                continue
            if status >= 400:
                self.stats[f"{status}"] += 1
                raise self._error(resp)
            return resp

    # --------------------------------------------------------------- endpoints
    def status(self) -> dict:
        return self._send("GET", "/status").json()

    def examples(self) -> list[dict]:
        return _items(self._send("GET", "/examples").json())

    def iter_tickets(self, status: str = "open") -> Iterator[dict]:
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 10, "status": status}
            if cursor:
                params["cursor"] = cursor
            page = self._send("GET", "/tickets", params=params).json()
            yield from _items(page)
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if not cursor:
                return

    def get_ticket(self, ticket_id: str) -> dict:
        data = self._send("GET", f"/tickets/{ticket_id}").json()
        # Accept either the bare ticket or {"ticket": {...}, "handling": {...}}.
        if isinstance(data.get("ticket"), dict):
            merged = dict(data["ticket"])
            merged.update({k: v for k, v in data.items() if k != "ticket"})
            return merged
        return data

    def reset(self) -> dict:
        return self._send("POST", "/practice/reset").json()

    def post_decision(self, ticket_id: str, action: str, payload: dict) -> str:
        """Send exactly one decision. Returns "posted" or "confirmed_after_ambiguity".

        Raises AlreadyHandled (409), ValidationFailed (422), RetriesExhausted.
        """
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._send("POST", f"/tickets/{ticket_id}/{action}", body=payload, write=True)
                return "posted"
            except AmbiguousWrite as exc:
                log.warning("%s: outcome unknown (%s); checking ticket before any retry",
                            ticket_id, exc)
                if is_handled(self.get_ticket(ticket_id)):
                    return "confirmed_after_ambiguity"
                self._backoff(attempt, "ambiguous write, ticket still open")
        raise RetriesExhausted(f"POST {ticket_id}/{action}")
