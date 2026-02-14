from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError


_GIT_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "ExoProtocol",
    "GIT_AUTHOR_EMAIL": "exo@local.invalid",
    "GIT_COMMITTER_NAME": "ExoProtocol",
    "GIT_COMMITTER_EMAIL": "exo@local.invalid",
}


def _run_git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    stdin: str | None = None,
    error_code: str = "GIT_COMMAND_FAILED",
    message: str = "Git command failed",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=(cwd or repo),
        capture_output=True,
        text=True,
        input=stdin,
        env=run_env,
    )
    if check and proc.returncode != 0:
        raise ExoError(
            code=error_code,
            message=message,
            details={
                "command": " ".join(cmd),
                "cwd": str((cwd or repo).resolve()),
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-1200:],
                "stderr": (proc.stderr or "")[-1200:],
            },
            blocked=True,
        )
    return proc


def _is_git_repo(repo: Path) -> bool:
    proc = _run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _ensure_git_repo(repo: Path, *, init_if_missing: bool, default_branch: str) -> bool:
    if _is_git_repo(repo):
        return False

    if not init_if_missing:
        raise ExoError(
            code="GIT_REQUIRED",
            message="Repository is not initialized. Run `git init` or use sidecar-init with git bootstrap enabled.",
            blocked=True,
        )

    init_branch = default_branch.strip() or "main"
    init = _run_git(repo, ["init", "-b", init_branch], check=False)
    if init.returncode != 0:
        _run_git(
            repo,
            ["init"],
            error_code="GIT_INIT_FAILED",
            message="Failed to initialize git repository",
        )
        _run_git(repo, ["symbolic-ref", "HEAD", f"refs/heads/{init_branch}"], check=False)
    return True


def _normalize_sidecar_path(repo: Path, sidecar: str) -> tuple[Path, str]:
    raw = sidecar.strip() if isinstance(sidecar, str) else ""
    if not raw:
        raise ExoError(code="SIDECAR_PATH_INVALID", message="sidecar path is required", blocked=True)

    candidate = Path(raw)
    if candidate.is_absolute():
        raise ExoError(
            code="SIDECAR_PATH_INVALID",
            message="sidecar path must be relative to repository root",
            blocked=True,
        )
    absolute = (repo / candidate).resolve()
    if absolute == repo or not absolute.is_relative_to(repo):
        raise ExoError(
            code="SIDECAR_PATH_INVALID",
            message=f"sidecar path must stay under repository root: {raw}",
            blocked=True,
        )

    rel = candidate.as_posix().rstrip("/")
    if rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        raise ExoError(
            code="SIDECAR_PATH_INVALID", message="sidecar path must not resolve to repository root", blocked=True
        )
    return absolute, rel


def _ensure_gitignore_entry(repo: Path, sidecar_rel: str) -> bool:
    path = repo / ".gitignore"
    entry = f"{sidecar_rel.rstrip('/')}/"

    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    if entry in lines:
        return False

    with path.open("a", encoding="utf-8") as handle:
        if lines and lines[-1].strip():
            handle.write("\n")
        handle.write(entry + "\n")
    return True


def _local_branch_exists(repo: Path, branch: str) -> bool:
    proc = _run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    return proc.returncode == 0


def _remote_exists(repo: Path, remote: str) -> bool:
    proc = _run_git(repo, ["remote", "get-url", remote], check=False)
    return proc.returncode == 0


def _remote_branch_exists(repo: Path, remote: str, branch: str) -> bool:
    proc = _run_git(repo, ["ls-remote", "--exit-code", "--heads", remote, branch], check=False)
    return proc.returncode == 0


def _create_orphan_branch(repo: Path, branch: str) -> str:
    tree = _run_git(
        repo,
        ["hash-object", "-t", "tree", "/dev/null"],
        error_code="GOVERNANCE_BRANCH_INIT_FAILED",
        message=f"Failed to create empty tree for branch {branch}",
    ).stdout.strip()
    if not tree:
        raise ExoError(
            code="GOVERNANCE_BRANCH_INIT_FAILED",
            message=f"Failed to compute empty tree hash for branch {branch}",
            blocked=True,
        )

    commit = _run_git(
        repo,
        ["commit-tree", tree],
        stdin=f"chore(exo): initialize {branch}\n",
        env=_GIT_AUTHOR_ENV,
        error_code="GOVERNANCE_BRANCH_INIT_FAILED",
        message=f"Failed to create orphan commit for branch {branch}",
    ).stdout.strip()
    if not commit:
        raise ExoError(
            code="GOVERNANCE_BRANCH_INIT_FAILED",
            message=f"Failed to create orphan commit for branch {branch}",
            blocked=True,
        )

    _run_git(
        repo,
        ["update-ref", f"refs/heads/{branch}", commit],
        error_code="GOVERNANCE_BRANCH_INIT_FAILED",
        message=f"Failed to update branch ref for {branch}",
    )
    return commit


def _ensure_governance_branch(
    repo: Path,
    *,
    branch: str,
    remote: str,
    fetch_remote: bool,
) -> dict[str, Any]:
    if _local_branch_exists(repo, branch):
        return {"branch_created": False, "branch_source": "local", "fetched_from_remote": False, "orphan_commit": None}

    if fetch_remote and _remote_exists(repo, remote) and _remote_branch_exists(repo, remote, branch):
        _run_git(
            repo,
            ["fetch", remote, f"{branch}:{branch}"],
            error_code="GOVERNANCE_BRANCH_FETCH_FAILED",
            message=f"Failed to fetch branch {remote}/{branch}",
        )
        return {
            "branch_created": False,
            "branch_source": f"{remote}/{branch}",
            "fetched_from_remote": True,
            "orphan_commit": None,
        }

    orphan_commit = _create_orphan_branch(repo, branch)
    return {
        "branch_created": True,
        "branch_source": "orphan",
        "fetched_from_remote": False,
        "orphan_commit": orphan_commit,
    }


