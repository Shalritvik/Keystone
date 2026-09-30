"""Tests for keystone/llm.py that need no network access and no API key --

replay must never depend on this being configured, so that guarantee has to
be checkable without one.
"""

from __future__ import annotations

import pytest

from keystone.config import Settings
from keystone.llm import LLMClient, LLMError


def test_unconfigured_without_api_key():
    client = LLMClient(Settings(llm_api_key=""))
    assert client.configured is False


def test_configured_with_api_key():
    client = LLMClient(Settings(llm_api_key="nvapi-fake-key-for-testing"))
    assert client.configured is True


@pytest.mark.asyncio
async def test_complete_json_refuses_without_configuration():
    client = LLMClient(Settings(llm_api_key=""))
    with pytest.raises(LLMError):
        await client.complete_json(system="s", user="u")
