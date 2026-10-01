"""The Surface abstraction.

This is the seam the brief asks about: *how we perceive and act on a surface*
lives behind this interface, and *the recorded flow* (``schemas.py``) lives in
front of it. Neither knows about the other's internals.

Perception is modelled as a list of accessibility nodes rather than a DOM, a
screenshot, or a coordinate space. That choice is what makes the interface
implementable for three quite different surfaces:

* a modern web app — roles and names come from ARIA and semantic HTML;
* a legacy frameset web app — roles and names are computed from tag types and
  physical label adjacency, which is all these apps offer;
* a native desktop app — the OS already publishes exactly this tree (macOS AX
  API, Windows UI Automation), so the adapter is thinner than the web one.

Only the web adapter is implemented here. The interface exists in this shape
so that the desktop one would not require touching the artifact schema, the
replay engine, or the agent loop.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from keystone.schemas import AXLocator

# Roles a human operator can actually act on. Used to keep the observation
# small enough to fit in a rate-limited model's context.
INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "listbox",
    "checkbox",
    "radio",
    "menuitem",
    "tab",
    "spinbutton",
    "slider",
    "switch",
    "option",
}

# Roles that carry information the agent needs to read.
CONTENT_ROLES = {
    "heading",
    "cell",
    "columnheader",
    "rowheader",
    "alert",
    "status",
    "dialog",
    "alertdialog",
    "paragraph",
    "text",
    "StaticText",
    "label",
    "row",
    "table",
    "list",
    "listitem",
    "region",
    "form",
}


@dataclass
class AXNode:
    """One control or piece of content, as the accessibility layer sees it."""

    ref: str
    """Ephemeral handle valid only for the current observation.

    Deliberately *not* persisted into artifacts. Refs are how the agent points
    at something it can see right now; ``AXLocator`` is how a recorded step
    points at something months later. Conflating the two is how automation
    ends up brittle.
    """

    role: str
    name: str | None = None
    value: str | None = None
    description: str | None = None

    options: list[str] = field(default_factory=list)
    """The exact strings a combobox/listbox will accept for `select`.

    A <select>'s accessible name never includes its options' text (that's
    content, not a label), and its current value is just the one currently
    chosen -- neither tells the model what else it could pick. Legacy forms
    like this one label options "S07 - VACATION CLUB" (code + label), which a
    model guessing from the goal text alone ("VACATION CLUB") will not
    produce. Listing the real strings here is what lets `select` target one
    of them exactly instead of guessing blind.
    """

    frame_path: list[str] = field(default_factory=list)
    ancestor_roles: list[str] = field(default_factory=list)
    ancestor_name: str | None = None

    disabled: bool = False
    focused: bool = False
    required: bool = False
    invalid: bool = False
    checked: bool | None = None

    # Present only to build fallbacks; never the primary targeting strategy.
    css_hint: str | None = None
    ordinal: int = 0

    def normalized_name(self) -> str:
        return " ".join((self.name or "").split()).strip().casefold()

    def render(self) -> str:
        """Compact single-line form for the model's context window.

        Terse on purpose: the free NIM tier is rate-limited, and a 200-node
        observation rendered verbosely is the difference between a run that
        completes and one that spends its budget on whitespace.
        """
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value is not None and self.value != "":
            bits.append(f"={self.value!r}")
        flags = [
            f for f, on in (
                ("disabled", self.disabled),
                ("required", self.required),
                ("invalid", self.invalid),
                ("focused", self.focused),
            ) if on
        ]
        if self.checked is not None:
            flags.append("checked" if self.checked else "unchecked")
        if flags:
            bits.append(f"({','.join(flags)})")
        if self.frame_path:
            bits.append(f"<{'/'.join(self.frame_path)}>")
        if self.options:
            bits.append("options=" + "|".join(self.options))
        return " ".join(bits)

    def to_locator(self, rationale: str = "") -> AXLocator:
        """Promote a live node into a persistable locator.

        The rationale is the interesting part. A recorded step is reviewed by
        a human who was not present when it was recorded, so the schema
        requires a written justification for why this targeting should hold.
        """
        if not rationale:
            rationale = self._default_rationale()
        loc = AXLocator(
            role=self.role,
            name=self.name,
            name_match="normalized" if self.name else "exact",
            frame_path=list(self.frame_path),
            ancestor_roles=list(self.ancestor_roles[-3:]),
            ancestor_name=self.ancestor_name,
            ordinal=self.ordinal,
            rationale=rationale,
        )
        if self.css_hint:
            loc.fallbacks.append(
                _css_fallback(self.css_hint)
            )
        return loc

    def _default_rationale(self) -> str:
        if self.name and self.ordinal == 0:
            base = (
                f"Targeted by accessible name {self.name!r} on role {self.role!r}, which is "
                "computed from the rendered label rather than markup structure and therefore "
                "survives restyling and per-tenant branding."
            )
        elif self.name:
            base = (
                f"Accessible name {self.name!r} is not unique on this surface; disambiguated by "
                f"position ({self.ordinal}) within the same container. Fragile if the app reorders "
                "these controls."
            )
        else:
            base = (
                f"Control exposes no accessible name, so it is targeted by role {self.role!r} and "
                "container position. This is the weakest targeting in the flow."
            )
        if self.frame_path:
            base += f" Scoped to frame {'/'.join(self.frame_path)}."
        if self.ancestor_name:
            base += f" Disambiguated by containing region {self.ancestor_name!r}."
        return base


def _css_fallback(expression: str):
    from keystone.schemas import SelectorFallback

    return SelectorFallback(
        strategy="css",
        expression=expression,
        weaker_because=(
            "Structural selector tied to markup rather than meaning. Legacy enterprise apps "
            "regenerate class names and table structure between releases, so this is a "
            "last resort used only when accessibility targeting misses."
        ),
    )


@dataclass
class Observation:
    """A complete view of surface state at one moment."""

    url: str
    title: str
    nodes: list[AXNode]
    frame_paths: list[list[str]] = field(default_factory=list)
    text_digest: str = ""
    """Flattened visible text, used for text_present/absent conditions."""

    modal_present: bool = False
    step_index: int = 0

    def by_ref(self, ref: str) -> AXNode | None:
        return next((n for n in self.nodes if n.ref == ref), None)

    def interactive(self) -> list[AXNode]:
        return [n for n in self.nodes if n.role in INTERACTIVE_ROLES]

    def render(self, max_nodes: int = 120) -> str:
        """Render for the model. Interactive controls first, then content."""
        inter = self.interactive()
        content = [n for n in self.nodes if n.role not in INTERACTIVE_ROLES and n.name]
        budget = max(0, max_nodes - len(inter))
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.modal_present:
            lines.append("NOTE: a modal/dialog is present and is probably blocking the page.")
        lines.append("")
        lines.append("INTERACTIVE CONTROLS:")
        lines.extend("  " + n.render() for n in inter[:max_nodes])
        if content:
            lines.append("")
            lines.append("VISIBLE CONTENT:")
            lines.extend("  " + n.render() for n in content[:budget])
        return "\n".join(lines)


@dataclass
class ActionRequest:
    """An action to perform, addressed either by live ref or by stored locator.

    Discovery addresses by ``ref`` (it can see the screen). Replay addresses
    by ``locator`` (it cannot, and must not need to). Same code path, same
    action semantics, different addressing — which is exactly the property
    that makes a replayed flow equivalent to the discovered one.
    """

    action: Literal["navigate", "click", "type", "select", "wait", "read"]
    ref: str | None = None
    locator: AXLocator | None = None
    value: str | None = None
    url: str | None = None
    timeout_s: float | None = None
    settle_ms: int = 0
    """Extra quiet period the surface should honour after the action

    succeeds, carried straight from ``Step.settle_ms``. This is a fixed
    delay, not a wait-for-condition — the durable version (retry the
    checkpoint within a window) belongs to the replay engine, not the
    surface; this only keeps the schema field from being silently ignored.
    """
    sensitive: bool = False
    """If true the value must never appear in a log, trace, or evidence file."""

    def describe(self) -> str:
        target = self.ref or (self.locator.describe() if self.locator else self.url or "")
        val = "***" if self.sensitive else (self.value or "")
        return f"{self.action}({target}{', ' + val if val else ''})"


@dataclass
class ActionOutcome:
    """Result of attempting one action against the surface."""

    ok: bool
    detail: str = ""
    read_value: str | None = None
    resolved: str | None = None
    """How the target was actually found — primary locator or which fallback."""
    duration_ms: int = 0
    error_kind: (
        Literal["not_found", "ambiguous", "disabled", "timeout", "error", "escalation_required"] | None
    ) = None
    """"escalation_required" is distinct from "disabled" (a plain policy

    block) on purpose: a caller needs to tell "the guardrail said no" from
    "the guardrail said ask a human" apart to route the latter to an
    EscalationController instead of just failing.
    """


class Surface(ABC):
    """What every surface adapter must provide.

    Six operations. Everything above this line — the agent loop, the replay
    engine, checkpoints, escalation — is written against these and nothing
    else, which is what keeps the desktop adapter a genuinely additive change.
    """

    kind: str = "abstract"

    @abstractmethod
    async def observe(self) -> Observation:
        """Return the current accessible state."""

    @abstractmethod
    async def act(self, request: ActionRequest) -> ActionOutcome:
        """Perform one action."""

    @abstractmethod
    async def resolve(self, locator: AXLocator) -> tuple[str | None, str]:
        """Resolve a stored locator to a live ref.

        Returns ``(ref, how)`` where ``how`` names the strategy that matched,
        or ``(None, reason)`` if nothing did. Replay reports ``how`` in its
        trace so a reviewer can see when a flow is quietly surviving on
        fallbacks and is due for re-recording.
        """

    @abstractmethod
    async def screenshot(self, path: str) -> bool:
        """Capture a visual record. Evidence only, never perception."""

    @abstractmethod
    async def current_url(self) -> str:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    # -- optional: richer evidence across a human handoff -----------------
    #
    # Deliberately concrete, not abstract: tracing is a Playwright-specific
    # capability with no obvious equivalent on, say, a future desktop
    # adapter (macOS AX API / Windows UI Automation have no comparable
    # browser-trace concept). The six methods above are the real contract
    # every surface must honour; these two are a best-effort extra every
    # surface gets to opt into, defaulting to a clean no-op rather than
    # forcing every future implementation to stub them out.

    async def start_trace(self) -> bool:
        """Best-effort: start capturing a detailed action trace across an

        escalation handoff. Returns whether tracing actually started.
        """
        return False

    async def stop_trace(self, path: str) -> bool:
        """Best-effort: stop capturing and save the trace to ``path``.

        Returns whether a trace was actually saved.
        """
        return False


def normalize(text: str | None) -> str:
    return " ".join((text or "").split()).strip().casefold()


def name_matches(candidate: str | None, wanted: str | None, mode: str) -> bool:
    """Compare an observed accessible name against a stored one."""
    if wanted is None:
        return True
    if candidate is None:
        return False
    if mode == "exact":
        return candidate == wanted
    if mode == "regex":
        try:
            return re.search(wanted, candidate, re.I) is not None
        except re.error:
            return False
    c, w = normalize(candidate), normalize(wanted)
    if mode == "contains":
        return w in c
    # "normalized" — whitespace- and case-insensitive equality, plus tolerance
    # for the trailing punctuation legacy apps put on field labels ("Member #:").
    return c.rstrip(":*# ") == w.rstrip(":*# ")
