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
        # Changing `version` changes the sealed content, so re-seal each
        # variant -- otherwise _check_seal (correctly) refuses it as an
        # approved artifact edited after approval.
        artifact = CapabilityArtifact.model_validate(dict(base, version=version))
        artifact.seal()
        (tmp_path / f"lookup_member_savings_balance.v{version}.json").write_text(artifact.model_dump_json())

    artifact, path = _load_artifact(settings, "lookup_member_savings_balance")
    assert artifact.version == 10
    assert path.name.endswith("v10.json")


def test_load_artifact_raises_when_nothing_matches(tmp_path):
    settings = Settings(artifact_dir=tmp_path)
    with pytest.raises(SystemExit):
        _load_artifact(settings, "no_such_capability")


def test_load_artifact_refuses_an_approved_artifact_hand_edited_after_approval(tmp_path):
    """Regression test: seal()/compute_hash() exist specifically to let a

    reviewer's approval bind to exact content, but nothing checked the
    stored hash against the loaded content -- verified live, a hand-edited
    checkpoint pattern on an approved artifact went completely unnoticed.
    """
    settings = Settings(artifact_dir=tmp_path)
    data = json.loads(LOOKUP_ARTIFACT.read_text())
    assert data["approval"]["state"] == "approved"
    data["steps"][1]["checkpoint"]["pattern"] = "hand-edited-after-approval"
    (tmp_path / "lookup_member_savings_balance.v1.json").write_text(json.dumps(data))

    with pytest.raises(SystemExit, match="does not match the hash"):
        _load_artifact(settings, "lookup_member_savings_balance")


def test_load_artifact_allows_an_unsealed_draft(tmp_path):
    """A draft makes no promise about its content yet, so an empty or

    stale content_hash on a draft is not an error -- only an approved
    artifact's hash is load-bearing.
    """
    settings = Settings(artifact_dir=tmp_path)
    data = json.loads(LOOKUP_ARTIFACT.read_text())
    data["approval"]["state"] = "draft"
    data["steps"][1]["checkpoint"]["pattern"] = "edited-while-still-a-draft"
    (tmp_path / "lookup_member_savings_balance.v1.json").write_text(json.dumps(data))

    artifact, _path = _load_artifact(settings, "lookup_member_savings_balance")
    assert artifact.approval.state == "draft"


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


def test_catalog_skips_a_malformed_artifact_instead_of_crashing(tmp_path, monkeypatch, capsys):
    """Regression test: one unrelated bad file in artifacts/ (a WIP hand-edit,

    a leftover from a crashed process) previously crashed catalog entirely,
    hiding every other, perfectly good capability behind a traceback.
    """
    from argparse import Namespace

    from grip.cli import cmd_catalog

    monkeypatch.setenv("GRIP_ARTIFACT_DIR", str(tmp_path))
    (tmp_path / "broken.v1.json").write_text("{ not valid json")
    good = json.loads(LOOKUP_ARTIFACT.read_text())
    (tmp_path / "good.v1.json").write_text(json.dumps(good))

    exit_code = cmd_catalog(Namespace(state="all"))
    out = capsys.readouterr()

    assert exit_code == 0
    assert "skipping unreadable artifact" in out.err
    schemas = json.loads(out.out)
    assert [s["function"]["name"] for s in schemas] == ["lookup_member_savings_balance"]


def test_catalog_skips_an_approved_artifact_hand_edited_after_approval(tmp_path, monkeypatch, capsys):
    """A tampered "approved" artifact must not be handed to a calling agent

    as a trustworthy tool schema -- catalog's whole job is to be that
    agent-facing surface (module docstring), so this is the same integrity
    gap as replay, checked at the same seam _check_seal already covers.
    """
    from argparse import Namespace

    from grip.cli import cmd_catalog

    monkeypatch.setenv("GRIP_ARTIFACT_DIR", str(tmp_path))
    tampered = json.loads(LOOKUP_ARTIFACT.read_text())
    tampered["steps"][1]["checkpoint"]["pattern"] = "hand-edited-after-approval"
    (tmp_path / "tampered.v1.json").write_text(json.dumps(tampered))

    exit_code = cmd_catalog(Namespace(state="all"))
    out = capsys.readouterr()

    assert exit_code == 0
    assert "does not match the hash" in out.err
    assert json.loads(out.out) == []
