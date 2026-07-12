"""Deterministic offline mock LLM provider.

* Same inputs -> same output (seeded by ``LLM_MOCK_SEED`` + an input hash).
* Structured, schema-valid outputs (not arbitrary placeholder text).
* Can simulate failure scenarios for tests via an ``inputs["_simulate"]`` marker:
  ``low_confidence | malformed | timeout | rate_limit | refusal | provider_error``.
* Outputs are marked ``is_simulated=True`` so the UI/metadata can flag demo results.
"""

from __future__ import annotations

import hashlib
import random
import re

from app.llm.base import (
    LLMMalformedError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
)

_PAIN_KEYWORDS = {
    "trust_issue": ["scam", "trust", "fake", "legit"],
    "price_frustration": ["expensive", "overpriced", "price", "cost", "pricey"],
    "speed_complaint": ["slow", "late", "delay", "wait", "took forever"],
    "poor_customer_service": ["support", "response", "ignored", "rude", "service"],
    "quality_concern": ["quality", "broke", "cheap", "flimsy", "defective"],
    "hidden_fees": ["hidden fee", "surprise charge", "extra charge"],
    "safety_concern": ["irritation", "breakout", "reaction", "unsafe", "headache"],
    "product_confusion": ["confusing", "how do i", "don't understand", "unclear"],
}

_SIGNAL_TYPE_KEYWORDS = {
    "buying_intent": ["looking for", "recommend", "where can i buy", "need a", "best "],
    "complaint": ["terrible", "worst", "hate", "disappointed", "never again"],
    "question": ["how", "what", "why", "?", "anyone know"],
    "competitor_dissatisfaction": ["switched from", "instead of", "unlike"],
    "trend_discussion": ["trending", "everyone", "viral", "new trend"],
}


def _hash_inputs(seed: str, inputs: dict) -> str:
    payload = seed + "|" + repr(sorted(inputs.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def _pick(rng: random.Random, options: list[str]) -> str:
    return options[rng.randrange(len(options))]


def _detect(text: str, table: dict[str, list[str]], default: str) -> str:
    low = text.lower()
    for key, kws in table.items():
        if any(kw in low for kw in kws):
            return key
    return default


class MockLLMProvider:
    name = "mock"

    def __init__(self, model: str = "mock-1", seed: str = "signalnest-dev"):
        self.model = model
        self.seed = seed

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        inputs = request.inputs
        simulate = inputs.get("_simulate")
        if simulate == "timeout":
            raise LLMTimeoutError("simulated timeout")
        if simulate == "rate_limit":
            raise LLMRateLimitError("simulated rate limit")
        if simulate == "refusal":
            raise LLMRefusalError("simulated refusal")
        if simulate == "provider_error":
            raise LLMProviderError("simulated provider error")
        if simulate == "malformed":
            raise LLMMalformedError("simulated malformed output")

        input_hash = _hash_inputs(request.seed or self.seed, inputs)
        rng = random.Random(int(input_hash[:12], 16))
        output = self._build_output(request.task, inputs, rng)
        status = "low_confidence" if simulate == "low_confidence" else "ok"

        text_len = len(str(inputs))
        usage = LLMUsage(
            input_tokens=max(1, text_len // 4),
            output_tokens=max(1, len(str(output)) // 4),
            estimated_cost_usd=0.0,
        )
        return LLMResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            output=output,
            usage=usage,
            latency_ms=1.0,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            input_hash=input_hash,
            status=status,
            is_simulated=True,
        )

    def _build_output(self, task: str, inputs: dict, rng: random.Random) -> dict:
        if task == "classify_signal":
            return self._classify(inputs, rng)
        if task == "explain_opportunity":
            return self._explain(inputs, rng)
        if task == "check_claim_safety":
            return self._claim(inputs)
        if task == "summarize_website":
            content = str(inputs.get("content", ""))[:280]
            return {"summary": f"Marketing summary: {content.strip()[:180]}"}
        if task == "geo_reasoning":
            market = inputs.get("target_market")
            return {
                "resolved_market": market,
                "confidence": 0.8 if market else 0.0,
                "evidence": inputs.get("evidence", []),
            }
        return {}

    def _classify(self, inputs: dict, rng: random.Random) -> dict:
        content = str(inputs.get("content", ""))
        signal_type = _detect(content, _SIGNAL_TYPE_KEYWORDS, "pain_point")
        pain = _detect(content, _PAIN_KEYWORDS, "quality_concern")
        sentiment = "negative" if signal_type in {"complaint", "pain_point"} else "neutral"
        audiences = inputs.get("audiences") or ["local customers comparing options"]
        return {
            "signal_type": signal_type,
            "pain_point_dna": pain,
            "sentiment": sentiment,
            "audience_fit_label": _pick(rng, list(audiences)),
            "is_actionable": signal_type != "trend_discussion",
            "rationale": f"Language indicates a {signal_type.replace('_', ' ')}.",
        }

    def _explain(self, inputs: dict, rng: random.Random) -> dict:
        market = inputs.get("resolved_market") or "your market"
        audience = inputs.get("audience_fit") or "your target customers"
        pain = str(inputs.get("pain_point_dna", "a recurring concern")).replace("_", " ")
        angle = str(inputs.get("safe_positioning") or "Lead with proven, brand-safe strengths.")
        return {
            "why_it_matters": (
                f"Multiple public sources in {market} surface {pain}, matching your positioning."
            ),
            "who_cares": audience,
            "ai_inference": (
                f"Inference: demand is forming around {pain}; not yet saturated by competitors."
            ),
            "recommended_action": (
                f"Create brand-safe content addressing {pain} for {audience}."
            ),
            "risk_note": "Use only approved claims; avoid competitor comparisons.",
            "suggested_angles": [
                angle,
                f"Educational post about {pain}",
                f"FAQ addressing {pain} for {audience}",
            ],
        }

    def _claim(self, inputs: dict) -> dict:
        text = str(inputs.get("text", ""))
        risky = bool(re.search(r"\b(cure|guarantee|best|#1|won'?t cause)\b", text, re.I))
        return {
            "risk_level": "high" if risky else "low",
            "warnings": (["Contains an unsupported or absolute claim."] if risky else []),
            "safe_alternative": (
                "Consider educational, approved-claim messaging instead."
                if risky
                else ""
            ),
        }
