"""Playwright adapter: the concrete ``Surface`` for browser-driven targets.

Perception design decision
---------------------------
The obvious two options for computing role + accessible name are (a) Chrome
DevTools Protocol's ``Accessibility.getFullAXTree`` and (b) an injected
in-page script that recomputes the accname algorithm itself. This adapter
uses only (b), for a reason specific to this problem rather than a general
preference:

The mock (and the real legacy consoles it stands in for) labels every form
field by the table cell physically to its left, which is a *visual*
convention this application invented, not part of the ARIA accessible-name
algorithm. CDP's AX tree will never produce that name, because no browser
computes it -- it isn't spec'd anywhere. Even with CDP available, a second
pass over the DOM would still be required for every node CDP returned empty,
and that second pass would need to re-implement most of the standard
priority chain anyway to know *when* to fall back to it. Rather than run two
name computations that can disagree about the same node, there is exactly
one: a per-element script that implements the full priority chain --
aria-labelledby, aria-label, ``label[for]``, a wrapping ``<label>``, the
``value`` attribute (submit/button inputs only), ``alt``, ``title``,
``placeholder``, text content, and only then the adjacent-left-cell fallback.
One source of truth per node, in the one place that already has to know the
apps-specific convention.

The adjacent-cell heuristic, and what it gets wrong
----------------------------------------------------
Table-based legacy layouts have no ``<label for>`` anywhere; the caption is
just the ``<td>`` to the left in the same row. Any control that reaches the
end of the standard priority chain with no name of its own (true of every
plain ``<input>`` in this app, since inputs carry no text-content) is named
from that left sibling cell.

The same convention is applied to *static* label:value pairs (e.g. "Member
#:" / "12345") one row of the summary panel, because to this application a
read-only field and an editable one are the same idiom -- a caption cell
followed by a content cell. A ``<td>`` is treated as the "value" half of such
a pair only when: its row contains an even number of plain cells (no nested
interactive control anywhere in the row), and it sits at an odd position in
that row. The value half's own text becomes both its ``AXNode.value`` and,
inherited, its *name*. The label half is not emitted as a node at all: its
text is already carried as the value cell's name, so keeping it too would
put two nodes at the same (role, name) key, with the label -- the one with
no value -- landing at ordinal 0 by DOM order. That is precisely the wrong
half for a locator built with the default ordinal to land on, so it is
excluded at the source instead of guarded against downstream.

What this gets wrong: it cannot distinguish a two-column label:value summary
row from a same-shaped data grid row. The "Share & Loan Accounts" table in
this app has four plain ``<td>`` cells per row (account number, type,
balance, status) with no ``<th>`` or ``headers``/``id`` association -- to
this heuristic that is structurally identical to two label:value pairs. It
therefore treats "account number" and "balance" as the *label* half of a
pair and drops them from perception entirely (see the label-half skip
below), leaving only "type" and "status" visible, misnamed after the cells
that were just discarded. Nothing in this app's Phase 4 capability reads
from that table, so it is a known, documented gap rather than a silent one:
a real fix needs column headers, which this legacy idiom does not reliably
provide either.

The other thing it gets wrong: two controls with the same accessible name in
different frames are genuinely ambiguous by name alone -- "Search" is a link
in ``navFrame`` and a submit button in ``contentFrame``. That is exactly why
``frame_path`` is part of the locator's primary key, not an afterthought.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from playwright.async_api import (
    ElementHandle,
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from grip.schemas import AXLocator, SelectorFallback
from grip.surface.base import (
    ActionOutcome,
    ActionRequest,
    AXNode,
    Observation,
    Surface,
    name_matches,
    normalize,
)

# Elements worth perceiving at all. `[role]` is a catch-all for anything the
# app annotated explicitly (dialog, alert, status here); the rest are native
# HTML elements that carry an implicit role with no attribute needed.
_SELECTOR = (
    "input:not([type=hidden]), select, textarea, button, a[href], [role], "
    "td, th, h1, h2, h3, h4, h5, h6, label"
)

# Single source of truth for role/name/value computation. See module
# docstring for why this lives here instead of split across a CDP path and a
# DOM-fallback path. Shared by both entry points below so there is exactly
# one describeOne() -- _DESCRIBE_JS (single element, used by the `read`
# action) and _DESCRIBE_ALL_JS (all matched elements in one round-trip, used
# by _scan) must never compute a node's role/name/value differently.
_HELPERS_JS = """
  function textOf(e) {
    return (e.innerText !== undefined ? e.innerText : (e.textContent || ''))
      .replace(/\\s+/g, ' ').trim();
  }

  function computeRole(e) {
    const explicit = (e.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit;
    const tag = e.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return (e.multiple || (e.size && e.size > 1)) ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'td') return 'cell';
    if (tag === 'th') return 'columnheader';
    if (tag === 'label') return 'label';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const t = (e.getAttribute('type') || 'text').toLowerCase();
      const map = {
        submit: 'button', button: 'button', reset: 'button', image: 'button',
        checkbox: 'checkbox', radio: 'radio', search: 'searchbox',
        range: 'slider', number: 'spinbutton',
      };
      return map[t] || 'textbox';
    }
    return tag;
  }

  // See module docstring: label:value pairing generalises the adjacent-cell
  // heuristic to static content, not just form fields. Computed once per
  // element and reused by name/value/skip so the three never disagree about
  // which half of a pair `e` is.
  function pairInfo(e) {
    const tag = e.tagName.toLowerCase();
    if (tag !== 'td' && tag !== 'th') return { isLabel: false, isValue: false, label: null };
    const row = e.parentElement;
    if (!row) return { isLabel: false, isValue: false, label: null };
    const cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
    const idx = cells.indexOf(e);
    const hasControl = c => !!c.querySelector('input,select,textarea,button,a[href]');
    const isPairRow = cells.length >= 2 && cells.length % 2 === 0 && !cells.some(hasControl);
    if (!isPairRow) return { isLabel: false, isValue: false, label: null };
    if (idx % 2 === 1) {
      const label = textOf(cells[idx - 1]);
      return { isLabel: false, isValue: !!label, label: label || null };
    }
    return { isLabel: true, isValue: false, label: null };
  }

  function computeName(e) {
    const labelledby = e.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\\s+/).map(id => {
        const t = document.getElementById(id);
        return t ? textOf(t) : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const ariaLabel = e.getAttribute('aria-label');
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    if (e.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
      if (lbl && textOf(lbl)) return textOf(lbl);
    }
    const wrapping = e.closest('label');
    if (wrapping && wrapping !== e) {
      const t = textOf(wrapping);
      if (t) return t;
    }
    const tag = e.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (e.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(type) && e.value) return e.value.trim();
    }
    const alt = e.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim();
    const title = e.getAttribute('title');
    if (title && title.trim()) return title.trim();
    const placeholder = e.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim();
    const pair = pairInfo(e);
    if (pair.isValue) return pair.label;
    // A <select>'s rendered text is the concatenation of every <option> --
    // that is its *content*, not its name, so it never counts as "text
    // content" for naming purposes (browsers agree: nothing computes a
    // select's accessible name from its options' text).
    const own = (tag === 'select') ? '' : textOf(e);
    if (own) return own;
    // Last resort: the field this control is inside has no name of its own
    // anywhere above, so borrow the caption cell physically to its left.
    const td = e.closest('td, th');
    if (td) {
      const prev = td.previousElementSibling;
      if (prev && (prev.tagName === 'TD' || prev.tagName === 'TH')) {
        const t = textOf(prev);
        if (t) return t;
      }
    }
    return null;
  }

  // `value` is "the content this node exists to convey" -- for a control
  // bound to an external label (input/select/textarea, a paired value cell)
  // that's its current content, distinct from its name. For everything else
  // read() has no separate concept to fall back to, so plain content roles
  // (status/alert/heading/an unpaired cell) report their own text as value
  // too, rather than making read() special-case them by guessing from name.
  const CONTENT_VALUE_ROLES = new Set([
    'status', 'alert', 'alertdialog', 'heading', 'label', 'columnheader',
  ]);
  function computeValue(e, role) {
    const tag = e.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (e.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'checkbox', 'radio', 'hidden', 'image'].includes(type)) {
        return null;
      }
      return e.value;
    }
    if (tag === 'textarea') return e.value;
    if (tag === 'select') {
      const opt = e.options[e.selectedIndex];
      return opt ? textOf(opt) : '';
    }
    if (tag === 'td' || tag === 'th') {
      const pair = pairInfo(e);
      if (pair.isLabel) return null;  // its text is already the paired value's name
      const t = textOf(e);
      return t || null;
    }
    if (CONTENT_VALUE_ROLES.has(role)) {
      const t = textOf(e);
      return t || null;
    }
    return null;
  }

  function computeChecked(e) {
    const tag = e.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (e.getAttribute('type') || '').toLowerCase();
      if (type === 'checkbox' || type === 'radio') return !!e.checked;
    }
    return null;
  }

  function cssHint(e) {
    if (e.id) return `#${CSS.escape(e.id)}`;
    const name = e.getAttribute('name');
    if (name) return `${e.tagName.toLowerCase()}[name="${name}"]`;
    return null;
  }

  const ANCESTOR_TAGS = new Set(['form', 'table', 'fieldset']);
  const ANCESTOR_ROLES = new Set(['dialog', 'alertdialog', 'region']);
  function ancestorInfo(e) {
    const roles = [];
    let name = null;
    let p = e.parentElement;
    while (p) {
      const role = (p.getAttribute('role') || '').toLowerCase();
      const tag = p.tagName.toLowerCase();
      if (ANCESTOR_ROLES.has(role) || ANCESTOR_TAGS.has(tag)) {
        roles.push(role || tag);
        if (name === null) {
          const al = p.getAttribute('aria-label');
          if (al && al.trim()) name = al.trim();
        }
      }
      p = p.parentElement;
    }
    roles.reverse();
    return { roles, name };
  }

  function isVisible(e) {
    // offsetParent-based checks (the usual shortcut) misreport `position:
    // fixed` elements as hidden -- exactly what this app's interstitial
    // dialog uses -- so visibility is judged from computed style plus an
    // actual laid-out box instead.
    const style = getComputedStyle(e);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
    const rects = e.getClientRects();
    return rects.length > 0 && rects[0].width > 0 && rects[0].height > 0;
  }

  function isDisabled(e) {
    if ('disabled' in e && e.disabled) return true;
    return e.getAttribute('aria-disabled') === 'true';
  }

  function describeOne(el) {
    const tag = el.tagName.toLowerCase();
    const role = computeRole(el);
    // A <td>/<th> that exists only to hold an interactive control is pure
    // layout -- the control itself is the meaningful node, so the wrapping
    // cell would just be a name-duplicate of it. Skip it. Likewise skip the
    // *label* half of a value pair: its text is already carried as the value
    // cell's name (see pairInfo), so keeping it too would give the pair's
    // name two nodes -- one of them (the label, ordinal 0, by DOM order)
    // with no value, which is exactly the wrong one for an extraction step
    // to land on by default.
    const isLayoutCell = (tag === 'td' || tag === 'th') &&
      !!el.querySelector('input,select,textarea,button,a[href]');
    const isLabelHalf = pairInfo(el).isLabel;
    const anc = ancestorInfo(el);
    return {
      role: role,
      name: computeName(el),
      value: computeValue(el, role),
      checked: computeChecked(el),
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      invalid: (el.getAttribute('aria-invalid') === 'true') ||
               (typeof el.checkValidity === 'function' && !el.checkValidity()),
      skip: isLayoutCell || isLabelHalf,
      cssHint: cssHint(el),
      ancestorRoles: anc.roles,
      ancestorName: anc.name,
    };
  }
