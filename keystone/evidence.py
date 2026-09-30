"""Crash-safe evidence recording.

Every discovery and replay run writes here, and this file is the only thing
standing between "the process died" and "we can say exactly where." Three
design decisions follow directly from that:

1. **Append-only JSONL, flushed and fsynced per record.** A run that buffers
   its trace in memory and writes it at the end tells you nothing if the
   process is killed at step 14 of 20 -- which is precisely the situation an
   operator escalation or a crash needs evidence *for*. Every record is
   written, flushed, and fsynced before the next action even starts.

2. **Two records per step, not one.** ``step_started`` is written before the
   action is attempted; ``step_finished`` after. If a run dies mid-action,
   the trace ends in an orphaned ``step_started`` with no matching
   ``step_finished`` -- and that gap *is* the crash location, not something
   inferred after the fact from a stack trace that may not exist.

3. **Redaction happens in the record's ``__post_init__``, not in a
   serialisation step.** A record that carries a live, unredacted credential
   in memory has a window -- however small -- in which a crash, a debugger,
   or a careless log statement elsewhere in the process can observe it. A
   record whose constructor already replaced the value the instant it was
   built has no such window: by the time the object exists at all, there is
   nothing left in it to leak. The redaction is not a step that can be
   skipped by a caller who forgets to call it; there is no unredacted state
   to forget to clean up.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "1.0"
REDACTED = "***REDACTED***"

StepStatus = Literal["ok", "recovered", "failed", "skipped"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ==========================================================================
# Records
# ==========================================================================
#
# Plain dataclasses, not the discovery/replay schemas in schemas.py -- those
# describe a *capability*; these describe a *log line*. Keeping them
# separate means a change to one never forces a change to the other.


@dataclass
class RunStarted:
    kind: str = field(default="run_started", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    capability_id: str = ""
    tenant: str | None = None
    entry_url: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    sensitive_params: InitVar[frozenset[str]] = frozenset()
    """Constructor-only: which `params` keys to redact. Not itself stored or
    serialised -- a key *name* like "password" isn't sensitive on its own,
    and this set has no meaning once the redaction it drives has happened.
    """

    def __post_init__(self, sensitive_params: frozenset[str]) -> None:
        if sensitive_params:
            self.params = {
                k: (REDACTED if k in sensitive_params else v) for k, v in self.params.items()
            }


@dataclass
class StepStarted:
    kind: str = field(default="step_started", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    step_index: int = 0
    action: str = ""
    target: str | None = None
    value: str | None = None
    sensitive: bool = False
    """If true, `value` is redacted in __post_init__ -- see module docstring."""
    timestamp: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.sensitive and self.value is not None:
            self.value = REDACTED


@dataclass
class StepFinished:
    kind: str = field(default="step_finished", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    step_index: int = 0
    status: StepStatus = "ok"
    expected: str | None = None
    observed: str | None = None
    detail: str = ""
    recoveries_applied: list[str] = field(default_factory=list)
    screenshot: str | None = None
    duration_ms: int = 0
    sensitive: bool = False
    """If true, `observed` is redacted -- a checkpoint can read a sensitive
    field's live value even though the step that wrote it never did.
    """
    timestamp: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.sensitive and self.observed is not None:
            self.observed = REDACTED


@dataclass
class RunFinished:
    kind: str = field(default="run_finished", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    status: str = ""
    duration_ms: int = 0
    timestamp: str = field(default_factory=_now)


@dataclass
class ModelDecision:
    """Discovery-only: the model's raw decision, before ref validation or

    guardrail checks run. Exists for exactly one reason -- "a structured log
    of what the agent did and *why*" (the brief's evidence requirement) means
    the model's stated reasoning, not just the action that resulted. Recorded
    even when the ref turns out to be invalid, so a reviewer can see what was
    claimed against what was actually validated/executed in the surrounding
    step_started/step_finished pair.
    """

    kind: str = field(default="model_decision", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    step_index: int = 0
    action: str = ""
    ref: str | None = None
    value: str | None = None
    reason: str = ""
    sensitive: bool = False
    timestamp: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.sensitive and self.value is not None:
            self.value = REDACTED


@dataclass
class EscalationRaised:
    """Automation paused and handed the live session to a human. Carries

    exactly what the brief asks an intervention request to carry: which
    capability/goal, the current step, and why it stopped. The screenshot
    and full surface state live alongside this in the same evidence dir
    (screenshots/) rather than being duplicated into the record itself.
    """

    kind: str = field(default="escalation_raised", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    request_id: str = ""
    capability_id: str | None = None
    goal: str | None = None
    step_index: int | None = None
    reason: str = ""
    screenshot: str | None = None
    timestamp: str = field(default_factory=_now)


@dataclass
class EscalationResumed:
    """Control was handed back to automation. ``note`` records what the

    human did or decided, per the brief's "record what the human did."
    """

    kind: str = field(default="escalation_resumed", init=False)
    schema_version: str = field(default=SCHEMA_VERSION, init=False)
    run_id: str = ""
    request_id: str = ""
    note: str = ""
    timestamp: str = field(default_factory=_now)


Record = (
    RunStarted | StepStarted | StepFinished | RunFinished | ModelDecision
    | EscalationRaised | EscalationResumed
)

_KIND_TO_TYPE: dict[str, type] = {
    "run_started": RunStarted,
    "step_started": StepStarted,
    "step_finished": StepFinished,
    "run_finished": RunFinished,
    "model_decision": ModelDecision,
    "escalation_raised": EscalationRaised,
    "escalation_resumed": EscalationResumed,
}


# ==========================================================================
# Writer
# ==========================================================================


class EvidenceWriter:
    """One instance per run_id, one writer per directory.

    Layout matches ``evidence/README.md``::

        <run_dir>/
          run.jsonl        append-only, one record per line
          result.json      atomic; presence means the run finished
          screenshots/      captured on failure only
    """

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.run_dir / "run.jsonl"
        # line-buffered, but flush()+fsync() after every write regardless --
        # buffering is not a substitute for the durability guarantee here.
        self._fh = self._events_path.open("a", buffering=1, encoding="utf-8")

    def write(self, record: Record) -> None:
        line = json.dumps(dataclasses.asdict(record), sort_keys=True)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def screenshot_dir(self) -> Path:
        d = self.run_dir / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_result(self, result: dict[str, Any]) -> None:
        """Publish the final result atomically.

        A reader can check ``result.json``'s existence to tell "finished"
        from "crashed or still running" without racing a partial write --
        the temp file is only ever visible under its final name after
        ``os.replace`` has made the whole write atomic (POSIX).
        """
        tmp = self.run_dir / "result.json.tmp"
        tmp.write_text(json.dumps(result, indent=2, sort_keys=True))
        os.replace(tmp, self.run_dir / "result.json")

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "EvidenceWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ==========================================================================
# Reader
# ==========================================================================


class EvidenceReader:
    """Reads a run directory back. Never raises on a partial or unknown record.

    A reader that raises on the first bad line defeats the purpose of a
    crash-safe writer: the whole point of fsync-per-record is that
    everything *before* the crash is trustworthy, and the reader has to
    actually honour that by skipping over whatever the crash left broken
    rather than giving up on the file.
    """

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)

    def read_events(self) -> list[Record]:
        path = self.run_dir / "run.jsonl"
        if not path.exists():
            return []

        records: list[Record] = []
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    # A truncated trailing line -- the writer was killed
                    # mid-write of this one. Everything before it is a
                    # complete, fsynced record; this line just isn't one.
                    continue
                cls = _KIND_TO_TYPE.get(raw.get("kind"))
                if cls is None:
                    # Unknown kind: a newer writer, an older reader. Skip
                    # rather than raise, so the reader never has to be
                    # upgraded in lockstep with the writer.
                    continue
                init_fields = {f.name for f in dataclasses.fields(cls) if f.init}
                kwargs = {k: v for k, v in raw.items() if k in init_fields}
                records.append(cls(**kwargs))
        return records

    def orphaned_step(self) -> int | None:
        """The step_index of a step_started with no matching step_finished.

        Append-only writes mean this can only ever be the last step_started
        in the file -- if the crash location. Returns None for a clean run.
        """
        started: set[int] = set()
        finished: set[int] = set()
        for record in self.read_events():
            if isinstance(record, StepStarted):
                started.add(record.step_index)
            elif isinstance(record, StepFinished):
                finished.add(record.step_index)
        orphans = started - finished
        return max(orphans) if orphans else None

    def read_result(self) -> dict[str, Any] | None:
        path = self.run_dir / "result.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def finished(self) -> bool:
        return (self.run_dir / "result.json").exists()
