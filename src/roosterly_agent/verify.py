"""Checks a Dutch (or English/German) reply without anyone needing to read Dutch.

1. Deterministic checks, no model involved:
   - the reply is in the customer's language (langdetect);
   - every menu name next to an arrow (→) and every quoted UI label occurs in the help centre;
   - every URL/domain in the reply occurs in the help centre;
   - no payment details.
2. A separate judge call back-translates the reply to English and lists any
   claim that is not backed by the help centre.
A reply is only sent if both pass; otherwise the ticket goes to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langdetect import DetectorFactory, LangDetectException, detect, detect_langs

from .redact import contains_payment_details

DetectorFactory.seed = 0  # deterministic

# Double quotes only: Dutch uses apostrophes inside words (foto's, app's).
QUOTED_RE = re.compile(r"[\"“”„«»]([^\"“”„«»\n]{3,60})[\"“”„«»]")
URL_RE = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?:nl|com|de|eu|io|app)(?:/[^\s)\]]*)?)", re.I)
WORD_RE = re.compile(r"[\wÀ-ÿ.]+", re.U)


@dataclass
class CheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


def detect_language(text: str) -> str | None:
    try:
        lang = detect(text)
    except LangDetectException:
        return None
    return {"af": "nl"}.get(lang, lang)  # Afrikaans is a common false label for Dutch


def detect_language_confident(text: str, min_prob: float = 0.9, min_letters: int = 25) -> str | None:
    """Ticket language when the detector is sure (nl/en/de only), else None."""
    if sum(c.isalpha() for c in text) < min_letters:
        return None
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        return None
    lang = {"af": "nl"}.get(best.lang, best.lang)
    return lang if lang in ("nl", "en", "de") and best.prob >= min_prob else None


def _neighbour_words(reply: str) -> list[str]:
    """Words directly left and right of every arrow in the reply."""
    words: list[str] = []
    for line in reply.splitlines():
        if not re.search(r"→|->", line):
            continue
        parts = re.split(r"→|->", line)
        for i, part in enumerate(parts):
            part = re.sub(r"\([^)]*\)", " ", part)  # drop bracketed translations
            tokens = WORD_RE.findall(part)
            if not tokens:
                continue
            if i > 0:
                words.append(tokens[0])
            if i < len(parts) - 1:
                words.append(tokens[-1])
    return [w.strip(".").strip() for w in words if w.strip(".")]


def deterministic_checks(reply: str, expected_language: str, faq_text: str) -> CheckResult:
    issues: list[str] = []
    faq_lower = faq_text.lower()

    if not reply.strip():
        return CheckResult(False, ["empty reply"])

    lang = detect_language(reply)
    if lang and lang != expected_language:
        issues.append(f"reply language detected as {lang}, expected {expected_language}")

    for word in _neighbour_words(reply):
        if word.lower() not in faq_lower:
            issues.append(f"menu item not in help centre: {word!r}")

    for quoted in QUOTED_RE.findall(reply):
        q = quoted.strip()
        if re.search(r"[A-Za-z]", q) and q.lower() not in faq_lower:
            issues.append(f"quoted label not in help centre: {q!r}")

    for url in URL_RE.findall(reply):
        domain = url.lower().rstrip(".,")
        if domain not in faq_lower:
            issues.append(f"link not in help centre: {domain!r}")

    if contains_payment_details(reply):
        issues.append("payment details in reply")

    return CheckResult(not issues, issues)
