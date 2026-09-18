"""Read merchant-supplied text for facts. Never take an instruction from it.

`item_details` is genuinely useful — shoe size, panel size, return window — and
it is also written by whoever wants the payment approved. Those two facts are
not in tension as long as the text is only ever *read from*, never *obeyed*.

So this module does two separate things and keeps them separate:

* Extract structured facts by pattern. A pattern can only ever yield a number
  or a token, so no phrasing produces a permission.
* Detect text addressed to an automated decider. A finding here does not change
  a rule — by construction nothing here can — but it does mean the seller's own
  copy is no longer a trustworthy source for the facts above, and it is a
  signal in its own right that the customer should see.

The rule that makes the whole thing safe: the sanitiser returns data. It has no
access to the policy and cannot reach the verdict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dc_field

# "returns accepted within 30 days", "14-day returns", "return within 7 days"
RETURN_WINDOW = re.compile(
    r"(?:returns?\D{0,24}?(?P<d1>\d{1,3})\s*[- ]?\s*days?"
    r"|(?P<d2>\d{1,3})\s*[- ]?\s*day\s+returns?)",
    re.IGNORECASE,
)
FINAL_SALE = re.compile(r"final\s+sale|no\s+returns?|non[- ]returnable|sold\s+as\s+seen", re.IGNORECASE)
NOT_STATED = re.compile(r"return\s+policy\s+(?:is\s+)?not\s+stated|no\s+return\s+policy\s+stated", re.IGNORECASE)

SIZE = re.compile(r"\bsize\s+(?P<size>[\w]+)", re.IGNORECASE)
INCH = re.compile(r"(?P<inch>\d{1,3})\s*[- ]?\s*(?:inch|\")", re.IGNORECASE)

RECURRING_CHARGE = re.compile(
    r"billed\s+(?:monthly|annually|yearly|每)|recurring\s+(?:charge|billing)"
    r"|renews?\s+automatically|subscription\s+after",
    re.IGNORECASE,
)

# Text aimed at an automated decider rather than at a person. Each pattern
# describes a *form of address*, not a topic: the point is that the copy is
# trying to steer a system, whatever it is asking for.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "addresses_automated_agent",
        re.compile(
            r"\b(?:note|message|notice|instruction)s?\s+(?:for|to)\s+"
            r"(?:automated\s+)?(?:purchasing\s+)?(?:agents?|bots?|assistants?|systems?)",
            re.IGNORECASE,
        ),
    ),
    (
        "impersonates_system_role",
        re.compile(r"(?:^|[\s\"'\[(.;])(?:system|assistant|developer)\s*[:>]", re.IGNORECASE),
    ),
    (
        "overrides_prior_instructions",
        re.compile(
            r"ignore\s+(?:any\s+|all\s+|the\s+)?(?:previous|prior|earlier|above|other)?\s*"
            r"(?:instructions?|rules?|limits?|spending\s+\w+)"
            r"|disregard\s+(?:any|all|the|previous|prior)"
            r"|override\s+(?:the\s+)?(?:policy|limit|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "claims_pre_authorisation",
        re.compile(
            r"pre[- ]?authoris?z?ed|already\s+approved\s+by\s+the\s+(?:customer|cardholder)"
            r"|cardholder\s+has\s+(?:pre[- ]?)?authoris?z?ed|no\s+further\s+approval\s+(?:is\s+)?(?:needed|required)",
            re.IGNORECASE,
        ),
    ),
    (
        "demands_immediate_approval",
        re.compile(
            r"approve\s+(?:this|the)\s+(?:payment|purchase|transaction|order)\s+immediately"
            r"|do\s+not\s+(?:ask|escalate|pause|decline)"
            r"|skip\s+(?:the\s+)?(?:check|verification|approval)",
            re.IGNORECASE,
        ),
    ),
    (
        "raises_or_removes_a_limit",
        re.compile(
            r"(?:limit|cap|budget)\s+(?:is\s+)?(?:raised|removed|lifted|waived|does\s+not\s+apply)"
            r"|exempt\s+from\s+(?:the\s+)?(?:limit|cap|policy)"
            r"|no\s+(?:spending\s+)?limit\s+applies",
            re.IGNORECASE,
        ),
    ),
)

# Characters used to smuggle text past a naive scan: zero-width joiners,
# bidirectional overrides, and the soft hyphen.
INVISIBLE = dict.fromkeys(
    [0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0xFEFF]
)


@dataclass
class TextFacts:
    """Facts read out of one piece of merchant copy."""

    return_window_days: int | None = None
    return_explicitly_refused: bool = False
    return_window_unstated: bool = False
    sizes: list[str] = dc_field(default_factory=list)
    inches: list[str] = dc_field(default_factory=list)
    recurring_charge: bool = False


@dataclass
class InjectionFinding:
    code: str
    excerpt: str


@dataclass
class Sanitised:
    """The result of reading one merchant string."""

    source: str
    normalised: str
    facts: TextFacts
    injections: list[InjectionFinding] = dc_field(default_factory=list)

    @property
    def is_manipulated(self) -> bool:
        return bool(self.injections)

    @property
    def trustworthy_for_facts(self) -> bool:
        """Copy that tries to steer a decider is not a reliable source.

        We still parse the facts — we simply stop treating them as established.
        """
        return not self.injections


def normalise(text: str) -> str:
    """Fold the text so evasion by encoding does not defeat the patterns."""
    folded = unicodedata.normalize("NFKC", text).translate(INVISIBLE)
    return re.sub(r"\s+", " ", folded).strip()


def _excerpt(text: str, match: re.Match[str], width: int = 90) -> str:
    start = max(0, match.start() - 12)
    end = min(len(text), match.end() + width)
    piece = text[start:end].strip()
    return f"…{piece}…" if start or end < len(text) else piece


def extract_facts(normalised: str) -> TextFacts:
    """Pull structured facts out of merchant copy.

    Every branch here produces a number or a token. No branch can produce a
    permission, which is what makes reading this text safe at all.
    """
    facts = TextFacts()

    window = RETURN_WINDOW.search(normalised)
    if window:
        facts.return_window_days = int(window.group("d1") or window.group("d2"))
    if FINAL_SALE.search(normalised):
        facts.return_explicitly_refused = True
        facts.return_window_days = None
    if NOT_STATED.search(normalised):
        facts.return_window_unstated = True
        facts.return_window_days = None

    facts.sizes = [m.group("size").lower() for m in SIZE.finditer(normalised)]
    facts.inches = [m.group("inch") for m in INCH.finditer(normalised)]
    facts.recurring_charge = bool(RECURRING_CHARGE.search(normalised))
    return facts


def detect_injection(normalised: str) -> list[InjectionFinding]:
    findings: list[InjectionFinding] = []
    for code, pattern in INJECTION_PATTERNS:
        match = pattern.search(normalised)
        if match:
            findings.append(InjectionFinding(code, _excerpt(normalised, match)))
    return findings


def sanitise(text: str) -> Sanitised:
    """Read one piece of merchant-supplied text."""
    normalised = normalise(text or "")
    return Sanitised(
        source=text or "",
        normalised=normalised,
        facts=extract_facts(normalised),
        injections=detect_injection(normalised),
    )
