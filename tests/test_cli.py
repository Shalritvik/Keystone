"""Tests for keystone/cli.py's argument handling and guardrail behaviours that

don't need a live browser -- draft refusal, unknown capability, and the
approve/overwrite protections all short-circuit before ever touching a
surface, so they're plain synchronous tests against a temp artifact dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone.cli import _load_artifact, _next_discovery_version, _parse_params, build_parser
from keystone.config import Settings
from keystone.schemas import CapabilityArtifact

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


# ---- _next_discovery_version -----------------------------------------
#
# Regression coverage: `--force` used to skip the approved-check but still
# save to the hardcoded `.v1.json` path, so forcing a rediscovery of an
# approved capability didn't supersede it -- it erased the exact content a
# human had reviewed, with no diff and no rollback. Fixed by bumping to the
# next version instead of overwriting in place, but only when the existing
# artifact is actually approved; an unreviewed draft still overwrites, since
# forking a new version number on every tuning iteration would just litter
# the artifact store with nothing anyone needs to review.


def _write_version(tmp_path, base: dict, version: int, *, approved: bool) -> None:
    data = dict(base, version=version)
    if approved:
        data["approval"] = {"state": "approved", "approved_by": "test", "note": ""}
    else:
        data["approval"] = {"state": "draft", "approved_by": None, "note": ""}
    artifact = CapabilityArtifact.model_validate(data)
    artifact.seal()
    (tmp_path / f"lookup_member_savings_balance.v{version}.json").write_text(artifact.model_dump_json())


def test_next_discovery_version_starts_at_one_when_nothing_exists(tmp_path):
    settings = Settings(artifact_dir=tmp_path)
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=False) == 1


def test_next_discovery_version_overwrites_an_unreviewed_draft_in_place(tmp_path):
    base = json.loads(LOOKUP_ARTIFACT.read_text())
    _write_version(tmp_path, base, 1, approved=False)
    settings = Settings(artifact_dir=tmp_path)
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=False) == 1


def test_next_discovery_version_bumps_an_approved_artifact_only_with_force(tmp_path):
    base = json.loads(LOOKUP_ARTIFACT.read_text())
    _write_version(tmp_path, base, 1, approved=True)
    settings = Settings(artifact_dir=tmp_path)

    # cmd_discover refuses before ever reaching this without --force; called
    # directly, the function's own contract is "no bump unless forced".
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=False) == 1
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=True) == 2


def test_next_discovery_version_targets_the_highest_existing_version(tmp_path):
    base = json.loads(LOOKUP_ARTIFACT.read_text())
    _write_version(tmp_path, base, 1, approved=True)
    _write_version(tmp_path, base, 2, approved=False)  # a later draft, not yet reviewed
    settings = Settings(artifact_dir=tmp_path)

    # The highest version (2) is a draft, so it overwrites in place --
    # v1's approval is untouched and not what force is even evaluated against.
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=False) == 2
    assert _next_discovery_version(settings, "lookup_member_savings_balance", force=True) == 2


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

    from keystone.cli import cmd_catalog

    monkeypatch.setenv("KEYSTONE_ARTIFACT_DIR", str(tmp_path))
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

    from keystone.cli import cmd_catalog

    monkeypatch.setenv("KEYSTONE_ARTIFACT_DIR", str(tmp_path))
    tampered = json.loads(LOOKUP_ARTIFACT.read_text())
    tampered["steps"][1]["checkpoint"]["pattern"] = "hand-edited-after-approval"
    (tmp_path / "tampered.v1.json").write_text(json.dumps(tampered))

    exit_code = cmd_catalog(Namespace(state="all"))
    out = capsys.readouterr()

    assert exit_code == 0
    assert "does not match the hash" in out.err
    assert json.loads(out.out) == []
