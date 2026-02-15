"""Tests for structured trace export (OTel-compatible JSONL)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.stdlib.traces import (
    SESSION_INDEX_PATH,
    TRACES_OUTPUT_PATH,
    _iso_to_unix_nano,
    _session_to_span,
    _span_id,
    _trace_id,
    export_traces,
    format_traces_human,
)


def _write_index(repo: Path, entries: list[dict[str, Any]]) -> None:
    """Write session index entries for testing."""
    index_path = repo / SESSION_INDEX_PATH
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _sample_entry(**overrides: Any) -> dict[str, Any]:
    """Create a sample session index entry."""
    entry: dict[str, Any] = {
        "session_id": "SES-20260215-120000-ABCD1234",
        "actor": "agent:claude",
        "ticket_id": "TKT-20260215-110000-XYZ9",
        "vendor": "anthropic",
        "model": "claude-opus-4-6",
        "mode": "work",
        "started_at": "2026-02-15T12:00:00+00:00",
        "finished_at": "2026-02-15T12:30:00+00:00",
        "verify": "passed",
        "set_status": "review",
        "ticket_status": "review",
        "break_glass_reason": "",
        "drift_score": 0.15,
        "trace_passed": True,
        "trace_violations": 0,
        "artifact_count": 3,
        "error_count": 0,
        "git_branch": "main",
    }
    entry.update(overrides)
    return entry


# ── ID Generation ────────────────────────────────────────────────


class TestTraceIdGeneration:
    def test_trace_id_deterministic(self) -> None:
        a = _trace_id("SES-123")
        b = _trace_id("SES-123")
        assert a == b

    def test_trace_id_length(self) -> None:
        tid = _trace_id("SES-123")
        assert len(tid) == 32
        assert all(c in "0123456789abcdef" for c in tid)

    def test_trace_id_varies_by_session(self) -> None:
        a = _trace_id("SES-A")
        b = _trace_id("SES-B")
        assert a != b

    def test_span_id_deterministic(self) -> None:
        a = _span_id("SES-123")
        b = _span_id("SES-123")
        assert a == b

    def test_span_id_length(self) -> None:
        sid = _span_id("SES-123")
        assert len(sid) == 16
        assert all(c in "0123456789abcdef" for c in sid)

    def test_span_id_suffix_varies(self) -> None:
        a = _span_id("SES-123", "drift")
        b = _span_id("SES-123", "trace")
        assert a != b


# ── Timestamp Conversion ────────────────────────────────────────


class TestIsoToUnixNano:
    def test_converts_iso(self) -> None:
        nano = _iso_to_unix_nano("2026-02-15T12:00:00+00:00")
        assert nano > 0
        # Should be in nanoseconds (> 1e18 for 2026)
        assert nano > 1_000_000_000_000_000_000

    def test_empty_string_returns_zero(self) -> None:
        assert _iso_to_unix_nano("") == 0

    def test_deterministic(self) -> None:
        a = _iso_to_unix_nano("2026-02-15T12:00:00+00:00")
        b = _iso_to_unix_nano("2026-02-15T12:00:00+00:00")
        assert a == b

    def test_ordering_preserved(self) -> None:
        early = _iso_to_unix_nano("2026-02-15T12:00:00+00:00")
        late = _iso_to_unix_nano("2026-02-15T13:00:00+00:00")
        assert early < late


# ── Session-to-Span Conversion ──────────────────────────────────


class TestSessionToSpan:
    def test_basic_span_structure(self) -> None:
        entry = _sample_entry()
        span = _session_to_span(entry)
        assert "traceId" in span
        assert "spanId" in span
        assert "parentSpanId" in span
        assert span["parentSpanId"] == ""
        assert span["kind"] == "INTERNAL"
        assert span["name"] == "exo.session.work"

    def test_status_passed(self) -> None:
        span = _session_to_span(_sample_entry(verify="passed"))
        assert span["status"]["code"] == "OK"

    def test_status_failed(self) -> None:
        span = _session_to_span(_sample_entry(verify="failed"))
        assert span["status"]["code"] == "ERROR"

    def test_status_bypassed(self) -> None:
        span = _session_to_span(_sample_entry(verify="bypassed"))
        assert span["status"]["code"] == "UNSET"

    def test_break_glass_in_status_message(self) -> None:
        span = _session_to_span(_sample_entry(break_glass_reason="handoff"))
        assert span["status"]["message"] == "handoff"

    def test_attributes_populated(self) -> None:
        span = _session_to_span(_sample_entry())
        attrs = span["attributes"]
        assert attrs["exo.session_id"] == "SES-20260215-120000-ABCD1234"
        assert attrs["exo.ticket_id"] == "TKT-20260215-110000-XYZ9"
        assert attrs["exo.actor"] == "agent:claude"
        assert attrs["exo.vendor"] == "anthropic"
        assert attrs["exo.model"] == "claude-opus-4-6"
        assert attrs["exo.drift_score"] == 0.15

    def test_null_attributes_excluded(self) -> None:
        entry = _sample_entry()
        entry.pop("drift_score")
        span = _session_to_span(entry)
        assert "exo.drift_score" not in span["attributes"]

    def test_timestamps_nanosecond(self) -> None:
        span = _session_to_span(_sample_entry())
        assert span["startTimeUnixNano"] > 0
        assert span["endTimeUnixNano"] > span["startTimeUnixNano"]

    def test_audit_mode_name(self) -> None:
        span = _session_to_span(_sample_entry(mode="audit"))
        assert span["name"] == "exo.session.audit"

    def test_drift_event(self) -> None:
        span = _session_to_span(_sample_entry(drift_score=0.42))
        drift_events = [e for e in span["events"] if e["name"] == "drift_check"]
        assert len(drift_events) == 1
        assert drift_events[0]["attributes"]["exo.drift_score"] == 0.42

    def test_trace_event(self) -> None:
        span = _session_to_span(_sample_entry(trace_passed=True, trace_violations=3))
        trace_events = [e for e in span["events"] if e["name"] == "feature_trace"]
        assert len(trace_events) == 1
        assert trace_events[0]["attributes"]["exo.trace_passed"] is True
        assert trace_events[0]["attributes"]["exo.trace_violations"] == 3

    def test_no_events_when_missing(self) -> None:
        entry = _sample_entry()
        entry.pop("drift_score")
        entry.pop("trace_passed")
        span = _session_to_span(entry)
        assert span["events"] == []


# ── Export Traces ────────────────────────────────────────────────


class TestExportTraces:
    def test_empty_index(self, tmp_path: Path) -> None:
        result = export_traces(tmp_path)
        assert result["span_count"] == 0
        assert result["spans"] == []

    def test_no_index_file(self, tmp_path: Path) -> None:
        result = export_traces(tmp_path)
        assert result["span_count"] == 0
        assert result["output_path"] is None

    def test_exports_single_session(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        result = export_traces(tmp_path)
        assert result["span_count"] == 1
        assert result["spans"][0]["name"] == "exo.session.work"

    def test_exports_multiple_sessions(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="SES-A", started_at="2026-02-15T10:00:00+00:00"),
            _sample_entry(session_id="SES-B", started_at="2026-02-15T11:00:00+00:00"),
            _sample_entry(session_id="SES-C", started_at="2026-02-15T12:00:00+00:00"),
        ]
        _write_index(tmp_path, entries)
        result = export_traces(tmp_path)
        assert result["span_count"] == 3

    def test_sorted_by_start_time(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="SES-LATE", started_at="2026-02-15T15:00:00+00:00"),
            _sample_entry(session_id="SES-EARLY", started_at="2026-02-15T10:00:00+00:00"),
        ]
        _write_index(tmp_path, entries)
        result = export_traces(tmp_path)
        spans = result["spans"]
        assert spans[0]["attributes"]["exo.session_id"] == "SES-EARLY"
        assert spans[1]["attributes"]["exo.session_id"] == "SES-LATE"

    def test_since_filter(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="SES-OLD", started_at="2026-02-14T10:00:00+00:00"),
            _sample_entry(session_id="SES-NEW", started_at="2026-02-16T10:00:00+00:00"),
        ]
        _write_index(tmp_path, entries)
        result = export_traces(tmp_path, since="2026-02-15T00:00:00+00:00")
        assert result["span_count"] == 1
        assert result["spans"][0]["attributes"]["exo.session_id"] == "SES-NEW"

    def test_writes_jsonl_file(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        result = export_traces(tmp_path, write=True)
        assert result["output_path"] is not None
        out_path = tmp_path / TRACES_OUTPUT_PATH
        assert out_path.exists()
        lines = out_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        span = json.loads(lines[0])
        assert span["kind"] == "INTERNAL"

    def test_no_write_mode(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        result = export_traces(tmp_path, write=False)
        assert result["output_path"] is None
        assert result["span_count"] == 1
        out_path = tmp_path / TRACES_OUTPUT_PATH
        assert not out_path.exists()

    def test_jsonl_round_trip(self, tmp_path: Path) -> None:
        """Each line in JSONL output is valid JSON with OTel fields."""
        entries = [
            _sample_entry(session_id="SES-A"),
            _sample_entry(session_id="SES-B"),
        ]
        _write_index(tmp_path, entries)
        export_traces(tmp_path, write=True)
        out_path = tmp_path / TRACES_OUTPUT_PATH
        lines = out_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            span = json.loads(line)
            assert len(span["traceId"]) == 32
            assert len(span["spanId"]) == 16
            assert span["kind"] == "INTERNAL"
            assert "code" in span["status"]

    def test_skips_malformed_index_lines(self, tmp_path: Path) -> None:
        index_path = tmp_path / SESSION_INDEX_PATH
        index_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(_sample_entry()) + "\n" + "not-json\n"
        index_path.write_text(content, encoding="utf-8")
        result = export_traces(tmp_path)
        assert result["span_count"] == 1


# ── Human Formatting ────────────────────────────────────────────


class TestFormatTracesHuman:
    def test_shows_span_count(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        result = export_traces(tmp_path, write=False)
        human = format_traces_human(result)
        assert "1 span(s)" in human

    def test_shows_session_id(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        result = export_traces(tmp_path, write=False)
        human = format_traces_human(result)
        assert "SES-20260215-120000-ABCD1234" in human

    def test_shows_events(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(drift_score=0.3)])
        result = export_traces(tmp_path, write=False)
        human = format_traces_human(result)
        assert "drift_check" in human

    def test_shows_since(self) -> None:
        result = {"spans": [], "span_count": 0, "since": "2026-02-15T00:00:00+00:00", "output_path": None}
        human = format_traces_human(result)
        assert "Since:" in human

    def test_shows_output_path(self) -> None:
        result = {"spans": [], "span_count": 0, "since": None, "output_path": ".exo/logs/traces.jsonl"}
        human = format_traces_human(result)
        assert "traces.jsonl" in human
