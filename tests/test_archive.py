"""Tests for exo archive (closes feedback #7b/c).

Covers:
- archive_paths moves files and directories
- archive/INDEX.md creation and append
- Refusal of forbidden paths (.git/, .exo/, archive/, missing)
- Reason required, --dry-run preview
- Scanner adds RULE-ARC-001 when archive/ exists
- CLI: exo archive
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exo.stdlib.archive import (
    ArchiveResult,
    archive_paths,
    archive_to_dict,
    format_archive_human,
)


class TestArchivePathsBasic:
    def test_moves_file_into_archive(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "old_module.py").write_text("# stale\n", encoding="utf-8")
        result = archive_paths(repo, ["old_module.py"], reason="superseded by new_module")
        assert len(result.moved) == 1
        assert result.moved[0].source == "old_module.py"
        assert result.moved[0].destination == "archive/old_module.py"
        assert not (repo / "old_module.py").exists()
        assert (repo / "archive" / "old_module.py").exists()
        assert result.index_appended is True

    def test_moves_directory_into_archive(self, tmp_path: Path) -> None:
        repo = tmp_path
        old_pkg = repo / "old_pkg"
        old_pkg.mkdir()
        (old_pkg / "__init__.py").write_text("", encoding="utf-8")
        (old_pkg / "thing.py").write_text("x\n", encoding="utf-8")
        result = archive_paths(repo, ["old_pkg"], reason="renamed to new_pkg")
        assert len(result.moved) == 1
        assert (repo / "archive" / "old_pkg" / "thing.py").exists()
        assert not old_pkg.exists()

    def test_index_md_created(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "old.txt").write_text("x", encoding="utf-8")
        archive_paths(repo, ["old.txt"], reason="dead code")
        index = (repo / "archive" / "INDEX.md").read_text(encoding="utf-8")
        assert "Archive Index" in index
        assert "old.txt" in index
        assert "archive/old.txt" in index
        assert "dead code" in index
        assert "git mv" in index  # restoration hint

    def test_index_appends_not_overwrites(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "first.txt").write_text("x", encoding="utf-8")
        archive_paths(repo, ["first.txt"], reason="first reason")
        (repo / "second.txt").write_text("y", encoding="utf-8")
        archive_paths(repo, ["second.txt"], reason="second reason")
        index = (repo / "archive" / "INDEX.md").read_text(encoding="utf-8")
        assert "first.txt" in index
        assert "second.txt" in index
        assert "first reason" in index
        assert "second reason" in index


class TestArchiveRefusals:
    def test_empty_reason_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path
        (repo / "x.py").write_text("", encoding="utf-8")
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, ["x.py"], reason="")
        assert exc_info.value.code == "ARCHIVE_REASON_REQUIRED"

    def test_no_paths_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        with pytest.raises(ExoError) as exc_info:
            archive_paths(tmp_path, [], reason="cleanup")
        assert exc_info.value.code == "ARCHIVE_NO_PATHS"

    def test_missing_path_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        with pytest.raises(ExoError) as exc_info:
            archive_paths(tmp_path, ["does_not_exist.py"], reason="cleanup")
        assert exc_info.value.code == "ARCHIVE_PATH_NOT_FOUND"

    def test_git_path_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path
        (repo / ".git").mkdir()
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, [".git"], reason="x")
        assert exc_info.value.code == "ARCHIVE_PATH_FORBIDDEN"

    def test_exo_path_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path
        (repo / ".exo").mkdir()
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, [".exo"], reason="x")
        assert exc_info.value.code == "ARCHIVE_PATH_FORBIDDEN"

    def test_archive_path_itself_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path
        (repo / "archive").mkdir()
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, ["archive"], reason="x")
        assert exc_info.value.code == "ARCHIVE_PATH_FORBIDDEN"

    def test_outside_repo_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, ["../escape.py"], reason="x")
        assert exc_info.value.code == "ARCHIVE_PATH_OUTSIDE_REPO"

    def test_dest_collision_refused(self, tmp_path: Path) -> None:
        from exo.kernel.errors import ExoError

        repo = tmp_path
        (repo / "old.txt").write_text("a", encoding="utf-8")
        archive_paths(repo, ["old.txt"], reason="first")
        (repo / "old.txt").write_text("b", encoding="utf-8")
        with pytest.raises(ExoError) as exc_info:
            archive_paths(repo, ["old.txt"], reason="second")
        assert exc_info.value.code == "ARCHIVE_DEST_BUSY"


class TestArchiveDryRun:
    def test_dry_run_does_not_move(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "old.txt").write_text("x", encoding="utf-8")
        result = archive_paths(repo, ["old.txt"], reason="preview only", dry_run=True)
        assert (repo / "old.txt").exists()
        assert not (repo / "archive" / "old.txt").exists()
        # Index path is reported but not appended
        assert result.index_appended is False
        # Plan still records what would have moved
        assert len(result.moved) == 1
        assert result.moved[0].destination == "archive/old.txt"


class TestArchiveScannerRule:
    """Scanner generates RULE-ARC-001 when archive/ exists (closes feedback #7c)."""

    def test_constitution_includes_rule_when_archive_present(self, tmp_path: Path) -> None:
        from exo.stdlib.scan import generate_constitution, scan_repo

        (tmp_path / "archive").mkdir()
        report = scan_repo(tmp_path)
        constitution = generate_constitution(report)
        assert "RULE-ARC-001" in constitution
        assert "archive/**" in constitution

    def test_constitution_omits_rule_without_archive(self, tmp_path: Path) -> None:
        from exo.stdlib.scan import generate_constitution, scan_repo

        report = scan_repo(tmp_path)
        constitution = generate_constitution(report)
        assert "RULE-ARC-001" not in constitution


class TestArchiveSerialization:
    def test_to_dict(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "x.py").write_text("", encoding="utf-8")
        result = archive_paths(repo, ["x.py"], reason="test")
        d = archive_to_dict(result)
        assert d["count"] == 1
        assert d["moved"][0]["source"] == "x.py"
        assert d["index_path"]
        assert d["index_appended"] is True
        json.dumps(d, ensure_ascii=True)

    def test_format_human_no_moves(self) -> None:
        result = ArchiveResult()
        text = format_archive_human(result)
        assert "no paths moved" in text

    def test_format_human_with_moves(self, tmp_path: Path) -> None:
        repo = tmp_path
        (repo / "x.py").write_text("", encoding="utf-8")
        result = archive_paths(repo, ["x.py"], reason="t")
        text = format_archive_human(result)
        assert "x.py" in text
        assert "archive/x.py" in text


class TestArchiveCLI:
    def test_cli_archives_via_subcommand(self, tmp_path: Path, monkeypatch) -> None:
        from exo.cli import main as cli_main_local

        repo = tmp_path
        (repo / "old.py").write_text("x\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        rc = cli_main_local(["archive", "old.py", "--reason", "deprecated by issue #42"])
        assert rc == 0
        assert (repo / "archive" / "old.py").exists()
        assert "deprecated by issue #42" in (repo / "archive" / "INDEX.md").read_text(encoding="utf-8")
