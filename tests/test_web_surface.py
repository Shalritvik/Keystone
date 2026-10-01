"""Tests for keystone/surface/web.py against the real mock app.

This file didn't exist until a deep adversarial pass found a real,
completely untested bug here: `wait(ref=None)` -- exactly how discovery's
agent loop calls it, and how the schema explicitly allows a target-less
`wait` step -- unconditionally failed with error_kind="not_found", because
the generic ref-required check ran before the wait branch was ever reached.
Zero prior coverage meant it shipped through five phases undetected.
"""

from __future__ import annotations

import time

import pytest

from keystone.surface.base import ActionRequest


@pytest.mark.asyncio
async def test_wait_with_no_ref_succeeds(surface, mockapp_server):
    """Regression test for the bug found during the deep pass: wait() is the

    one action (besides navigate) that acts on the page, not a specific
    element, and must not be routed through the ref-lookup that every
    targeted action needs.
    """
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    t0 = time.monotonic()
    outcome = await surface.act(ActionRequest(action="wait", ref=None, timeout_s=0.3))
    elapsed = time.monotonic() - t0

    assert outcome.ok is True
    assert outcome.error_kind is None
    assert 0.25 < elapsed < 2.0


@pytest.mark.asyncio
async def test_wait_with_a_stale_ref_still_succeeds(surface, mockapp_server):
    """A ref happening to be set (e.g. carried over from a prior step's

    ActionRequest by a careless caller) must not matter -- wait doesn't
    address anything by ref, so an invalid one shouldn't break it either.
    """
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    outcome = await surface.act(ActionRequest(action="wait", ref="not_a_real_ref", timeout_s=0.1))
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_adjacent_cell_naming_differs_by_tenant(surface, mockapp_server):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    pinnacle_obs = await surface.observe()
    pinnacle_field = next(n for n in pinnacle_obs.nodes if n.role == "textbox")
    assert pinnacle_field.name == "Member #:"

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/harbor/lookup"))
    harbor_obs = await surface.observe()
    harbor_field = next(n for n in harbor_obs.nodes if n.role == "textbox")
    assert harbor_field.name == "Account Number:"


@pytest.mark.asyncio
async def test_same_name_different_frame_are_distinct_nodes(surface, mockapp_server):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/"))
    obs = await surface.observe()
    lookup_link = next(n for n in obs.nodes if n.name == "Member Lookup")
    await surface.act(ActionRequest(action="click", ref=lookup_link.ref))

    obs2 = await surface.observe()
    searches = [(n.role, n.name, n.frame_path) for n in obs2.nodes if n.name == "Search"]
    assert ("link", "Search", ["navFrame"]) in searches
    assert ("button", "Search", ["contentFrame"]) in searches


@pytest.mark.asyncio
async def test_resolve_fails_when_target_absent_and_succeeds_again_after_navigating_back(surface, mockapp_server):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    obs = await surface.observe()
    field = next(n for n in obs.nodes if n.role == "textbox")
    locator = field.to_locator()

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/member/12345"))
    ref, how = await surface.resolve(locator)
    assert ref is None

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "primary"


@pytest.mark.asyncio
async def test_screenshot_writes_a_real_png(surface, mockapp_server, tmp_path):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    path = tmp_path / "shot.png"
    ok = await surface.screenshot(str(path))
    assert ok is True
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not path.with_suffix(".png.part").exists()


@pytest.mark.asyncio
async def test_combobox_exposes_its_real_options(surface, mockapp_server):
    """Regression test for a bug found during a real discovery run: the

    sub-account type <select>'s options are labelled "S07 - VACATION CLUB"
    (code + label), but neither the AXNode's name (never includes option
    text) nor its value (the currently-selected one, here the blank
    placeholder) told the model what it could actually pick -- it had no
    way to know the real strings without guessing from the goal text alone.
    """
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/subaccount/new"))
    obs = await surface.observe()
    combo = next(n for n in obs.nodes if n.role == "combobox")
    assert combo.options == [
        "— Select —", "S02 - SECONDARY SAVINGS", "S07 - VACATION CLUB",
        "S09 - HOLIDAY CLUB", "S12 - YOUTH SAVINGS",
    ]
    assert "options=" in combo.render()


@pytest.mark.asyncio
async def test_select_matches_the_exact_compound_label(surface, mockapp_server):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/subaccount/new"))
    combo = next(n for n in (await surface.observe()).nodes if n.role == "combobox")
    outcome = await surface.act(ActionRequest(action="select", ref=combo.ref, value="S07 - VACATION CLUB"))
    assert outcome.ok is True


@pytest.mark.asyncio
async def test_select_falls_back_to_a_unique_substring_match(surface, mockapp_server):
    """The exact bug found live: a caller passing the bare label ("VACATION

    CLUB") that matches neither an option's value ("S07") nor its full label
    ("S07 - VACATION CLUB") used to make Playwright's own select_option()
    hang for the full action timeout, twice (label= then value=), before
    failing. It now matches the one option that contains the requested text.
    """
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/subaccount/new"))
    combo = next(n for n in (await surface.observe()).nodes if n.role == "combobox")
    outcome = await surface.act(ActionRequest(action="select", ref=combo.ref, value="VACATION CLUB"))
    assert outcome.ok is True

    read_back = await surface.act(ActionRequest(action="read", ref=combo.ref))
    assert read_back.read_value == "S07 - VACATION CLUB"


@pytest.mark.asyncio
async def test_select_with_no_matching_option_fails_fast_with_available_options_listed(surface, mockapp_server):
    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/subaccount/new"))
    combo = next(n for n in (await surface.observe()).nodes if n.role == "combobox")

    t0 = time.monotonic()
    outcome = await surface.act(ActionRequest(action="select", ref=combo.ref, value="GOLD MEMBERSHIP"))
    elapsed = time.monotonic() - t0

    assert outcome.ok is False
    assert outcome.error_kind == "not_found"
    assert "S07 - VACATION CLUB" in outcome.detail
    assert elapsed < 2.0  # previously: two full action-timeout waits (40s)
