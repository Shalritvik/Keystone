"""Tests for keystone/evidence.py.

Priorities per CLAUDE.md: redaction happens at construction time, and the
reader survives exactly the kind of file a killed writer leaves behind.
"""

from __future__ import annotations

import json

from keystone.evidence import (
    REDACTED,
    EvidenceReader,
    EvidenceWriter,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
)


def test_redaction_happens_at_construction_not_serialisation():
    """The value must already be gone the instant the object exists --

    before write() is ever called, before json.dumps runs. That is the
    whole point of doing it in __post_init__ rather than on the way out.
    """
    step = StepStarted(
        run_id="r1", step_index=0, action="type", target='textbox "PIN:"',
        value="1234", sensitive=True,
    )
    assert step.value == REDACTED

    finished = StepFinished(
        run_id="r1", step_index=0, status="ok", observed="1234", sensitive=True,
    )
    assert finished.observed == REDACTED

    run = RunStarted(
        run_id="r1", capability_id="cap", entry_url="http://x/",
        params={"member_id": "12345", "pin": "9999"},
        sensitive_params=frozenset({"pin"}),
    )
    assert run.params == {"member_id": "12345", "pin": REDACTED}


def test_non_sensitive_values_pass_through_untouched():
    step = StepStarted(run_id="r1", step_index=0, action="type", value="12345")
    assert step.value == "12345"


def test_writer_produces_records_every_field_intact(tmp_path):
    run_dir = tmp_path / "run-1"
    writer = EvidenceWriter(run_dir)
    try:
        writer.write(RunStarted(run_id="run-1", capability_id="cap", entry_url="http://x/"))
        writer.write(StepStarted(run_id="run-1", step_index=0, action="navigate"))
        writer.write(StepFinished(run_id="run-1", step_index=0, status="ok"))
    finally:
        writer.close()

    reader = EvidenceReader(run_dir)
    events = reader.read_events()
    assert [e.kind for e in events] == ["run_started", "step_started", "step_finished"]
    assert reader.orphaned_step() is None


def test_reader_tolerates_truncated_trailing_line_and_returns_orphaned_step(tmp_path):
    """Simulates a writer killed mid-run: two complete steps, a third

    step_started with no matching step_finished (the crash point), and a
    trailing line that was cut off mid-write (a literal truncated JSON
    fragment, as os-buffered writes can leave behind). The reader must
    return every complete record, skip the garbage silently, and let the
    caller identify exactly where the crash happened.
    """
    run_dir = tmp_path / "run-crash"
    writer = EvidenceWriter(run_dir)
    writer.write(RunStarted(run_id="run-crash", capability_id="cap", entry_url="http://x/"))
    writer.write(StepStarted(run_id="run-crash", step_index=0, action="navigate"))
    writer.write(StepFinished(run_id="run-crash", step_index=0, status="ok"))
    writer.write(StepStarted(run_id="run-crash", step_index=1, action="click", target='button "Search"'))
    # No matching step_finished for step 1 -- the process "dies" here.
    writer.close()

    # Simulate the OS cutting a write off mid-line, appended directly (the
    # writer is gone; this stands in for whatever the kernel actually
    # flushed to disk before the process was killed).
    with (run_dir / "run.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "step_finished", "run_id": "run-crash", "step_i')

    reader = EvidenceReader(run_dir)
    events = reader.read_events()

    assert [(e.kind, getattr(e, "step_index", None)) for e in events] == [
        ("run_started", None),
        ("step_started", 0),
        ("step_finished", 0),
        ("step_started", 1),
    ]
    assert reader.orphaned_step() == 1
    assert reader.finished() is False


def test_reader_skips_unknown_kind_without_raising(tmp_path):
    run_dir = tmp_path / "run-future"
    run_dir.mkdir()
    with (run_dir / "run.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "step_started", "schema_version": "1.0",
                              "run_id": "r", "step_index": 0, "action": "navigate"}) + "\n")
        fh.write(json.dumps({"kind": "some_future_kind", "schema_version": "9.9",
                              "run_id": "r", "anything": "goes"}) + "\n")
        fh.write(json.dumps({"kind": "step_finished", "schema_version": "1.0",
                              "run_id": "r", "step_index": 0, "status": "ok"}) + "\n")

    reader = EvidenceReader(run_dir)
    events = reader.read_events()
    assert [e.kind for e in events] == ["step_started", "step_finished"]


def test_result_json_is_atomic_and_marks_the_run_finished(tmp_path):
    run_dir = tmp_path / "run-2"
    writer = EvidenceWriter(run_dir)
    reader = EvidenceReader(run_dir)

    assert reader.finished() is False
    assert reader.read_result() is None

    writer.write_result({"status": "success", "capability_id": "cap"})
    writer.close()

    assert reader.finished() is True
    assert reader.read_result() == {"status": "success", "capability_id": "cap"}
    assert not (run_dir / "result.json.tmp").exists()


def test_screenshot_dir_created_on_demand(tmp_path):
    run_dir = tmp_path / "run-3"
    writer = EvidenceWriter(run_dir)
    d = writer.screenshot_dir()
    assert d.exists()
    assert d == run_dir / "screenshots"
    writer.close()


def test_run_finished_record_roundtrips(tmp_path):
    run_dir = tmp_path / "run-4"
    writer = EvidenceWriter(run_dir)
    writer.write(RunFinished(run_id="run-4", status="success", duration_ms=1200))
    writer.close()

    reader = EvidenceReader(run_dir)
    events = reader.read_events()
    assert len(events) == 1
    assert events[0].status == "success"
    assert events[0].duration_ms == 1200
