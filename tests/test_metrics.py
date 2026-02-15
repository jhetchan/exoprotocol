"""Tests for governance metrics API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from exo.stdlib.metrics import SESSION_INDEX_PATH, compute_metrics, format_metrics_human


def _write_index(repo: Path, entries: list[dict[str, Any]]) -> None:
    index_path = repo / SESSION_INDEX_PATH
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _sample_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "session_id": "SES-001",
        "actor": "agent:claude",
        "ticket_id": "TKT-001",
        "mode": "work",
        "verify": "passed",
        "drift_score": 0.2,
    }
    entry.update(overrides)
    return entry


# ── compute_metrics() ────────────────────────────────────────────


class TestComputeMetrics:
    def test_empty_repo(self, tmp_path: Path) -> None:
        data = compute_metrics(tmp_path)
        assert data["session_count"] == 0
        assert data["verify_pass_rate"] == 0.0
        assert data["avg_drift_score"] == 0.0

    def test_session_count(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(), _sample_entry(session_id="SES-2")])
        data = compute_metrics(tmp_path)
        assert data["session_count"] == 2

    def test_verify_stats(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="S1", verify="passed"),
            _sample_entry(session_id="S2", verify="passed"),
            _sample_entry(session_id="S3", verify="failed"),
            _sample_entry(session_id="S4", verify="bypassed"),
        ]
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["verify_passed"] == 2
        assert data["verify_failed"] == 1
        assert data["verify_bypassed"] == 1
        assert data["verify_pass_rate"] == 0.5

    def test_drift_distribution(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="S1", drift_score=0.1),
            _sample_entry(session_id="S2", drift_score=0.5),
            _sample_entry(session_id="S3", drift_score=0.8),
            _sample_entry(session_id="S4", drift_score=0.2),
        ]
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["drift_distribution"]["low"] == 2
        assert data["drift_distribution"]["medium"] == 1
        assert data["drift_distribution"]["high"] == 1
        assert data["avg_drift_score"] == 0.4
        assert data["max_drift_score"] == 0.8

    def test_tickets_touched(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="S1", ticket_id="TKT-A"),
            _sample_entry(session_id="S2", ticket_id="TKT-B"),
            _sample_entry(session_id="S3", ticket_id="TKT-A"),
        ]
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["tickets_touched"] == 2

    def test_actor_breakdown(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="S1", actor="agent:claude"),
            _sample_entry(session_id="S2", actor="agent:cursor"),
            _sample_entry(session_id="S3", actor="agent:claude"),
        ]
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["actor_count"] == 2
        actors_by_name = {a["actor"]: a["session_count"] for a in data["actors"]}
        assert actors_by_name["agent:claude"] == 2
        assert actors_by_name["agent:cursor"] == 1

    def test_mode_counts(self, tmp_path: Path) -> None:
        entries = [
            _sample_entry(session_id="S1", mode="work"),
            _sample_entry(session_id="S2", mode="audit"),
            _sample_entry(session_id="S3", mode="work"),
        ]
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["mode_counts"]["work"] == 2
        assert data["mode_counts"]["audit"] == 1

    def test_computed_at_present(self, tmp_path: Path) -> None:
        data = compute_metrics(tmp_path)
        assert "computed_at" in data

    def test_no_drift_scores(self, tmp_path: Path) -> None:
        entries = [_sample_entry(session_id="S1")]
        entries[0].pop("drift_score")
        _write_index(tmp_path, entries)
        data = compute_metrics(tmp_path)
        assert data["avg_drift_score"] == 0.0
        assert data["max_drift_score"] == 0.0


# ── format_metrics_human() ──────────────────────────────────────


class TestFormatMetricsHuman:
    def test_shows_session_count(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry()])
        data = compute_metrics(tmp_path)
        human = format_metrics_human(data)
        assert "1 session(s)" in human

    def test_shows_pass_rate(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(verify="passed")])
        data = compute_metrics(tmp_path)
        human = format_metrics_human(data)
        assert "100.0%" in human

    def test_shows_drift_stats(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(drift_score=0.35)])
        data = compute_metrics(tmp_path)
        human = format_metrics_human(data)
        assert "0.350" in human

    def test_shows_actors(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(actor="agent:test")])
        data = compute_metrics(tmp_path)
        human = format_metrics_human(data)
        assert "agent:test" in human

    def test_shows_mode_counts(self, tmp_path: Path) -> None:
        _write_index(tmp_path, [_sample_entry(mode="audit")])
        data = compute_metrics(tmp_path)
        human = format_metrics_human(data)
        assert "audit=1" in human
