from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from exo.kernel.errors import ExoError
from exo.stdlib.distributed_leases import GitDistributedLeaseManager

_GIT_TEST_ENV = {
    "GIT_AUTHOR_NAME": "ExoProtocol",
    "GIT_AUTHOR_EMAIL": "exo@local.invalid",
    "GIT_COMMITTER_NAME": "ExoProtocol",
    "GIT_COMMITTER_EMAIL": "exo@local.invalid",
}


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(_GIT_TEST_ENV)
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            "git command failed\n"
            f"command: git {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\n"
        )
    return proc


def _init_remote_pair(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))

    seed = tmp_path / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    init_main = _git(seed, "init", "-b", "main", check=False)
    if init_main.returncode != 0:
        _git(seed, "init")
        _git(seed, "symbolic-ref", "HEAD", "refs/heads/main")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    clone_a = tmp_path / "agent-a"
    clone_b = tmp_path / "agent-b"
    _git(tmp_path, "clone", str(remote), str(clone_a))
    _git(tmp_path, "clone", str(remote), str(clone_b))
    return clone_a, clone_b


def test_distributed_claim_conflict_between_two_clones(tmp_path: Path) -> None:
    clone_a, clone_b = _init_remote_pair(tmp_path)
    manager_a = GitDistributedLeaseManager(clone_a)
    manager_b = GitDistributedLeaseManager(clone_b)

    first = manager_a.claim("TICKET-700", owner="agent:a", role="developer", duration_hours=1, remote="origin")
    assert first["ticket_id"] == "TICKET-700"
    assert first["distributed"]["mode"] == "claim"
    assert int(first["lock"]["fencing_token"]) == 1

    with pytest.raises(ExoError) as collision_err:
        manager_b.claim("TICKET-700", owner="agent:b", role="developer", duration_hours=1, remote="origin")
    assert collision_err.value.code == "DISTRIBUTED_LOCK_COLLISION"


def test_distributed_renew_heartbeat_and_release_flow(tmp_path: Path) -> None:
    clone_a, clone_b = _init_remote_pair(tmp_path)
    manager_a = GitDistributedLeaseManager(clone_a)
    manager_b = GitDistributedLeaseManager(clone_b)

    first = manager_a.claim("TICKET-701", owner="agent:a", role="developer", duration_hours=1, remote="origin")
    renewed = manager_a.renew("TICKET-701", owner="agent:a", role="developer", duration_hours=2, remote="origin")
    assert renewed["distributed"]["mode"] == "renew"
    assert int(renewed["lock"]["fencing_token"]) == int(first["lock"]["fencing_token"]) + 1

    heartbeat = manager_a.heartbeat("TICKET-701", owner="agent:a", duration_hours=2, remote="origin")
    assert heartbeat["distributed"]["mode"] == "heartbeat"
    assert int(heartbeat["lock"]["fencing_token"]) == int(renewed["lock"]["fencing_token"])

    released = manager_a.release("TICKET-701", owner="agent:a", remote="origin")
    assert released["ticket_id"] == "TICKET-701"
    assert released["distributed"]["remote_released"] is True

    takeover = manager_b.claim("TICKET-701", owner="agent:b", role="developer", duration_hours=1, remote="origin")
    assert takeover["ticket_id"] == "TICKET-701"
    assert takeover["lock"]["owner"] == "agent:b"
