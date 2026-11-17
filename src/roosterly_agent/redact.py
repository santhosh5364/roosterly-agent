"""Remove payment details (IBANs, card numbers) before text reaches the model,
a reply, a handoff note or a log line. Client rule: never repeat payment details."""

from __future__ import annotations

import re

# IBAN: 2 letters, 2 check digits, then 11-30 alphanumerics, optionally in groups of 4.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b", re.I)
# Card-like: 13-19 digits, optionally separated by spaces or dashes.
CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

IBAN_MASK = "[IBAN VERWIJDERD]"
CARD_MASK = "[KAARTNUMMER VERWIJDERD]"


def redact(text: str | None) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_found)."""
    if not text:
        return text or "", []
    found: list[str] = []

    def _iban(m: re.Match) -> str:
        # Must contain at least 10 digits overall to avoid masking ordinary words.
        if sum(c.isdigit() for c in m.group(0)) < 10:
            return m.group(0)
        found.append("iban")
        return IBAN_MASK

    def _card(m: re.Match) -> str:
        found.append("card")
        return CARD_MASK

    text = IBAN_RE.sub(_iban, text)
    text = CARD_RE.sub(_card, text)
    return text, found


def contains_payment_details(text: str | None) -> bool:
    return bool(redact(text)[1])
