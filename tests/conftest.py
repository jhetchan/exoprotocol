"""Session-scoped pytest fixture: disable git commit signing for all tests.

The CI environment configures commit.gpgsign=true with a remote signing
service that requires an authenticated source.  Test repos do not need
signed commits, so this fixture points GIT_CONFIG_GLOBAL at a minimal
config that disables signing for every git subprocess spawned during the
test run.  Without this, any test that creates a git repo and commits
files will silently produce 0 commits and fail with assertion errors.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_git_signing(tmp_path_factory: pytest.TempPathFactory) -> None:
    cfg_dir = tmp_path_factory.mktemp("git-global-cfg")
    cfg_file = cfg_dir / "gitconfig"
    cfg_file.write_text(
        "[commit]\n\tgpgsign = false\n[user]\n\tname = Test\n\temail = test@exo.test\n",
        encoding="utf-8",
    )
    original = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg_file)
    yield
    if original is None:
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
    else:
        os.environ["GIT_CONFIG_GLOBAL"] = original
