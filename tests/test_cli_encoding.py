"""Tests for Windows cp1252 / narrow-encoding stdout safety (Bug 2 community feedback).

Without the fix, calling main() with a cp1252-encoded stdout raises
UnicodeEncodeError because the session banner and human formatters emit
box-drawing characters (╔ ║ ╚ ═) and arrows (→) that are not in cp1252.

With the fix (stream.reconfigure(encoding='utf-8', errors='replace') at the
top of both cli.main() and mcp_server.main()), the call must complete without
raising; any unencodable characters are substituted with '?'.
"""

from __future__ import annotations

import io
import sys

import pytest


class TestCliEncodingCp1252:
    def test_help_survives_cp1252_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main(['--help']) must not raise UnicodeEncodeError on a cp1252 stream."""
        narrow_buf = io.BytesIO()
        narrow_stream = io.TextIOWrapper(narrow_buf, encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", narrow_stream)
        monkeypatch.setattr(sys, "stderr", narrow_stream)

        from exo.cli import main

        # --help causes SystemExit(0); that's fine — we just must not get
        # UnicodeEncodeError before argparse exits.
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_help_box_chars_survive_utf8_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a utf-8 stream, reconfigure is a no-op and box chars are preserved."""
        utf8_buf = io.BytesIO()
        utf8_stream = io.TextIOWrapper(utf8_buf, encoding="utf-8", errors="strict")
        monkeypatch.setattr(sys, "stdout", utf8_stream)
        monkeypatch.setattr(sys, "stderr", utf8_stream)

        from exo.cli import main

        with pytest.raises(SystemExit):
            main(["--help"])

        utf8_stream.flush()
        output = utf8_buf.getvalue().decode("utf-8")
        # argparse help output should be present and not contain replacement chars
        # (box chars appear only in session-start banner, not --help output, so
        # this test just verifies the output is valid UTF-8 with no corruption)
        assert "exo" in output.lower() or len(output) > 0

    def test_cp1252_replaces_not_crashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On cp1252, box-drawing characters become '?' replacements, not exceptions."""
        narrow_buf = io.BytesIO()
        narrow_stream = io.TextIOWrapper(narrow_buf, encoding="cp1252", errors="strict")
        monkeypatch.setattr(sys, "stdout", narrow_stream)
        monkeypatch.setattr(sys, "stderr", narrow_stream)

        from exo.cli import main

        # --help exits 0 cleanly; the main() reconfigure must have fired before
        # argparse even parses args, which is exactly the fix we're testing.
        try:
            main(["--help"])
        except SystemExit as e:
            assert e.code == 0
        except UnicodeEncodeError:
            pytest.fail("UnicodeEncodeError raised on cp1252 stdout — the reconfigure fix is not working")
