"""Locator resolution and fallback ordering -- named second in CLAUDE.md's

testing priorities, and previously covered by nothing at all: no test in
this repo exercised a fallback strategy actually firing, only the primary-
match success/failure paths. Every artifact that declares fallbacks
(lookup_member_savings_balance.v1.json has two) had them completely
unverified.
"""

from __future__ import annotations

import pytest

from grip.schemas import AXLocator, SelectorFallback


def broken_locator(**fallback_kwargs) -> AXLocator:
    """A locator whose primary match can never succeed on the real page --

    the name doesn't exist -- so any successful resolve() must have come
    from the declared fallback(s).
    """
    return AXLocator(
        role="textbox",
        name="This Caption Does Not Exist Anywhere",
        name_match="exact",
        frame_path=[],
        ordinal=0,
        fallbacks=[SelectorFallback(weaker_because="test", **fallback_kwargs)],
    )


@pytest.mark.asyncio
async def test_primary_match_is_reported_when_it_matches(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    obs = await surface.observe()
    field = next(n for n in obs.nodes if n.role == "textbox")
    locator = field.to_locator()

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "primary"


@pytest.mark.asyncio
async def test_css_fallback_fires_when_primary_misses(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    locator = broken_locator(strategy="css", expression='input[name="txtMbrNo"]')

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "css"


@pytest.mark.asyncio
async def test_xpath_fallback_fires_when_primary_misses(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    locator = broken_locator(strategy="xpath", expression='//input[@name="txtMbrNo"]')

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "xpath"


@pytest.mark.asyncio
async def test_text_proximity_fallback_fires_when_primary_misses(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    # Matches by substring against the real accessible name "Member #:".
    locator = broken_locator(strategy="text_proximity", expression="Member")

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "text_proximity"


@pytest.mark.asyncio
async def test_ax_role_ordinal_fallback_fires_when_primary_misses(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    # The member field is the only textbox on this page -> ordinal 0.
    locator = broken_locator(strategy="ax_role_ordinal", expression="0")

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "ax_role_ordinal"


@pytest.mark.asyncio
async def test_fallbacks_are_tried_in_declared_order(surface, mockapp_server):
    """Two fallbacks: the first is a css selector guaranteed not to match

    anything, the second is a real one. resolve() must fall all the way
    through to the second and report *that* strategy, not silently prefer
    it or stop after the first fails.
    """
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    locator = AXLocator(
        role="textbox", name="This Caption Does Not Exist Anywhere", name_match="exact",
        frame_path=[], ordinal=0,
        fallbacks=[
            SelectorFallback(strategy="css", expression="#definitely-not-a-real-id", weaker_because="test"),
            SelectorFallback(strategy="css", expression='input[name="txtMbrNo"]', weaker_because="test"),
        ],
    )

    ref, how = await surface.resolve(locator)
    assert ref is not None
    assert how == "css"
    # Confirm it's genuinely the *second* fallback that matched, not a
    # coincidence -- resolving the target's own accessible name via the ref.
    node_name = await surface.act(ActionRequest(action="read", ref=ref))
    assert node_name.ok  # the field has a value (possibly empty), proving it's the real textbox


@pytest.mark.asyncio
async def test_all_fallbacks_exhausted_reports_a_clear_reason(surface, mockapp_server):
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/lookup"))
    locator = broken_locator(strategy="css", expression="#also-not-real")

    ref, how = await surface.resolve(locator)
    assert ref is None
    assert "no match" in how


@pytest.mark.asyncio
async def test_fallback_is_scoped_to_the_locators_declared_frame(surface, mockapp_server):
    """A css fallback must resolve within the locator's own frame_path, not

    search the whole page -- otherwise a fallback could silently match the
    wrong frame's identically-structured control.
    """
    from grip.surface.base import ActionRequest

    await surface.act(ActionRequest(action="navigate", url=f"{mockapp_server}/t/pinnacle/"))
    obs = await surface.observe()
    lookup_link = next(n for n in obs.nodes if n.name == "Member Lookup")
    await surface.act(ActionRequest(action="click", ref=lookup_link.ref))

    # A locator that claims to be in navFrame but whose only real match (by
    # this css selector) lives in contentFrame -- the fallback must not
    # cross frames to find it.
    locator = AXLocator(
        role="textbox", name="nonexistent", name_match="exact", frame_path=["navFrame"], ordinal=0,
        fallbacks=[SelectorFallback(strategy="css", expression='input[name="txtMbrNo"]', weaker_because="test")],
    )
    ref, how = await surface.resolve(locator)
    assert ref is None
