"""Evaluating a ``Condition`` against a live surface.

Extracted out of ``grip/replay/engine.py`` when discovery needed the exact
same capability: "never trust the model's claim that it's done -- assert
the compiled artifact's success condition against the live surface
yourself" (design rule 6) is mechanically the same operation replay already
does for checkpoints and business outcomes. One implementation, two callers,
so they can never quietly diverge on what "ax_present" means.
"""

from __future__ import annotations

import re

from grip.schemas import Condition
from grip.surface.base import ActionRequest, Surface, normalize

_PARAM_TOKEN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def substitute(text: str, params: dict[str, str]) -> str:
    """Replaces ``${param_name}`` tokens with bound param values.

    Lets a checkpoint recorded once (e.g. "the field holds ${member_id}")
    stay correct no matter which member number a given run was called with.
    """
    return _PARAM_TOKEN.sub(lambda m: params.get(m.group(1), m.group(0)), text)


async def evaluate(surface: Surface, condition: Condition, params: dict[str, str]) -> tuple[bool, str | None]:
    """Returns (holds, observed) -- ``observed`` is a human-readable

    description of what was actually found, used for failure reporting.
    """
    pattern = substitute(condition.pattern, params) if condition.pattern else None

    if condition.kind == "ax_present":
        ref, how = await surface.resolve(condition.locator)
        return ref is not None, (None if ref is not None else f"not found ({how})")

    if condition.kind == "ax_absent":
        ref, how = await surface.resolve(condition.locator)
        return ref is None, (f"found unexpectedly: {condition.locator.describe()}" if ref is not None else None)

    if condition.kind == "ax_value_matches":
        ref, how = await surface.resolve(condition.locator)
        if ref is None:
            return False, f"target not found ({how})"
        outcome = await surface.act(ActionRequest(action="read", ref=ref, locator=condition.locator))
        if not outcome.ok:
            return False, f"read failed: {outcome.detail}"
        value = outcome.read_value or ""
        return bool(re.search(pattern, value)), value

    if condition.kind == "url_matches":
        url = await surface.current_url()
        return bool(re.search(pattern, url)), url

    if condition.kind in ("text_present", "text_absent"):
        observation = await surface.observe()
        present = normalize(pattern) in observation.text_digest
        holds = present if condition.kind == "text_present" else not present
        return holds, observation.text_digest[:300]

    return False, f"unrecognised condition kind {condition.kind!r}"
