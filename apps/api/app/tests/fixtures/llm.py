"""Seeded mock LLM providers for tests (Project Phase 5A-W4; W21 lane ADV5AB).

Offline and free: every double here is a :class:`~app.llm.mock.MockLLMProvider`, so its
output is the mock's deterministic output for the seed and the inputs. The recording double
keeps each :class:`~app.llm.base.LLMRequest` exactly as the service sent it (after the
service's boundary sanitization), so a test can assert what reached the provider: the task,
the inputs, and the opaque tenant reference.
"""

from __future__ import annotations

import threading

from app.llm.base import LLMRequest, LLMResponse
from app.llm.mock import MockLLMProvider


class RecordingMock(MockLLMProvider):
    """The seeded mock, recording every request it is sent (thread-safe)."""

    def __init__(self, model: str = "mock-1", seed: str = "signalnest-tests-5a"):
        super().__init__(model=model, seed=seed)
        self.requests: list[LLMRequest] = []
        self._lock = threading.Lock()

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        with self._lock:
            self.requests.append(request.model_copy(deep=True))
        return super().generate(request, prompt_name, prompt_version)


__all__ = ["RecordingMock"]
