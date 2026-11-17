"""Deterministic safety net for the client's "always a human" rules.

The model also flags these, but a keyword match here forces a handoff no matter
what the model says. False positives only cost a human a minute; false
negatives send a refund or legal threat to an automated reply. Patterns cover
Dutch, English and German.
"""

from __future__ import annotations

import re

HARD_RULES = (
    "refund_or_disputed_charge",
    "cancellation_or_contract_change",
    "personal_data_request",
    "security_incident",
    "legal_threat",
    "churn_threat",
)

_PATTERNS: dict[str, list[str]] = {
    "refund_or_disputed_charge": [
        r"terug ?betal", r"terugstort", r"geld terug", r"restitutie", r"crediteren", r"creditnota",
        r"dubbel (?:afgeschreven|betaald|gefactureerd)", r"onterecht (?:afgeschreven|gefactureerd|in rekening)",
        r"te veel (?:betaald|gefactureerd|afgeschreven)", r"storneren", r"stornering",
        r"refund", r"money back", r"charged twice", r"double charged", r"overcharged", r"chargeback",
        r"dispute", r"r[üu]ckerstatt", r"erstatt", r"geld zur[üu]ck", r"doppelt (?:abgebucht|berechnet)",
    ],
    "cancellation_or_contract_change": [
        r"opzeg", r"zeg(?:gen)? .{0,20}op\b", r"be[eë]indig", r"stopzetten", r"abonnement stoppen",
        r"downgrad", r"terug naar (?:het )?basis", r"contract (?:wijzig|aanpass|ontbind)",
        r"cancel", r"terminate", r"end (?:our|my) (?:contract|subscription)",
        r"k[üu]ndig", r"vertrag (?:beenden|[äa]ndern)",
    ],
    "personal_data_request": [
        r"\bAVG\b", r"\bGDPR\b", r"\bDSGVO\b", r"inzageverzoek", r"recht op (?:inzage|vergetelheid|verwijdering)",
        r"(?:verwijder|wis).{0,30}(?:gegevens|data)", r"(?:gegevens|data).{0,30}(?:verwijder|wissen)",
        r"persoonsgegevens", r"welke gegevens.{0,30}(?:bewaren|opslaan|hebben)",
        r"delete (?:my|our|all) (?:personal )?data", r"data (?:access|deletion|subject) request",
        r"auskunftsersuchen", r"auskunft nach", r"daten l[öo]schen", r"personenbezogen",
    ],
    "security_incident": [
        r"gehackt", r"gehacked", r"hack", r"datalek", r"inbraak", r"ingebroken",
        r"onbekende? (?:login|inlog|apparaat|ip)", r"verdachte? (?:login|inlog|activiteit|mail)",
        r"niet (?:door mij|door ons) ingelogd", r"wachtwoord (?:gelekt|gestolen)", r"phishing",
        r"zonder toestemming (?:ingelogd|gewijzigd|toegang)", r"ongeautoriseerd",
        r"data breach", r"unauthori[sz]ed", r"compromised", r"suspicious login",
        r"datenleck", r"sicherheitsvorfall", r"unbefugt",
    ],
    "legal_threat": [
        r"advocaat", r"jurist", r"juridische stappen", r"rechtszaak", r"naar de rechter", r"aansprakelijk",
        r"ingebrekestelling", r"sommatie", r"consumentenbond", r"autoriteit persoonsgegevens",
        r"lawyer", r"attorney", r"legal action", r"\bsue\b", r"lawsuit", r"liable",
        r"anwalt", r"rechtliche schritte", r"\bklage\b", r"verklagen",
    ],
    "churn_threat": [
        r"(?:andere|nieuwe) (?:aanbieder|leverancier|planningstool|tool|app)", r"concurrent",
        r"overstappen naar (?!plus)", r"we (?:gaan|stappen) (?:weg|over)", r"vertrekken bij",
        r"weglopen",
        r"switch(?:ing)? to (?:another|a different|a competitor)", r"leave roosterly", r"take our business",
        r"anbieter wechseln", r"zur konkurrenz",
    ],
}

_COMPILED = {rule: re.compile("|".join(p), re.I) for rule, p in _PATTERNS.items()}


def match_hard_rules(text: str) -> dict[str, str]:
    """Return {rule: matched_snippet} for every hard rule that matches."""
    hits: dict[str, str] = {}
    for rule, rx in _COMPILED.items():
        m = rx.search(text or "")
        if m:
            hits[rule] = m.group(0)
    return hits


# Priority floor per rule (brief: P1 = security or legal risk; P2 = money or contract).
PRIORITY_FLOOR = {
    "security_incident": "P1",
    "legal_threat": "P1",
    "refund_or_disputed_charge": "P2",
    "cancellation_or_contract_change": "P2",
    "personal_data_request": "P2",
    "churn_threat": "P2",
}


def most_urgent(a: str, b: str) -> str:
    return min(a, b)  # "P1" < "P2" < ...


_INJECTION = re.compile("|".join([
    r"(?:negeer|vergeet|ignore|disregard|ignoriere|vergiss).{0,30}(?:instructie|instructions?|anweisung|regels|rules)",
    r"systeem ?bericht", r"system ?(?:message|prompt|notice)", r"systemnachricht",
    r"(?:notitie|bericht|opmerking|note|message|hinweis) (?:voor|for|an|f[üu]r) (?:de |the |den )?(?:ai|a\.i\.|bot|assistent|assistant|agent|model|ki)",
    r"\b(?:ai|ki)[- ](?:assistent|assistant|agent)", r"you are now", r"je bent nu", r"act as",
    r"(?:zet|set|change|markeer|mark) (?:de |the )?(?:prioriteit|priority|status|ticket)",
]), re.I)


def match_injection(text: str) -> str | None:
    """Text addressed to the AI that tries to steer the outcome. Returns the matched snippet."""
    m = _INJECTION.search(text or "")
    return m.group(0) if m else None


def is_empty(subject: str | None, body: str | None) -> bool:
    """No letters anywhere: nothing a human or the agent could act on."""
    text = f"{subject or ''} {body or ''}"
    return not re.search(r"[^\W\d_]", text)
