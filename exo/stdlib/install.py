"""One-shot ExoProtocol setup (``exo install``).

Orchestrates init → compile → adapter-generate → hook-install → gitignore
into a single idempotent command.  Each step is isolated — errors in one
don't block the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exo.kernel.utils import now_iso


# ── Data structures ────────────────────────────────────────────────


@dataclass
class InstallStep:
    """Result of one installation step."""

    name: str  # init | compile | adapters | hooks | gitignore
    status: str  # created | updated | skipped | error
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallReport:
    """Composite result of the full install pipeline."""

    steps: list[InstallStep] = field(default_factory=list)
    overall: str = ""  # ok | partial | error
    installed_at: str = ""
    dry_run: bool = False

    @property
    def succeeded(self) -> bool:
        return self.overall == "ok"

    @property
    def error_count(self) -> int:
        return sum(1 for s in self.steps if s.status == "error")


# ── Ephemeral paths excluded from git ──────────────────────────────

_EXO_GITIGNORE_ENTRIES = [
    "# ExoProtocol ephemeral data — do not commit",
    "cache/",
    "memory/sessions/",
    "logs/",
    "locks/",
    "audit/",
]

_GITIGNORE_SENTINEL = "cache/"


# ── Step implementations ──────────────────────────────────────────


def _install_init(repo: Path, *, dry_run: bool, scan: bool) -> InstallStep:
    """Run init if .exo/ doesn't exist; skip if already initialized."""
    exo_dir = repo / ".exo"
    if exo_dir.is_dir():
        has_constitution = (exo_dir / "CONSTITUTION.md").exists()
        has_config = (exo_dir / "config.yaml").exists()
        if has_constitution and has_config:
            return InstallStep(
                name="init",
                status="skipped",
                summary=".exo/ already initialized",
            )

    if dry_run:
        return InstallStep(
            name="init",
            status="skipped",
            summary="would create .exo/ scaffold (dry run)",
            details={"dry_run": True},
        )

    try:
        from exo.stdlib.engine import KernelEngine

        engine = KernelEngine(repo=str(repo), actor="human")
        result = engine.init(scan=scan)
        data = result.get("data", {})
        created = data.get("created", [])
        return InstallStep(
            name="init",
            status="created",
            summary=f"initialized .exo/ ({len(created)} files created)",
            details={"created": created},
        )
    except Exception as exc:
        return InstallStep(
            name="init",
            status="error",
            summary=f"init failed: {exc}",
        )


def _install_compile(repo: Path, *, dry_run: bool) -> InstallStep:
    """Compile constitution → governance.lock.json."""
    if not (repo / ".exo" / "CONSTITUTION.md").exists():
        return InstallStep(
            name="compile",
            status="skipped",
            summary="no CONSTITUTION.md found",
        )

    if dry_run:
        return InstallStep(
            name="compile",
            status="skipped",
            summary="would recompile governance (dry run)",
            details={"dry_run": True},
        )

    try:
        from exo.kernel.governance import compile_constitution

        result = compile_constitution(repo)
        source_hash = result.get("source_hash", "")[:16]
        return InstallStep(
            name="compile",
            status="updated",
            summary=f"governance compiled (hash: {source_hash}...)",
            details={"source_hash": result.get("source_hash", "")},
        )
    except Exception as exc:
        return InstallStep(
            name="compile",
            status="error",
            summary=f"compile failed: {exc}",
        )


def _install_adapters(repo: Path, *, dry_run: bool) -> InstallStep:
    """Generate all adapter files (brownfield-safe marker merge)."""
    if not (repo / ".exo" / "governance.lock.json").exists():
        return InstallStep(
            name="adapters",
            status="skipped",
            summary="no governance lock — compile first",
        )

    try:
        from exo.stdlib.adapters import generate_adapters

        result = generate_adapters(repo, dry_run=dry_run)
        if dry_run:
            targets = result.get("targets", [])
            return InstallStep(
                name="adapters",
                status="skipped",
                summary=f"would generate {len(targets)} adapter(s) (dry run)",
                details={"targets": targets, "dry_run": True},
            )
        written = result.get("written", [])
        return InstallStep(
            name="adapters",
            status="created" if written else "skipped",
            summary=f"{len(written)} adapter(s) written",
            details={"written": written},
        )
    except Exception as exc:
        return InstallStep(
            name="adapters",
            status="error",
            summary=f"adapter generation failed: {exc}",
        )


