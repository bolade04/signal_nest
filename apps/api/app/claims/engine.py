"""Claim-safety / brand-safety engine (pure, framework-free).

Two responsibilities used by Phase 2 reasoning:
  1. ``check_claim_safety`` — flag risky claims in generated/derived text.
  2. ``translate_competitor_weakness`` — turn a competitor complaint into a customer
     pain point and *safe* positive positioning (never an unsupported product claim).

Core rule: use competitor complaints as market insight, not as proof the user's
product is better.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.enums import ClaimRisk

# Industries requiring stricter controls.
STRICT_INDUSTRIES = {
    "alcohol", "cosmetics", "healthcare", "finance", "supplements", "legal",
    "legal services", "insurance", "children", "cybersecurity", "real estate",
    "education", "food and beverage",
}

_FLAGS: list[tuple[str, str, ClaimRisk]] = [
    (r"\b(cure|cures|treats?|prevents?|heals?)\b", "health_claim", ClaimRisk.HIGH),
    (r"\b(guarantee[ds]?|guaranteed)\b", "guarantee", ClaimRisk.HIGH),
    (r"\b(no\s+side\s+effects|risk[- ]free|100%\s+safe)\b", "safety_claim", ClaimRisk.HIGH),
    (r"\b(FDA|clinically proven|doctor recommended)\b", "regulated_claim", ClaimRisk.HIGH),
    (r"\b(guaranteed returns?|double your money|get rich)\b", "financial_claim", ClaimRisk.BLOCKED),
    (r"\bbest\b", "superlative_best", ClaimRisk.MEDIUM),
    (r"\b(number\s*one|#1|no\.?\s*1)\b", "superlative_number_one", ClaimRisk.MEDIUM),
    (r"\bwon'?t\s+(cause|give you)\b", "negative_absolute_claim", ClaimRisk.HIGH),
    (r"\b(better than|beats|outperforms)\s+\w+", "comparative_claim", ClaimRisk.MEDIUM),
    (
        r"\b(they|competitor|brand x)\b.*\b(ignore|scam|lie|fail)\b",
        "competitor_attack",
        ClaimRisk.HIGH,
    ),
]

_RISK_ORDER = {ClaimRisk.LOW: 0, ClaimRisk.MEDIUM: 1, ClaimRisk.HIGH: 2, ClaimRisk.BLOCKED: 3}


@dataclass
class ClaimFinding:
    category: str
    risk: ClaimRisk
    excerpt: str


@dataclass
class ClaimSafetyResult:
    risk_level: ClaimRisk
    findings: list[ClaimFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_terms: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return self.risk_level == ClaimRisk.BLOCKED


def check_claim_safety(
    text: str,
    industry: str | None = None,
    blocked_claims: list[str] | None = None,
) -> ClaimSafetyResult:
    findings: list[ClaimFinding] = []
    warnings: list[str] = []
    blocked_hit: list[str] = []
    highest = ClaimRisk.LOW

    for pattern, category, risk in _FLAGS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            findings.append(ClaimFinding(category=category, risk=risk, excerpt=m.group(0)))
            if _RISK_ORDER[risk] > _RISK_ORDER[highest]:
                highest = risk
            warnings.append(f"Detected {category.replace('_', ' ')}: '{m.group(0)}'")

    for claim in blocked_claims or []:
        if claim and claim.lower() in text.lower():
            blocked_hit.append(claim)
            highest = ClaimRisk.BLOCKED
            warnings.append(f"Contains a blocked claim: '{claim}'")

    # Strict industries escalate medium findings to high.
    if industry and industry.lower() in STRICT_INDUSTRIES and findings:
        if highest == ClaimRisk.MEDIUM:
            highest = ClaimRisk.HIGH
        warnings.append(f"'{industry}' is a strictly regulated category; extra review required.")

    return ClaimSafetyResult(
        risk_level=highest,
        findings=findings,
        warnings=warnings,
        blocked_terms=blocked_hit,
    )


# Competitor pain point -> safe positioning translation table.
_SAFE_POSITIONING = {
    "poor_customer_service": "Support that actually feels responsive.",
    "speed_complaint": "Built for a faster, smoother experience.",
    "hidden_fees": "Clear, upfront pricing with no surprises.",
    "lack_of_transparency": "Transparency you can verify.",
    "quality_concern": "Consistent quality you can rely on.",
    "trust_issue": "Earned trust, backed by real proof.",
    "bad_user_experience": "An experience designed to feel effortless.",
    "price_frustration": "Fair pricing that respects your budget.",
    "product_confusion": "Clear guidance so you always know what you're getting.",
    "safety_concern": "Consider brand-safe, educational messaging about your standards.",
}


def translate_competitor_weakness(pain_point_dna: str | None, category: str | None = None) -> str:
    """Return safe positive positioning (never an attack or unsupported claim)."""
    if pain_point_dna and pain_point_dna in _SAFE_POSITIONING:
        return _SAFE_POSITIONING[pain_point_dna]
    if category in STRICT_INDUSTRIES:
        return (
            "Consumers are discussing product quality and experience in this category. "
            "Consider brand-safe, educational messaging using only approved claims."
        )
    return "Focus on your proven strengths with brand-safe, approved messaging."
