"""NIM (OpenAI-compatible) client. The only file in this repo allowed to

import ``openai`` and the only file discovery calls into for a model
response. Nothing here is imported by ``grip/replay/engine.py`` -- design
rule 1 (no LLM in the replay path) is enforced by that absence, not by a
runtime check.

The NIM free tier answers with HTTP 429 once its per-model rate limit is
hit, and does so often enough that discovery has to treat it as routine
rather than exceptional -- hence the retry/backoff below rather than
surfacing it as a hard failure on the first hit.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

from openai import AsyncOpenAI, RateLimitError

from grip.config import Settings


class LLMError(RuntimeError):
    """The model call failed in a way retrying will not fix."""


class LLMClient:
    """Wraps one ``AsyncOpenAI`` client pointed at ``Settings.llm_base_url``.

    Any OpenAI-compatible endpoint (a local NIM container, vLLM, Ollama,
    OpenRouter) works by changing that one setting -- nothing here is
    NVIDIA-specific beyond the default base URL.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncOpenAI | None = None
        if settings.llm_configured:
            self._client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_s,
            )

    @property
    def configured(self) -> bool:
        """False with no API key set. Replay must never depend on this being

        true -- it has no model call on its path to guard, but discovery
        checks this before starting rather than failing several steps in.
        """
        return self._client is not None

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        """One structured-JSON completion. Discovery's only door to the model.

        Temperature is pinned to ``Settings.llm_temperature`` (0 by default)
        because discovery's action choice needs to be reproducible enough to
        debug, not creative. ``response_format={"type": "json_object"}``
        constrains the model to a JSON object rather than free text; the
        caller (grip/discovery/prompts.py) is responsible for describing the
        exact shape it wants inside the prompt and validating what comes
        back -- this method's contract is only "valid JSON," not "valid
        action."
        """
        if self._client is None:
            raise LLMError(
                "LLM is not configured (no API key set). This should never be "
                "reached from replay -- only discovery calls complete_json()."
            )

        attempt = 0
        delay = self._settings.llm_initial_backoff_s
        while True:
            try:
                response = await self._client.chat.completions.create(
                    model=self._settings.llm_model,
                    temperature=self._settings.llm_temperature,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
            except RateLimitError as exc:
                if attempt >= self._settings.llm_max_retries:
                    raise LLMError(
                        f"rate-limited after {attempt} retries: {exc}"
                    ) from exc
                # Full jitter: sleep a random amount up to the current
                # backoff ceiling, not the ceiling itself, so many runs
                # backing off in lockstep don't all retry on the same tick.
                sleep_for = random.uniform(0, delay)
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, self._settings.llm_max_backoff_s)
                attempt += 1
                continue

            content = response.choices[0].message.content or ""
            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:
                raise LLMError(f"model did not return valid JSON: {content!r}") from exc
