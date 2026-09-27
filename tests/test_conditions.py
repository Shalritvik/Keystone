"""Tests for grip/conditions.py's substitute() -- pure regex-template logic,

no surface or browser needed. evaluate() itself is exercised end-to-end via
the real checkpoints in tests/test_replay_engine.py.
"""

from __future__ import annotations

import re

from grip.conditions import substitute


def test_substitute_replaces_a_known_token():
    assert substitute("^${member_id}$", {"member_id": "12345"}) == "^12345$"


def test_substitute_leaves_an_unknown_token_untouched():
    assert substitute("^${member_id}$", {}) == "^${member_id}$"


def test_substitute_escapes_regex_metacharacters_in_the_value():
    """Regression test: substitute() is only ever used to build a regex

    pattern (conditions.evaluate() passes its result straight to
    re.search), but the raw param value was spliced in unescaped. A value
    containing "." wildcard-matched a field that actually held a different
    character -- verified live: param "12.45" made the checkpoint pattern
    accept "12X45" as if it were correct.
    """
    pattern = substitute("^${member_id}$", {"member_id": "12.45"})
    assert pattern == r"^12\.45$"
    assert re.search(pattern, "12.45")
    assert not re.search(pattern, "12X45")
    assert not re.search(pattern, "12345")


def test_substitute_handles_multiple_tokens_independently():
    result = substitute("${a}-${b}", {"a": "x.y", "b": "1+1"})
    assert result == r"x\.y-1\+1"