def _install_hooks(repo: Path, *, dry_run: bool) -> InstallStep:
    """Install all hooks: session lifecycle + enforcement + git pre-commit."""
    try:
        from exo.stdlib.hooks import install_all_hooks

        result = install_all_hooks(repo, dry_run=dry_run)
        hooks = result.get("hooks", [])
        installed_count = sum(1 for h in hooks if h.get("installed"))

        if dry_run:
            return InstallStep(
                name="hooks",
                status="skipped",
                summary=f"would install {len(hooks)} hook group(s) (dry run)",
                details={"hooks": hooks, "dry_run": True},
            )
        return InstallStep(
            name="hooks",
            status="created" if installed_count > 0 else "skipped",
            summary=f"{installed_count}/{len(hooks)} hook group(s) installed",
            details={"hooks": hooks},
        )
    except Exception as exc:
        return InstallStep(
            name="hooks",
            status="error",
            summary=f"hook install failed: {exc}",
        )


def _install_gitignore(repo: Path, *, dry_run: bool) -> InstallStep:
    """Create .exo/.gitignore for ephemeral paths."""
    gitignore_path = repo / ".exo" / ".gitignore"

    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        if _GITIGNORE_SENTINEL in content:
            return InstallStep(
                name="gitignore",
                status="skipped",
                summary=".exo/.gitignore already has ephemeral exclusions",
            )

    if dry_run:
        return InstallStep(
            name="gitignore",
            status="skipped",
            summary="would create .exo/.gitignore (dry run)",
            details={"dry_run": True},
        )

    try:
        new_content = "\n".join(_EXO_GITIGNORE_ENTRIES) + "\n"
        gitignore_path.parent.mkdir(parents=True, exist_ok=True)
        gitignore_path.write_text(new_content, encoding="utf-8")
        return InstallStep(
            name="gitignore",
            status="created",
            summary=".exo/.gitignore created",
        )
    except Exception as exc:
        return InstallStep(
            name="gitignore",
            status="error",
            summary=f"gitignore creation failed: {exc}",
        )


# ── Orchestrator ──────────────────────────────────────────────────


def install(
    repo: Path | str,
    *,
    dry_run: bool = False,
    skip_init: bool = False,
    skip_hooks: bool = False,
    skip_adapters: bool = False,
    scan: bool = True,
) -> InstallReport:
    """Run full ExoProtocol installation pipeline.

    Steps: init → compile → adapters → hooks → gitignore.
    Each step is isolated — errors in one don't block others.
    Idempotent: safe to run multiple times.
    """
    repo = Path(repo).resolve()
    steps: list[InstallStep] = []

    # 1. Init
    if not skip_init:
        steps.append(_install_init(repo, dry_run=dry_run, scan=scan))

    # 2. Compile (always, to ensure lock is current)
    steps.append(_install_compile(repo, dry_run=dry_run))

    # 3. Adapters
    if not skip_adapters:
        steps.append(_install_adapters(repo, dry_run=dry_run))

    # 4. Hooks
    if not skip_hooks:
        steps.append(_install_hooks(repo, dry_run=dry_run))

    # 5. .exo/.gitignore
    steps.append(_install_gitignore(repo, dry_run=dry_run))

    # Determine overall status
    has_error = any(s.status == "error" for s in steps)
    has_work = any(s.status in ("created", "updated") for s in steps)

    if has_error and has_work:
        overall = "partial"
    elif has_error:
        overall = "error"
    else:
        overall = "ok"

    return InstallReport(
        steps=steps,
        overall=overall,
        installed_at=now_iso(),
        dry_run=dry_run,
    )


# ── Serialization ─────────────────────────────────────────────────


def install_to_dict(report: InstallReport) -> dict[str, Any]:
    """Convert InstallReport to JSON-safe dict."""
    return {
        "overall": report.overall,
        "succeeded": report.succeeded,
        "dry_run": report.dry_run,
        "installed_at": report.installed_at,
        "error_count": report.error_count,
        "step_count": len(report.steps),
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "summary": s.summary,
                "details": s.details,
            }
            for s in report.steps
        ],
    }


def format_install_human(report: InstallReport) -> str:
    """Format InstallReport for human-readable CLI output."""
    dry_tag = " (DRY RUN)" if report.dry_run else ""
    if report.succeeded:
        verdict = "OK"
    elif report.overall == "partial":
        verdict = "PARTIAL"
    else:
        verdict = "ERROR"

    lines = [
        f"ExoProtocol Install{dry_tag}: {verdict}",
        f"  steps: {len(report.steps)}, errors: {report.error_count}",
        "",
    ]

    icon_map = {
        "created": "+",
        "updated": "~",
        "skipped": "-",
        "error": "!",
    }
    for step in report.steps:
        marker = icon_map.get(step.status, "?")
        lines.append(f"  [{marker}] {step.name}: {step.summary}")

    return "\n".join(lines)
