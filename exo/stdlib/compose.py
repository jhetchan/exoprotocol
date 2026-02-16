"""Policy compiler: compile all governance subsystems into a single sealed artifact.

Reads constitution, config, features, and requirements manifests, then produces
`.exo/policy.sealed.json` — a canonical, hash-verified snapshot of all governance
state. Hooks and session-start verify against this artifact for tamper detection.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from exo.kernel.errors import ExoError
from exo.kernel.utils import load_json, load_yaml, now_iso

SEALED_POLICY_PATH = Path(".exo/policy.sealed.json")
CONSTITUTION_PATH = Path(".exo/CONSTITUTION.md")
GOVERNANCE_LOCK_PATH = Path(".exo/governance.lock.json")
CONFIG_PATH = Path(".exo/config.yaml")
FEATURES_PATH = Path(".exo/features.yaml")
REQUIREMENTS_PATH = Path(".exo/requirements.yaml")
HOOKS_SETTINGS_PATH = Path(".claude/settings.json")


def _sha256_file(path: Path) -> str:
    """SHA-256 hex digest of a file, or empty string if missing."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_str(content: str) -> str:
    """SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _load_governance_lock(repo: Path) -> dict[str, Any]:
    lock_path = repo / GOVERNANCE_LOCK_PATH
    if not lock_path.exists():
        raise ExoError(
            code="GOVERNANCE_LOCK_MISSING",
            message="governance.lock.json not found — run `exo build-governance` first",
            blocked=True,
        )
    return load_json(lock_path)


def _load_config(repo: Path) -> dict[str, Any]:
    config_path = repo / CONFIG_PATH
    if not config_path.exists():
        return {}
    return load_yaml(config_path)


def _compute_source_hashes(repo: Path) -> dict[str, str]:
    """SHA-256 each governance source file."""
    return {
        "constitution": _sha256_file(repo / CONSTITUTION_PATH),
        "config": _sha256_file(repo / CONFIG_PATH),
        "features": _sha256_file(repo / FEATURES_PATH),
        "requirements": _sha256_file(repo / REQUIREMENTS_PATH),
    }


def _extract_deny_patterns(rules: list[dict[str, Any]]) -> list[str]:
    """Pull deny patterns from filesystem_deny constitution rules."""
    patterns: list[str] = []
    for rule in rules:
        if rule.get("type") == "filesystem_deny":
            patterns.extend(rule.get("patterns", []))
    return sorted(set(patterns))


def _extract_feature_deny(repo: Path) -> list[str]:
    """Load features.yaml and extract scope deny globs for locked features."""
    features_path = repo / FEATURES_PATH
    if not features_path.exists():
        return []
    try:
        from exo.stdlib.features import generate_scope_deny, load_features

        features = load_features(repo)
        return generate_scope_deny(features)
    except Exception:
        return []


def _compute_hooks_hash(repo: Path) -> str:
    """SHA-256 of .claude/settings.json hooks section, or empty if missing."""
    settings_path = repo / HOOKS_SETTINGS_PATH
    if not settings_path.exists():
        return ""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks")
        if not hooks:
            return ""
        canonical = json.dumps(hooks, sort_keys=True, ensure_ascii=True)
        return _sha256_str(canonical)
    except (json.JSONDecodeError, OSError):
        return ""


def _compute_integrity_hash(policy: dict[str, Any]) -> str:
    """Compute SHA-256 of policy dict (excluding integrity_hash field itself)."""
    to_hash = {k: v for k, v in policy.items() if k != "integrity_hash"}
    canonical = json.dumps(to_hash, sort_keys=True, ensure_ascii=True)
    return _sha256_str(canonical)


def compose(repo: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Compile all governance subsystems into a single sealed policy artifact.

    Reads constitution lock, config, features manifest, and requirements manifest.
    Produces `.exo/policy.sealed.json` with SHA-256 integrity verification.

    Args:
        repo: Repository root path.
        dry_run: Preview policy without writing file.

    Returns:
        Dict with sealed_policy_path, policy content, and dry_run flag.
    """
    repo = Path(repo).resolve()

    lock = _load_governance_lock(repo)
    config = _load_config(repo)

    policy: dict[str, Any] = {
        "version": "1",
        "composed_at": now_iso(),
        "sources": _compute_source_hashes(repo),
        "governance": {
            "kernel": lock.get("kernel", {}),
            "rules": lock.get("rules", []),
        },
        "deny_patterns": _extract_deny_patterns(lock.get("rules", [])),
        "scope_deny_from_features": _extract_feature_deny(repo),
        "budgets": config.get("defaults", {}).get("ticket_budgets", {}),
        "checks_allowlist": config.get("checks_allowlist", []),
        "coherence_rules": config.get("coherence", {}).get("co_update_rules", []),
        "hooks_hash": _compute_hooks_hash(repo),
    }

    policy["integrity_hash"] = _compute_integrity_hash(policy)

    if not dry_run:
        sealed_path = repo / SEALED_POLICY_PATH
        sealed_path.parent.mkdir(parents=True, exist_ok=True)
        sealed_path.write_text(
            json.dumps(policy, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    return {
        "sealed_policy_path": str(SEALED_POLICY_PATH),
        "policy": policy,
        "dry_run": dry_run,
    }


def load_sealed_policy(repo: Path) -> dict[str, Any] | None:
    """Load sealed policy from disk. Returns None if file is missing."""
    repo = Path(repo).resolve()
    sealed_path = repo / SEALED_POLICY_PATH
    if not sealed_path.exists():
        return None
    try:
        return load_json(sealed_path)
    except Exception:
        return None


def verify_sealed_policy(repo: Path) -> dict[str, Any]:
    """Verify sealed policy integrity by recomputing its hash.

    Returns:
        Dict with valid (bool), reason (str), and optionally the policy.
    """
    repo = Path(repo).resolve()
    policy = load_sealed_policy(repo)
    if policy is None:
        return {"valid": False, "reason": "missing"}

    stored_hash = policy.get("integrity_hash", "")
    if not stored_hash:
        return {"valid": False, "reason": "no_hash", "policy": policy}

    recomputed = _compute_integrity_hash(policy)
    if stored_hash != recomputed:
        return {"valid": False, "reason": "tampered", "policy": policy}

    # Check if sources have changed since composition
    current_sources = _compute_source_hashes(repo)
    stored_sources = policy.get("sources", {})
    stale_sources: list[str] = []
    for key in current_sources:
        if current_sources[key] != stored_sources.get(key, ""):
            stale_sources.append(key)

    if stale_sources:
        return {
            "valid": False,
            "reason": "stale",
            "stale_sources": stale_sources,
            "policy": policy,
        }

    return {"valid": True, "reason": "ok", "policy": policy}