"""

# Playwright's evaluate() takes exactly one function expression, so the
# shared helpers have to live *inside* each entry point's body rather than
# alongside it as separate top-level declarations.

# Single-element entry point: used by the `read` action, which already has
# the exact ElementHandle it wants to re-describe.
_DESCRIBE_JS = "(el) => {\n" + _HELPERS_JS + "\n  return describeOne(el);\n}"

# Batch entry point: used by _scan(). Folding visibility and disabled-ness
# into this one call is what collapses perception from three round-trips per
# element down to two round-trips per *frame* -- see _scan()'s docstring.
_DESCRIBE_ALL_JS = "(elements) => {\n" + _HELPERS_JS + """
  return elements.map((el) => {
    const d = describeOne(el);
    d.visible = isVisible(el);
    d.disabled = isDisabled(el);
    return d;
  });
}
"""

_TEXT_DIGEST_JS = "() => document.body ? document.body.innerText : ''"


class PlaywrightSurface(Surface):
    """The web adapter. One browser context per instance; see ``close()``."""

    kind = "web"

    def __init__(
        self,
        playwright: Any,
        browser: Any,
        context: Any,
        page: Page,
        *,
        action_timeout_s: float = 10.0,
        settle_timeout_s: float = 8.0,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._action_timeout_s = action_timeout_s
        self._settle_timeout_s = settle_timeout_s
        # Ephemeral: rebuilt by every observe()/resolve() call. Never persisted.
        self._ref_map: dict[str, ElementHandle] = {}
        self._next_fallback_id = 0

    @classmethod
    async def create(
        cls,
        *,
        headless: bool = True,
        action_timeout_s: float = 10.0,
        settle_timeout_s: float = 8.0,
    ) -> "PlaywrightSurface":
        """Launch a fresh browser + one context. Multi-tenant isolation boundary."""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        return cls(
            playwright,
            browser,
            context,
            page,
            action_timeout_s=action_timeout_s,
            settle_timeout_s=settle_timeout_s,
        )

    # -- perception ---------------------------------------------------------

    def _frame_path(self, frame: Frame) -> list[str]:
        path: list[str] = []
        f: Frame | None = frame
        while f is not None and f.parent_frame is not None:
            path.append(f.name or "<frame>")
            f = f.parent_frame
        return list(reversed(path))

    def _frame_by_path(self, frame_path: list[str]) -> Frame | None:
        for frame in self._page.frames:
            if not frame.is_detached() and self._frame_path(frame) == list(frame_path):
                return frame
        return None

    async def _dispose_ref_map(self) -> None:
        """Release the previous scan's handles browser-side.

        Each ``_scan()`` call replaces ``self._ref_map`` wholesale; letting
        the Python dict just get garbage-collected does not reliably (or
        promptly) tell the browser to free the handle it backs. A discovery
        run doing two or three observations per step, dozens of nodes per
        observation, over 25 steps otherwise leaks thousands of handles.
        """
        for handle in self._ref_map.values():
            try:
                await handle.dispose()
            except PlaywrightError:
                pass
        self._ref_map = {}

    async def _scan(self) -> tuple[list[AXNode], dict[str, ElementHandle]]:
        """Walk every live frame once. The single perception pass both

        ``observe()`` and ``resolve()`` build on, so there is exactly one
        place that decides what counts as a node.

        Per element this used to cost three round-trips (``is_visible``,
        ``evaluate``, ``is_disabled``). Visibility and disabled-ness are now
        folded into the one ``_DESCRIBE_JS`` batch call below, so each frame
        costs two round-trips total -- one ``evaluate`` for every matched
        element's data, one ``query_selector_all`` for the handles to act on
        later -- instead of three per element. The two calls only agree on
        ordering because they run back-to-back against the same selector
        with no intervening await that could let the page mutate between
        them.
        """
        await self._dispose_ref_map()
        nodes: list[AXNode] = []
        ref_map: dict[str, ElementHandle] = {}
        # Duplicate (role, name, frame, ancestor) tuples get an increasing
        # ordinal in DOM encounter order -- the same order a locator's
        # `ordinal` was recorded against, so replay finds the same one back.
        seen: dict[tuple, int] = {}
        counter = 0

        for frame in self._page.frames:
            if frame.is_detached():
                continue
            try:
                descs = await frame.eval_on_selector_all(_SELECTOR, _DESCRIBE_ALL_JS)
                handles = await frame.query_selector_all(_SELECTOR)
            except PlaywrightError:
                continue
            if len(descs) != len(handles):
                # The DOM changed between the two round-trips (rare, but
                # possible on a page with live updates) -- the index
                # alignment this depends on no longer holds, so skip this
                # frame for this scan rather than risk misattributing a
                # description to the wrong handle.
                continue

            frame_path = self._frame_path(frame)
            for desc, handle in zip(descs, handles):
                if desc.get("skip") or not desc.get("visible"):
                    continue

                role = desc["role"]
                name = desc["name"]
                ancestor_roles = desc["ancestorRoles"][-3:]
                key = (role, normalize(name), tuple(frame_path), tuple(ancestor_roles))
                ordinal = seen.get(key, 0)
                seen[key] = ordinal + 1

                ref = f"n{counter}"
                counter += 1
                node = AXNode(
                    ref=ref,
                    role=role,
                    name=name,
                    value=desc["value"],
                    frame_path=frame_path,
                    ancestor_roles=ancestor_roles,
                    ancestor_name=desc["ancestorName"],
                    disabled=desc["disabled"],
                    required=desc["required"],
                    invalid=desc["invalid"],
                    checked=desc["checked"],
                    css_hint=desc["cssHint"],
                    ordinal=ordinal,
                )
                nodes.append(node)
                ref_map[ref] = handle

        return nodes, ref_map

    async def observe(self) -> Observation:
        nodes, ref_map = await self._scan()
        self._ref_map = ref_map

        frame_paths: list[list[str]] = []
        text_parts: list[str] = []
        for frame in self._page.frames:
            if frame.is_detached():
                continue
            frame_paths.append(self._frame_path(frame))
            try:
                text = await frame.evaluate(_TEXT_DIGEST_JS)
            except PlaywrightError:
                continue
            if text:
                text_parts.append(text)

        modal_present = any(n.role in {"dialog", "alertdialog"} for n in nodes)
        return Observation(
            url=self._page.url,
            title=await self._page.title(),
            nodes=nodes,
            frame_paths=frame_paths,
            text_digest=normalize(" ".join(text_parts)),
            modal_present=modal_present,
        )

    # -- action ---------------------------------------------------------

    @staticmethod
    def _elapsed_ms(start: float) -> int:
        return int((time.monotonic() - start) * 1000)

    async def act(self, request: ActionRequest) -> ActionOutcome:
        start = time.monotonic()
        try:
            if request.action == "navigate":
                if not request.url:
                    return ActionOutcome(
                        ok=False, detail="navigate requires a url", error_kind="error"
                    )
                await self._page.goto(
                    request.url,
                    wait_until="domcontentloaded",
                    timeout=self._action_timeout_s * 1000,
                )
                await asyncio.sleep((request.settle_ms / 1000) if request.settle_ms else 0.1)
                return ActionOutcome(ok=True, duration_ms=self._elapsed_ms(start))

            handle = self._ref_map.get(request.ref) if request.ref else None
            if handle is None:
                return ActionOutcome(
                    ok=False,
                    detail=f"ref {request.ref!r} is not present in the current observation",
                    error_kind="not_found",
                )

            if request.action in ("type", "select") and request.value is None:
                return ActionOutcome(
                    ok=False, detail=f"{request.action} requires a value", error_kind="error"
                )

            timeout_ms = (request.timeout_s or self._action_timeout_s) * 1000

            if request.action == "click":
                await handle.click(timeout=timeout_ms)
            elif request.action == "type":
                await handle.fill(request.value, timeout=timeout_ms)
            elif request.action == "select":
                try:
                    await handle.select_option(label=request.value, timeout=timeout_ms)
                except PlaywrightError:
                    await handle.select_option(value=request.value, timeout=timeout_ms)
            elif request.action == "wait":
                await asyncio.sleep(request.timeout_s or self._settle_timeout_s)
            elif request.action == "read":
                desc = await handle.evaluate(_DESCRIBE_JS)
                value = desc.get("value")
                if value is None:
                    # No silent substitution of `name` here: for a plain
                    # label cell or an empty field that would report a
                    # caption (or nothing) as if it were the read value --
                    # a wrong answer that passes every check. `value` is
                    # already defined (see _DESCRIBE_JS) to equal `name` for
                    # every node type where the two are legitimately the
                    # same thing (status/alert/heading/an unpaired cell), so
                    # a `None` here means there truly is nothing to read.
                    return ActionOutcome(
                        ok=False,
                        detail="target has no readable value",
                        error_kind="not_found",
                        duration_ms=self._elapsed_ms(start),
                    )
                return ActionOutcome(ok=True, read_value=value, duration_ms=self._elapsed_ms(start))
            else:
                return ActionOutcome(
                    ok=False, detail=f"unsupported action {request.action!r}", error_kind="error"
                )

            if request.settle_ms:
                await asyncio.sleep(request.settle_ms / 1000)
            return ActionOutcome(ok=True, duration_ms=self._elapsed_ms(start))

        except PlaywrightTimeoutError:
            return ActionOutcome(
                ok=False, detail="action timed out", error_kind="timeout",
                duration_ms=self._elapsed_ms(start),
            )
        except PlaywrightError as exc:
            return ActionOutcome(
                ok=False,
                detail=str(exc),
                error_kind=self._classify_error(exc),
                duration_ms=self._elapsed_ms(start),
            )

    _STALE_MARKERS = (
        "detached",
        "not attached to the dom",
        "target closed",
        "context was destroyed",
        "execution context was destroyed",
    )

    @classmethod
    def _classify_error(cls, exc: PlaywrightError) -> str:
        """A ref from a page that has since navigated is a stale reference,

        not a surface malfunction -- the correct response is to re-resolve
        the locator, not to treat the run as broken. Both look identical as
        raw Playwright exceptions, so the message is the only signal.
        """
        message = str(exc).lower()
        if any(marker in message for marker in cls._STALE_MARKERS):
            return "not_found"
        return "error"

    # -- resolution ---------------------------------------------------------

    async def resolve(self, locator: AXLocator) -> tuple[str | None, str]:
        nodes, ref_map = await self._scan()
        self._ref_map = ref_map

        candidates = [
            n
            for n in nodes
            if n.role == locator.role
            and list(n.frame_path) == list(locator.frame_path)
            and name_matches(n.name, locator.name, locator.name_match)
            and (locator.ancestor_name is None or n.ancestor_name == locator.ancestor_name)
        ]
        if locator.ordinal < len(candidates):
            return candidates[locator.ordinal].ref, "primary"

        for fallback in locator.fallbacks:
            ref = await self._resolve_fallback(fallback, locator, nodes)
            if ref is not None:
                return ref, fallback.strategy

        return None, (
            f"no match for {locator.describe()}: {len(candidates)} primary candidate(s) "
            f"at ordinal {locator.ordinal}, {len(locator.fallbacks)} fallback(s) exhausted"
        )

    async def _resolve_fallback(
        self, fallback: SelectorFallback, locator: AXLocator, nodes: list[AXNode]
    ) -> str | None:
        if fallback.strategy == "text_proximity":
            wanted = normalize(fallback.expression)
            for n in nodes:
                if list(n.frame_path) == list(locator.frame_path) and wanted in normalize(n.name):
                    return n.ref
            return None

        if fallback.strategy == "ax_role_ordinal":
            try:
                want_ordinal = int(fallback.expression)
            except ValueError:
                return None
            same_role = [
                n
                for n in nodes
                if n.role == locator.role and list(n.frame_path) == list(locator.frame_path)
            ]
            if want_ordinal < len(same_role):
                return same_role[want_ordinal].ref
            return None

        frame = self._frame_by_path(locator.frame_path)
        if frame is None:
            return None

        if fallback.strategy == "css":
            selector = fallback.expression
        elif fallback.strategy == "xpath":
            selector = f"xpath={fallback.expression}"
        else:
            return None

        try:
            handle = await frame.query_selector(selector)
        except PlaywrightError:
            return None
        if handle is None:
            return None

        self._next_fallback_id += 1
        ref = f"fb{self._next_fallback_id}"
        self._ref_map[ref] = handle
        return ref

    # -- misc ---------------------------------------------------------

    async def screenshot(self, path: str) -> bool:
        # Capturing to bytes and writing them ourselves -- rather than
        # passing `path=` straight to Playwright -- is what the atomic
        # .part-then-rename convention actually requires: Playwright infers
        # the image format from the path's extension, and ".part" is not a
        # format it recognises, so every path-based call here would fail.
        suffix = Path(path).suffix.lstrip(".").lower()
        img_type = "jpeg" if suffix in ("jpg", "jpeg") else "png"
        try:
            data = await self._page.screenshot(full_page=True, type=img_type)
        except PlaywrightError:
            return False
        part = Path(f"{path}.part")
        part.write_bytes(data)
        os.replace(part, path)
        return True

    async def current_url(self) -> str:
        return self._page.url

    async def close(self) -> None:
        await self._dispose_ref_map()
        await self._context.close()
        await self._browser.close()
        await self._playwright.stop()
