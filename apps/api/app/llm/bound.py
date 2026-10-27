"""The worst-case cost bound of one metered provider request (D-LLM-1 option B).

:func:`worst_case_cost` is a pure function of the exact request body, the model and the
configured price schedule. It is what the budget ledger reserves before a provider call
(:mod:`app.llm.metering`, :mod:`app.llm.budget`). It replaces the earlier
"~3 characters per token" estimate, which was an estimate, not a bound.

Facts it relies on (each stated with its source; none is verified against a live
provider, and the bound is only as true as they are):

* **F1 - bytes bound tokens.** A byte-pair (byte-level BPE) tokenizer emits at most one
  token per UTF-8 byte of its input, because every byte is in its base alphabet and a
  merge only ever joins tokens. Source: the byte-level BPE construction (Sennrich et al.
  2016; GPT-2 byte-level variant, Radford et al. 2019); OpenAI's tokenizers are public
  (``tiktoken``, ``cl100k_base``/``o200k_base``). **Anthropic's tokenizer is not
  public**: for ``claude-*`` models F1 is the operator-ruled conservative premise
  (coordinator ruling 2026-10-27T19:27Z: "tokens <= UTF-8 byte count of every text
  component, plus the per-message/request overhead constant"), not a documented fact.
  A model whose family is not in :data:`SUPPORTED_MODEL_FAMILIES` is unsupported and
  fails closed.
* **F2 - overheads are constants.** Neither provider documents a per-message or
  per-request token overhead that this module can cite for Anthropic. OpenAI's cookbook
  ("How to count tokens with tiktoken") gives 3 tokens per message and 3 to prime the
  reply. :data:`REQUEST_OVERHEAD_TOKENS` and :data:`MESSAGE_OVERHEAD_TOKENS` are
  **stated conservative constants** (about 40x and 5x the OpenAI figures), labelled as
  such; they are not provider figures.
* **F3 - what the request can bill.** The providers' adapters (``providers_real``)
  send only the keys in :data:`ANTHROPIC_KEYS` / :data:`OPENAI_KEYS`, with text-only
  message content, no tool definitions, no ``cache_control``, no extended thinking, no
  server tools, no ``n`` > 1. The bound enforces that: any other key, or a message
  content that is not a string, is **unbounded** (fail closed), so cache write/read
  pricing (which needs a ``cache_control`` marker), tool-use overhead, and tool or
  thinking tokens cannot be billed by a request this function accepts. Output is bounded
  by ``max_tokens`` (Anthropic) / ``max_completion_tokens`` (OpenAI), which also cap
  reasoning tokens where a model has them.

Price components covered: **input tokens** and **output tokens** from the schedule's
``[input, output]`` USD per million tokens. Excluded by F3, with the request shape
proving it: cache writes, cache reads, tool and server-tool charges, batch/priority
tier differences, thinking tokens beyond ``max_tokens``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

MICRO = Decimal("0.000001")

#: Model-id prefixes whose tokenizer premise F1 is accepted (see the module docstring).
SUPPORTED_MODEL_FAMILIES = ("claude-", "gpt-")
#: Stated conservative constant per request (F2): not a provider figure.
REQUEST_OVERHEAD_TOKENS = 128
#: Stated conservative constant per message, the ``system`` string counted as one (F2).
MESSAGE_OVERHEAD_TOKENS = 16
#: A request asking for more output tokens than this is not bounded by this module.
MAX_OUTPUT_TOKENS = 1_000_000

ANTHROPIC_KEYS = frozenset({"model", "max_tokens", "system", "messages", "temperature", "metadata"})
OPENAI_KEYS = frozenset(
    {
        "model",
        "temperature",
        "max_completion_tokens",
        "response_format",
        "messages",
        "user",
    }
)
_MESSAGE_ROLES = frozenset({"user", "assistant", "system"})


@dataclass(frozen=True)
class Unbounded:
    """The request's cost cannot be bounded: no attempt may be made (never a 0 reserve).

    ``reason`` is one of ``model_not_priced``, ``price_invalid``,
    ``model_family_unsupported``, ``no_wire_payload``, ``payload_not_json``,
    ``payload_shape_unsupported``, ``payload_feature_not_priced``, ``model_mismatch``,
    ``output_limit_missing``, ``output_limit_invalid``.
    """

    reason: str
    detail: str = ""


def _decimal_price(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    price = Decimal(str(value))
    return price if price >= 0 and price.is_finite() else None


def _raw_bytes(payload: bytes | bytearray | str | Mapping[str, Any]) -> bytes | Unbounded:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        try:
            return payload.encode("utf-8")
        except UnicodeEncodeError:
            return Unbounded("payload_not_json", "not encodable as UTF-8")
    if isinstance(payload, Mapping):
        try:
            return encode_body(payload)
        except (TypeError, ValueError):
            return Unbounded("payload_not_json", "not encodable as JSON")
    return Unbounded("payload_not_json", f"unsupported payload type {type(payload).__name__}")


def encode_body(body: Mapping[str, Any]) -> bytes:
    """The one serialisation of a request body: compact JSON, UTF-8, no ``\\u`` escapes.

    The adapters send exactly these bytes, and :func:`worst_case_cost` counts exactly
    these bytes when it is given the mapping.
    """
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _message_count(body: dict[str, Any], keys: frozenset[str]) -> int | Unbounded:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return Unbounded("payload_shape_unsupported", "messages")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            return Unbounded("payload_feature_not_priced", "message keys")
        if message["role"] not in _MESSAGE_ROLES:
            return Unbounded("payload_shape_unsupported", "message role")
        if not isinstance(message["content"], str):
            # A list of content blocks is how images, tool results and cache_control
            # markers are sent: none of them is priced here.
            return Unbounded("payload_feature_not_priced", "non-text message content")
    count = len(messages)
    if "system" in keys and "system" in body:
        if not isinstance(body["system"], str):
            return Unbounded("payload_feature_not_priced", "non-text system")
        count += 1
    return count


def worst_case_cost(
    model: str,
    request_payload: bytes | bytearray | str | Mapping[str, Any],
    price_schedule: Mapping[str, Sequence[float]],
) -> Decimal | Unbounded:
    """An upper bound, in USD rounded up to 1e-6, on what one request can cost.

    Derivation. Let ``B`` be the number of UTF-8 bytes of the complete request body
    exactly as the transport sends it (``request_payload``: the bytes, or the mapping
    serialised by :func:`encode_body`; it covers the system prompt, every message, the
    tenant reference, the model id and every other field, JSON syntax included). Let
    ``M`` be the number of messages (the ``system`` string counts as one) and ``O`` the
    request's own output limit (``max_tokens`` / ``max_completion_tokens``).

    * input tokens ``<= B + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * M``
      (F1: at most one token per byte of any text component, hence of the whole body;
      F2: the overhead constants);
    * output tokens ``<= O`` (the provider stops at the limit; F3 shows the request
      cannot ask for anything billed beyond it);
    * cost ``= (input * price_in + output * price_out) / 1e6`` in exact decimal
      arithmetic, then **rounded up** (ROUND_CEILING) to the ledger's 1e-6 precision and
      never below 1e-6, so a non-empty request is never reserved as free.

    Monotone: more bytes, more messages, a larger output limit or higher prices never
    lower the result. The ceiling-rounded value is always at or above the exact cost.

    Fails closed (returns :class:`Unbounded`, so the caller makes no attempt) for: a
    model missing from ``price_schedule`` or with an invalid price; a model family whose
    tokenizer premise F1 is not accepted; a body that is not one JSON object; any key,
    message shape or content type outside what the adapters send (F3: tools, cache
    controls, content blocks, unknown parameters); a ``model`` field that differs from
    the priced ``model``; a missing or non-positive output limit.

    Conservative basis: F1 for Claude models is the operator's ruling, not a provider
    document, and F2's constants are not provider figures (module docstring). The result
    bounds what the application's price schedule says the request can cost; it is not
    the provider's invoice.
    """
    prices = price_schedule.get(model) if isinstance(model, str) else None
    if prices is None:
        return Unbounded("model_not_priced", "model")
    if not isinstance(prices, Sequence) or len(prices) != 2:
        return Unbounded("price_invalid")
    price_in, price_out = _decimal_price(prices[0]), _decimal_price(prices[1])
    if price_in is None or price_out is None:
        return Unbounded("price_invalid")
    if not model.startswith(SUPPORTED_MODEL_FAMILIES):
        return Unbounded("model_family_unsupported", "model family")

    raw = _raw_bytes(request_payload)
    if isinstance(raw, Unbounded):
        return raw
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return Unbounded("payload_not_json")
    if not isinstance(body, dict):
        return Unbounded("payload_not_json", "not an object")

    has_a, has_o = "max_tokens" in body, "max_completion_tokens" in body
    if has_a == has_o:
        return Unbounded("output_limit_missing", "exactly one output limit is required")
    keys = ANTHROPIC_KEYS if has_a else OPENAI_KEYS
    extra = sorted(set(body) - keys)
    if extra:
        return Unbounded("payload_feature_not_priced", ",".join(extra))
    if body.get("model") != model:
        return Unbounded("model_mismatch")
    limit = body["max_tokens" if has_a else "max_completion_tokens"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        return Unbounded("output_limit_invalid", "not an integer")
    if not 1 <= limit <= MAX_OUTPUT_TOKENS:
        return Unbounded("output_limit_invalid", "out of range")
    if has_a and "metadata" in body:
        meta = body["metadata"]
        if not isinstance(meta, dict) or set(meta) - {"user_id"}:
            return Unbounded("payload_feature_not_priced", "metadata")
    if has_o and "response_format" in body and body["response_format"] != {"type": "json_object"}:
        return Unbounded("payload_feature_not_priced", "response_format")
    messages = _message_count(body, keys)
    if isinstance(messages, Unbounded):
        return messages

    tokens_in = len(raw) + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS * messages
    # scaleb is an exact shift (a division could round to the context precision).
    exact = (Decimal(tokens_in) * price_in + Decimal(limit) * price_out).scaleb(-6)
    return max(exact.quantize(MICRO, rounding=ROUND_CEILING), MICRO)
