"""Versioned prompt templates, kept separate from business logic.

Prompt registry maps task -> (name, version, template). Real providers render the
template; the deterministic mock uses the same registry for name/version metadata so
observability is identical across providers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    template: str


PROMPTS: dict[str, Prompt] = {
    "classify_signal": Prompt(
        name="classify_signal",
        version="1.0.0",
        template=(
            "You are SignalNest's signal classifier. Classify the public content into a "
            "signal_type and pain_point_dna, infer sentiment and a SPECIFIC audience label "
            "(never 'consumers'). Return strict JSON.\n\nContent:\n{content}\n"
            "Business keywords: {keywords}\n"
        ),
    ),
    "explain_opportunity": Prompt(
        name="explain_opportunity",
        version="1.0.0",
        template=(
            "You are SignalNest's explanation engine. Given the opportunity context, produce "
            "why_it_matters, who_cares, ai_inference, recommended_action, risk_note and "
            "suggested_angles. Separate observed evidence from inference. Do NOT invent "
            "unsupported product claims or attack competitors. Return strict JSON.\n\n"
            "Context:\n{context}\n"
        ),
    ),
    "check_claim_safety": Prompt(
        name="check_claim_safety",
        version="1.0.0",
        template=(
            "You are SignalNest's claim-safety reviewer. Assess risk_level and warnings and "
            "propose a brand-safe safe_alternative using only approved claims. Return strict "
            "JSON.\n\nText:\n{text}\nIndustry: {industry}\n"
        ),
    ),
    "summarize_website": Prompt(
        name="summarize_website",
        version="1.0.0",
        template="Summarize the website content for marketing context.\n\n{content}\n",
    ),
    "geo_reasoning": Prompt(
        name="geo_reasoning",
        version="1.0.0",
        template="Resolve the market from evidence. Return strict JSON.\n\n{evidence}\n",
    ),
}


def get_prompt(task: str) -> Prompt:
    if task not in PROMPTS:
        raise KeyError(f"No prompt registered for task '{task}'")
    return PROMPTS[task]
