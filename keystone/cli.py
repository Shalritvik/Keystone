"""The CLI: discover, replay, approve, catalog.

    python -m keystone discover "<goal>" --entry <url> --capability-id <id> [--tenant T] [--force]
    python -m keystone replay <capability_id> [param=value ...] [--tenant T] [--attended]
    python -m keystone approve <capability_id> [param=value ...] --by <name> [--note TEXT] [--runs N] [--force]
    python -m keystone catalog [--state draft|approved|deprecated|all]

``catalog``'s default is ``all`` deliberately: this command is a human
operator's view into the system, not the agent-facing surface itself -- a
real calling agent should be handed ``to_tool_schema()`` only for artifacts
it filtered to ``state == "approved"`` itself, since an unapproved capability
is exactly the thing rule 8 in CLAUDE.md says needs a human in the loop
first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from keystone.config import Policy, Settings
from keystone.discovery.agent import discover as run_discovery
from keystone.guardrails import GuardedSurface, Guardrails
from keystone.reliability import ReliabilityReport, assess
from keystone.replay.engine import ReplayEngine
from keystone.schemas import CapabilityArtifact
from keystone.surface.web import PlaywrightSurface


def _version_of(path: Path) -> int:
    match = re.search(r"\.v(\d+)\.json$", path.name)
    return int(match.group(1)) if match else 0


def _check_seal(artifact: CapabilityArtifact, path: Path) -> None:
    """Refuses a hand-edited approved artifact rather than replaying it silently.

    ``seal()`` hashes everything except provenance/approval specifically so
    that its identity survives approval and re-recording (schemas.py's
    ``compute_hash`` docstring) -- but nothing checks that identity against
    anything, which means an approved artifact edited on disk after the
    fact (a step's target, a checkpoint pattern) replays exactly as if a
    human had reviewed the edited version. Verified live: hand-editing an
    approved artifact's checkpoint pattern left its stored content_hash
    unchanged and nothing noticed. Only checked for approved artifacts --
    a draft makes no promise about its content yet (CLAUDE.md rule 8).
    """
    if artifact.approval.state != "approved" or not artifact.provenance.content_hash:
        return
    if artifact.provenance.content_hash != artifact.compute_hash():
        raise SystemExit(
            f"error: {path} is marked approved but its content does not match the hash "
            f"recorded at approval time (expected {artifact.provenance.content_hash}, got "
            f"{artifact.compute_hash()}). It was edited after approval -- re-approve it "
            "(`approve`, re-run reliability) or restore it, rather than replaying content "
            "nobody has reviewed."
        )


def _load_artifact(settings: Settings, capability_id: str) -> tuple[CapabilityArtifact, Path]:
    candidates = list(settings.artifact_dir.glob(f"{capability_id}.v*.json"))
    if not candidates:
        raise SystemExit(f"no artifact found for capability_id {capability_id!r} in {settings.artifact_dir}")
    path = max(candidates, key=_version_of)
    artifact = CapabilityArtifact.model_validate(json.loads(path.read_text()))
    _check_seal(artifact, path)
    return artifact, path


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {pair!r}")
        params[key] = value
    return params


def _next_discovery_version(settings: Settings, capability_id: str, *, force: bool) -> int:
    """Picks the version slot a fresh discovery run should write to.

    No existing artifact: start at v1, as always. An existing *draft* is
    still provisional -- nothing has reviewed it yet -- so re-discovering
    overwrites it in place; forking off a new version number for every
    tuning iteration would just litter the artifact store. An existing
    *approved* artifact is different: it may be running unattended in
    production right now, and silently overwriting it would destroy the
    exact content a human reviewed, with no diff and no rollback -- the
    file `--force` used to just clobber. Found live: `--force` skipped the
    approved-check but the save path was still hardcoded to `.v1.json`, so
    forcing a re-discovery of an approved capability didn't supersede it,
    it erased it. Approved now bumps to the next version instead; refusing
    without `--force` is unchanged.
    """
    candidates = list(settings.artifact_dir.glob(f"{capability_id}.v*.json"))
    if not candidates:
        return 1
    latest_path = max(candidates, key=_version_of)
    latest = CapabilityArtifact.model_validate(json.loads(latest_path.read_text()))
    latest_version = _version_of(latest_path)
    if latest.approval.state == "approved" and force:
        return latest_version + 1
    return latest_version


async def cmd_discover(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    candidates = list(settings.artifact_dir.glob(f"{args.capability_id}.v*.json"))
    if candidates and not args.force:
        latest_path = max(candidates, key=_version_of)
        existing = CapabilityArtifact.model_validate(json.loads(latest_path.read_text()))
        if existing.approval.state == "approved":
            print(
                f"error: {latest_path} is an approved artifact. Re-run with --force to let a "
                "fresh discovery run save as a new version, or use a different --capability-id.",
                file=sys.stderr,
            )
            return 2

    version = _next_discovery_version(settings, args.capability_id, force=args.force)
    verify_params = _parse_params(args.verify_param) if args.verify_param else None
    outcome = await run_discovery(
        args.goal, args.entry, capability_id=args.capability_id, tenant=args.tenant,
        verify_params=verify_params, version=version,
    )

    print(f"ok={outcome.ok}", file=sys.stderr)
    print(f"reason={outcome.reason}", file=sys.stderr)
    print(f"discovery_run={outcome.discovery_run_id}", file=sys.stderr)
    if outcome.verification_run_id:
        print(f"verification_run={outcome.verification_run_id}", file=sys.stderr)
    if outcome.artifact_path:
        print(f"saved: {outcome.artifact_path}", file=sys.stderr)
    return 0 if outcome.ok else 1


async def cmd_replay(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    policy = Policy.load(settings.policy_path)
    artifact, _path = _load_artifact(settings, args.capability_id)

    if artifact.approval.state != "approved" and not args.attended:
        print(
            f"error: {args.capability_id!r} is {artifact.approval.state!r}, not approved. "
            "Unattended replay refuses a draft -- pass --attended to run it under live "
            "supervision, or `approve` it first.",
            file=sys.stderr,
        )
        return 2

    headless = settings.headless and not args.attended
    raw = await PlaywrightSurface.create(headless=headless, action_timeout_s=settings.action_timeout_s)
    guarded = GuardedSurface(raw, Guardrails(policy, attended=args.attended))
    try:
        engine = ReplayEngine(guarded, settings.evidence_dir)
        result = await engine.run(artifact, _parse_params(args.params), tenant=args.tenant)
    finally:
        await guarded.close()

    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    print(f"STATUS: {result.status}", file=sys.stderr)
    return 0 if result.ok else 1


async def cmd_approve(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    policy = Policy.load(settings.policy_path)
    artifact, path = _load_artifact(settings, args.capability_id)

    params = _parse_params(args.params)
    report: ReliabilityReport | None = None
    if params:
        async def factory() -> GuardedSurface:
            raw = await PlaywrightSurface.create(headless=settings.headless, action_timeout_s=settings.action_timeout_s)
            return GuardedSurface(raw, Guardrails(policy, attended=False))

        report = await assess(factory, settings.evidence_dir, artifact, params, tenant=args.tenant, n_runs=args.runs)
        print(f"reliability: {report.summary()}", file=sys.stderr)
        if not report.is_healthy and not args.force:
            print(
                "error: reliability check did not pass cleanly. Re-run with --force to approve "
                "anyway -- the human approver is accountable for that call, not this tool.",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            "warning: no param=value given, skipping the reliability check (nothing to replay "
            "with). Pass e.g. member_id=12345 to run it before approving.",
            file=sys.stderr,
        )

    artifact.approval.state = "approved"
    artifact.approval.approved_by = args.by
    artifact.approval.approved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.note:
        artifact.approval.note = args.note
    artifact.seal()
    path.write_text(artifact.model_dump_json(indent=2))

    print(f"approved {args.capability_id!r} by {args.by!r} -> {path}", file=sys.stderr)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    schemas = []
    for candidate_path in sorted(settings.artifact_dir.glob("*.json")):
        try:
            artifact = CapabilityArtifact.model_validate(json.loads(candidate_path.read_text()))
            _check_seal(artifact, candidate_path)
        except (json.JSONDecodeError, ValidationError) as exc:
            # One unrelated bad file (a WIP hand-edit, a leftover from a
            # crashed process) must not take the whole catalog down for
            # every other, perfectly good capability -- found live: a
            # single malformed artifacts/*.json crashed catalog entirely.
            print(f"warning: skipping unreadable artifact {candidate_path}: {exc}", file=sys.stderr)
            continue
        except SystemExit as exc:
            # An approved-but-tampered artifact is worse than unreadable --
            # do not hand a calling agent a tool schema for content nobody
            # has actually reviewed.
            print(f"warning: skipping {candidate_path}: {exc}", file=sys.stderr)
            continue
        if args.state != "all" and artifact.approval.state != args.state:
            continue
        schemas.append(artifact.to_tool_schema())
    print(json.dumps(schemas, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keystone")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="run a real LLM-driven discovery session")
    p_discover.add_argument("goal")
    p_discover.add_argument("--entry", required=True)
    p_discover.add_argument("--tenant")
    p_discover.add_argument("--capability-id", required=True)
    p_discover.add_argument(
        "--force", action="store_true",
        help="rediscover an approved capability as a new version instead of refusing",
    )
    p_discover.add_argument(
        "--verify-param", action="append", metavar="key=value",
        help=(
            "a known-valid alternate param value for the mandatory generalisation replay "
            "(repeatable). Without this, a generic mutation is used, which can land on a "
            "value with no real record -- correctly failing verification for an artifact "
            "that hasn't yet had a business outcome declared for that case, rather than "
            "a bug in the discovered flow itself."
        ),
    )

    # --param key=value (repeatable), not a bare nargs="*" positional: argparse
    # cannot reliably tell where a variadic positional ends once an optional
    # flag appears on either side of it (verified live -- `replay X
    # --tenant harbor member_id=12345` silently fails to parse), while a
    # named, repeatable flag has no such ambiguity regardless of order.
    p_replay = sub.add_parser("replay", help="deterministic replay, no LLM")
    p_replay.add_argument("capability_id")
    p_replay.add_argument("--param", action="append", default=[], metavar="key=value", dest="params")
    p_replay.add_argument("--tenant")
    p_replay.add_argument("--attended", action="store_true", help="run headed; allows a draft and risky actions")

    p_approve = sub.add_parser("approve", help="promote draft -> approved")
    p_approve.add_argument("capability_id")
    p_approve.add_argument(
        "--param", action="append", default=[], metavar="key=value", dest="params",
        help="key=value pairs to run the reliability check with",
    )
    p_approve.add_argument("--by", required=True, help="who is approving this")
    p_approve.add_argument("--note", default="")
    p_approve.add_argument("--tenant")
    p_approve.add_argument("--runs", type=int, default=3)
    p_approve.add_argument("--force", action="store_true", help="approve even if the reliability check fails")

    p_catalog = sub.add_parser("catalog", help="list saved artifacts as agent-invocable tool schemas")
    p_catalog.add_argument("--state", default="all", choices=["draft", "approved", "deprecated", "all"])

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "discover":
        return asyncio.run(cmd_discover(args))
    if args.command == "replay":
        return asyncio.run(cmd_replay(args))
    if args.command == "approve":
        return asyncio.run(cmd_approve(args))
    if args.command == "catalog":
        return cmd_catalog(args)
    return 2  # argparse's `required=True` on the subparser makes this unreachable


if __name__ == "__main__":
    raise SystemExit(main())
