"""Tests for keystone/config.py's loading mechanics -- Policy.classify/url_allowed

are already covered thoroughly in test_guardrails.py via Guardrails; this
file covers what wasn't tested at all: Policy.load()'s file-level
validation, and Settings.from_env()'s environment-variable parsing.
"""

from __future__ import annotations

import pytest
import yaml

from keystone.config import Policy, Settings


def write_policy(tmp_path, data: dict) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


# ---- Policy.load() -----------------------------------------


def test_load_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        Policy.load(tmp_path / "does_not_exist.yaml")


def test_load_rejects_unknown_keys(tmp_path):
    path = write_policy(tmp_path, {"allowed_origins": ["http://x"], "totally_made_up_key": True})
    with pytest.raises(ValueError, match="Unknown key"):
        Policy.load(path)


def test_load_accepts_a_well_formed_minimal_policy(tmp_path):
    path = write_policy(tmp_path, {"allowed_origins": ["http://x"]})
    policy = Policy.load(path)
    assert policy.allowed_origins == ["http://x"]


@pytest.mark.parametrize(
    "field",
    [
        "allowed_origins", "allowed_route_patterns", "denied_route_patterns",
        "allowed_actions", "risky_actions", "risky_control_patterns",
        "forbidden_control_patterns", "sensitive_field_patterns",
    ],
)
def test_load_rejects_a_string_where_a_list_is_required(tmp_path, field):
    """Regression test: `allowed_actions: click` (a plausible YAML typo

    missing the `- ` list syntax) previously loaded silently as the string
    "click", turning `action_allowed()`'s list-membership check into a
    substring check -- verified live, "lick" was wrongly allowed.
    """
    path = write_policy(tmp_path, {"allowed_origins": ["http://x"], field: "not_a_list"})
    with pytest.raises(ValueError, match="must be a YAML list"):
        Policy.load(path)


def test_load_empty_file_uses_all_defaults(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("")
    policy = Policy.load(path)
    assert policy.allowed_origins == []
    assert policy.unattended_risky_disposition == "escalate"


def test_the_real_policy_yaml_still_loads_cleanly():
    """The actual, committed policy.yaml -- not a synthetic stand-in."""
    from keystone.config import DEFAULT_POLICY_PATH

    policy = Policy.load(DEFAULT_POLICY_PATH)
    assert "http://127.0.0.1:8800" in policy.allowed_origins
    assert policy.unattended_risky_disposition == "escalate"


# ---- Settings.from_env() -----------------------------------------


def test_llm_configured_reflects_whether_a_key_is_set():
    assert Settings(llm_api_key="").llm_configured is False
    assert Settings(llm_api_key="nvapi-x").llm_configured is True


def test_from_env_picks_up_nvidia_api_key(monkeypatch):
    # A real env var always wins over .env (override=False in from_env),
    # so setting it directly is a valid test regardless of what the repo's
    # own .env file happens to contain.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = Settings.from_env()
    assert settings.llm_api_key == "nvapi-test-key"


def test_from_env_generic_llm_api_key_is_a_fallback(monkeypatch, tmp_path):
    # Isolated from the repo's real .env: load_dotenv(override=False) would
    # otherwise refill NVIDIA_API_KEY right back in after delenv(), since
    # "override=False" only means "don't clobber an already-set var," not
    # "don't load the file at all."
    monkeypatch.setattr("keystone.config.DEFAULT_ENV_PATH", tmp_path / "no_such_env_file")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "generic-key")
    settings = Settings.from_env()
    assert settings.llm_api_key == "generic-key"


def test_from_env_headless_flag_parsing(monkeypatch):
    for value, expected in [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("no", False)]:
        monkeypatch.setenv("KEYSTONE_HEADLESS", value)
        assert Settings.from_env().headless is expected, f"KEYSTONE_HEADLESS={value!r}"


def test_from_env_invalid_numeric_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("KEYSTONE_MAX_STEPS", "not-a-number")
    settings = Settings.from_env()
    assert settings.max_steps == Settings.max_steps  # class default, not a crash


def test_from_env_run_timeout_is_the_documented_default_without_override(monkeypatch):
    monkeypatch.delenv("KEYSTONE_RUN_TIMEOUT_S", raising=False)
    assert Settings.from_env().run_timeout_s == 1200.0
