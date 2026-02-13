"""Config Schema Validation.

Validates ``.exo/config.yaml`` structure, types, and value ranges.
Returns a list of issues (errors and warnings) rather than raising.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exo.kernel.utils import load_yaml


CONFIG_PATH = Path(".exo/config.yaml")

# Current schema version
CURRENT_VERSION = 1

# Required top-level keys and their expected types
_REQUIRED_KEYS: dict[str, type | tuple[type, ...]] = {
    "version": int,
    "defaults": dict,
    "checks_allowlist": list,
    "do_allowlist": list,
    "recall_paths": list,
    "self_evolution": dict,
    "scheduler": dict,
    "control_caps": dict,
    "git_controls": dict,
    "privacy": dict,
    "coherence": dict,
}

# Required nested keys
_REQUIRED_DEFAULTS_KEYS: dict[str, type | tuple[type, ...]] = {
    "ticket_budgets": dict,
}

_REQUIRED_BUDGET_KEYS: dict[str, type | tuple[type, ...]] = {
    "max_files_changed": int,
    "max_loc_changed": int,
}

_REQUIRED_GIT_CONTROLS_KEYS: dict[str, type | tuple[type, ...]] = {
    "enabled": bool,
    "ignore_paths": list,
}

_REQUIRED_PRIVACY_KEYS: dict[str, type | tuple[type, ...]] = {
    "commit_logs": bool,
    "redact_local_paths": bool,
}

_REQUIRED_COHERENCE_KEYS: dict[str, type | tuple[type, ...]] = {
    "enabled": bool,
    "co_update_rules": list,
    "docstring_languages": list,
    "skip_patterns": list,
}


@dataclass
class ConfigIssue:
    """A single config validation issue."""
    severity: str  # error | warning
    path: str      # dotted key path (e.g., "defaults.ticket_budgets.max_files_changed")
    message: str


@dataclass
class ConfigValidation:
    """Result of config validation."""
    issues: list[ConfigIssue] = field(default_factory=list)
    config_exists: bool = False
    config_version: int | None = None

    @property
    def passed(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")


def validate_config(repo: Path) -> ConfigValidation:
    """Validate .exo/config.yaml structure, types, and values."""
    repo = Path(repo).resolve()
    result = ConfigValidation()

    config_path = repo / CONFIG_PATH
    if not config_path.exists():
        result.config_exists = False
        result.issues.append(ConfigIssue(
            severity="error",
            path="config.yaml",
            message="Config file not found",
        ))
        return result

    result.config_exists = True

    try:
        data = load_yaml(config_path)
    except Exception as exc:  # noqa: BLE001
        result.issues.append(ConfigIssue(
            severity="error",
            path="config.yaml",
            message=f"Failed to parse YAML: {exc}",
        ))
        return result

    if not isinstance(data, dict):
        result.issues.append(ConfigIssue(
            severity="error",
            path="config.yaml",
            message="Config must be a YAML mapping",
        ))
        return result

    # Version check
    version = data.get("version")
    if version is not None:
        if isinstance(version, int):
            result.config_version = version
            if version > CURRENT_VERSION:
                result.issues.append(ConfigIssue(
                    severity="warning",
                    path="version",
                    message=f"Config version {version} is newer than supported {CURRENT_VERSION}",
                ))
        else:
            result.issues.append(ConfigIssue(
                severity="error",
                path="version",
                message=f"version must be an integer, got {type(version).__name__}",
            ))

    # Check required top-level keys
    _check_keys(data, _REQUIRED_KEYS, "", result.issues)

    # Check nested defaults
    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        _check_keys(defaults, _REQUIRED_DEFAULTS_KEYS, "defaults", result.issues)
        budgets = defaults.get("ticket_budgets")
        if isinstance(budgets, dict):
            _check_keys(budgets, _REQUIRED_BUDGET_KEYS, "defaults.ticket_budgets", result.issues)
            # Value range checks
            for key in ("max_files_changed", "max_loc_changed"):
                val = budgets.get(key)
                if isinstance(val, int) and val <= 0:
                    result.issues.append(ConfigIssue(
                        severity="error",
                        path=f"defaults.ticket_budgets.{key}",
                        message=f"{key} must be positive, got {val}",
                    ))

    # Check git_controls
    git_controls = data.get("git_controls")
    if isinstance(git_controls, dict):
        _check_keys(git_controls, _REQUIRED_GIT_CONTROLS_KEYS, "git_controls", result.issues)

    # Check privacy
    privacy = data.get("privacy")
    if isinstance(privacy, dict):
        _check_keys(privacy, _REQUIRED_PRIVACY_KEYS, "privacy", result.issues)

    # Check coherence
    coherence = data.get("coherence")
    if isinstance(coherence, dict):
        _check_keys(coherence, _REQUIRED_COHERENCE_KEYS, "coherence", result.issues)

    # Check list items are strings
    for list_key in ("checks_allowlist", "do_allowlist", "recall_paths"):
        val = data.get(list_key)
        if isinstance(val, list):
            for i, item in enumerate(val):
                if not isinstance(item, str):
                    result.issues.append(ConfigIssue(
                        severity="warning",
                        path=f"{list_key}[{i}]",
                        message=f"Expected string, got {type(item).__name__}",
                    ))

    return result


def _check_keys(
    data: dict[str, Any],
    schema: dict[str, type | tuple[type, ...]],
    prefix: str,
    issues: list[ConfigIssue],
) -> None:
    """Check that required keys exist and have correct types."""
    for key, expected_type in schema.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in data:
            issues.append(ConfigIssue(
                severity="warning",
                path=path,
                message=f"Missing key '{key}'",
            ))
        else:
            val = data[key]
            if not isinstance(val, expected_type):
                expected_name = (
                    expected_type.__name__
                    if isinstance(expected_type, type)
                    else " | ".join(t.__name__ for t in expected_type)
                )
                issues.append(ConfigIssue(
                    severity="error",
                    path=path,
                    message=f"Expected {expected_name}, got {type(val).__name__}",
                ))


# ── Serialization ──────────────────────────────────────────────────


def validation_to_dict(result: ConfigValidation) -> dict[str, Any]:
    """Convert ConfigValidation to a plain dict for JSON."""
    return {
        "passed": result.passed,
        "config_exists": result.config_exists,
        "config_version": result.config_version,
        "error_count": result.error_count,
        "warning_count": result.warning_count,
        "issues": [
            {"severity": i.severity, "path": i.path, "message": i.message}
            for i in result.issues
        ],
    }


def format_validation_human(result: ConfigValidation) -> str:
    """Format ConfigValidation as human-readable text."""
    status = "PASS" if result.passed else "FAIL"
    lines = [
        f"Config Validation: {status}",
        f"  errors: {result.error_count}, warnings: {result.warning_count}",
    ]
    if result.config_version is not None:
        lines.append(f"  version: {result.config_version}")
    for issue in result.issues:
        tag = "ERROR" if issue.severity == "error" else "WARN"
        lines.append(f"  [{tag}] {issue.path}: {issue.message}")
    if not result.issues:
        lines.append("  No issues found.")
    return "\n".join(lines)