def _existing_worktree_branch(path: Path) -> str | None:
    if not path.exists() or not path.is_dir():
        return None
    proc = _run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False, cwd=path)
    if proc.returncode != 0:
        return None
    top = _run_git(path, ["rev-parse", "--show-toplevel"], check=False, cwd=path)
    if top.returncode != 0:
        return None
    top_path = Path(top.stdout.strip()).resolve() if top.stdout.strip() else None
    if top_path is None or top_path != path.resolve():
        # Directory is inside a worktree, but not itself the worktree root.
        return None
    branch = _run_git(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False, cwd=path)
    text = branch.stdout.strip()
    if branch.returncode != 0 or not text:
        return None
    return text


def _copy_tree_contents(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name == ".git":
            continue
        target = dst / item.name
        if item.is_symlink():
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            os.symlink(os.readlink(item), target)
            continue

        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _commit_if_needed(sidecar_root: Path, *, commit_changes: bool, message: str) -> dict[str, Any]:
    _run_git(sidecar_root, ["add", "-A"], cwd=sidecar_root)
    staged = _run_git(sidecar_root, ["diff", "--cached", "--quiet"], check=False, cwd=sidecar_root)
    if staged.returncode == 0:
        return {"staged": False, "committed": False, "commit": None}

    if not commit_changes:
        return {"staged": True, "committed": False, "commit": None}

    _run_git(
        sidecar_root,
        ["commit", "-m", message],
        cwd=sidecar_root,
        env=_GIT_AUTHOR_ENV,
        error_code="SIDECAR_COMMIT_FAILED",
        message="Failed to commit migrated sidecar content",
    )
    commit = _run_git(sidecar_root, ["rev-parse", "HEAD"], cwd=sidecar_root).stdout.strip()
    return {"staged": True, "committed": True, "commit": commit or None}


def init_sidecar_worktree(
    root: Path | str,
    *,
    branch: str = "exo-governance",
    sidecar: str = ".exo",
    remote: str = "origin",
    init_git: bool = True,
    default_branch: str = "main",
    fetch_remote: bool = True,
    commit_migration: bool = True,
) -> dict[str, Any]:
    repo = Path(root).resolve()
    if not repo.exists() or not repo.is_dir():
        raise ExoError(code="REPO_NOT_FOUND", message=f"Repository path not found: {repo}", blocked=True)

    normalized_branch = branch.strip()
    if not normalized_branch:
        raise ExoError(code="GOVERNANCE_BRANCH_INVALID", message="governance branch is required", blocked=True)

    sidecar_path, sidecar_rel = _normalize_sidecar_path(repo, sidecar)
    git_repo_created = _ensure_git_repo(repo, init_if_missing=init_git, default_branch=default_branch)
    gitignore_added = _ensure_gitignore_entry(repo, sidecar_rel)
    branch_meta = _ensure_governance_branch(
        repo,
        branch=normalized_branch,
        remote=remote.strip() or "origin",
        fetch_remote=fetch_remote,
    )

    existing_branch = _existing_worktree_branch(sidecar_path)
    if existing_branch and existing_branch != normalized_branch:
        raise ExoError(
            code="SIDECAR_BRANCH_MISMATCH",
            message=(
                f"Sidecar path {sidecar_rel} already points to branch {existing_branch}; expected {normalized_branch}"
            ),
            blocked=True,
        )

    migrated = False
    backup_path: Path | None = None
    worktree_added = False
    commit_result = {"staged": False, "committed": False, "commit": None}

    if not existing_branch:
        if sidecar_path.exists():
            if not sidecar_path.is_dir():
                raise ExoError(
                    code="SIDECAR_PATH_INVALID",
                    message=f"Sidecar path exists and is not a directory: {sidecar_rel}",
                    blocked=True,
                )
            backup_path = (
                repo / f"{sidecar_rel.replace('/', '_')}.pre-sidecar-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            shutil.move(str(sidecar_path), str(backup_path))
            migrated = True

        _run_git(
            repo,
            ["worktree", "add", sidecar_path.as_posix(), normalized_branch],
            error_code="SIDECAR_WORKTREE_ADD_FAILED",
            message=f"Failed to mount sidecar worktree {sidecar_rel} on {normalized_branch}",
        )
        worktree_added = True

        if backup_path and backup_path.exists():
            _copy_tree_contents(backup_path, sidecar_path)
            shutil.rmtree(backup_path)
            commit_result = _commit_if_needed(
                sidecar_path,
                commit_changes=commit_migration,
                message="chore(exo): migrate existing sidecar state",
            )

    current_branch = ""
    branch_proc = _run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    if branch_proc.returncode == 0:
        current_branch = branch_proc.stdout.strip()
    if not current_branch:
        fallback_proc = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if fallback_proc.returncode == 0:
            current_branch = fallback_proc.stdout.strip()
    if not current_branch:
        current_branch = default_branch.strip() or "main"

    return {
        "repo": repo.as_posix(),
        "current_branch": current_branch,
        "governance_branch": normalized_branch,
        "sidecar_path": sidecar_path.as_posix(),
        "sidecar_rel": sidecar_rel,
        "git_repo_created": git_repo_created,
        "gitignore_added": gitignore_added,
        "worktree_added": worktree_added,
        "already_mounted": bool(existing_branch == normalized_branch),
        "migrated_existing_sidecar": migrated,
        "migration_commit": commit_result,
        **branch_meta,
    }
