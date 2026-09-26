"""Tests for grip/cli.py's argument handling and guardrail behaviours that

don't need a live browser -- draft refusal, unknown capability, and the
approve/overwrite protections all short-circuit before ever touching a
surface, so they're plain synchronous tests against a temp artifact dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grip.cli import _load_artifact, _parse_params, build_parser
from grip.config import Settings
from grip.schemas import CapabilityArtifact

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOKUP_ARTIFACT = REPO_ROOT / "artifacts" / "lookup_member_savings_balance.v1.json"


def test_parse_params_splits_on_first_equals():
    assert _parse_params(["member_id=12345", "note=a=b"]) == {"member_id": "12345", "note": "a=b"}


def test_parse_params_rejects_missing_equals():
    with pytest.raises(SystemExit):
        _parse_params(["not-a-pair"])


def test_load_artifact_picks_the_highest_version(tmp_path):
    settings = Settings(artifact_dir=tmp_path)
    base = json.loads(LOOKUP_ARTIFACT.read_text())

    for version in (1, 2, 10):  # numeric ordering, not lexical -- v10 must beat v2
        data = dict(base, version=version)
        (tmp_path / f"lookup_member_savings_balance.v{version}.json").write_text(json.dumps(data))

    artifact, path = _load_artifact(settings, "lookup_member_savings_balance")
    assert artifact.version == 10
    assert path.name.endswith("v10.json")


def test_load_artifact_raises_when_nothing_matches(tmp_path):
    settings = Settings(artifact_dir=tmp_path)
    with pytest.raises(SystemExit):
        _load_artifact(settings, "no_such_capability")


def test_replay_param_flag_survives_any_ordering_relative_to_other_flags():
    """Regression test: a bare nargs="*" positional for params silently

    failed to parse once an optional flag appeared on either side of it
    (found live: `replay X --tenant harbor member_id=12345` errored with
    "unrecognized arguments"). --param as a named, repeatable flag has no
    such ambiguity.
    """
    parser = build_parser()

    args = parser.parse_args(["replay", "cap", "--tenant", "harbor", "--param", "member_id=12345"])
    assert args.params == ["member_id=12345"]
    assert args.tenant == "harbor"

    args = parser.parse_args(["replay", "cap", "--param", "member_id=12345", "--tenant", "harbor"])
    assert args.params == ["member_id=12345"]
    assert args.tenant == "harbor"


def test_replay_subcommand_requires_capability_id():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["replay"])


def test_approve_subcommand_requires_by():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["approve", "some_capability"])


def test_discover_subcommand_requires_entry_and_capability_id():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "a goal"])


def test_catalog_defaults_to_all_states():
    parser = build_parser()
    args = parser.parse_args(["catalog"])
    assert args.state == "all"


def test_saved_artifact_is_still_valid_against_the_schema():
    CapabilityArtifact.model_validate(json.loads(LOOKUP_ARTIFACT.read_text()))
