# Contributing to ExoProtocol

Thanks for your interest in ExoProtocol! This document covers the basics for contributing.

## Development setup

```bash
git clone <repo-url> && cd build-exo
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Running tests

```bash
python -m pytest tests/ -v
```

All PRs must pass the full test suite on Python 3.10+.

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for formatting and linting:

```bash
pip install ruff
ruff format exo/ tests/
ruff check exo/ tests/
```

## Architecture boundaries

Before making changes, understand the layer model:

- **`exo/kernel/`** — Enforcement core (governance, tickets/locks, audit, errors). Frozen 10-function public API. Do not expand without RFC.
- **`exo/control/`** — Transport-neutral control-plane wrappers over kernel syscalls.
- **`exo/stdlib/`** — Orchestration and userland behaviors (dispatch, recall, scratchpad, evolution protocol, engine).
- **`exo/orchestrator/`** — Layer-3 agent/task/workflow orchestration. Execution routes through kernel syscall checks.

Changes to `exo/kernel/` require extra scrutiny. See `KERNEL_EVOLUTION_POLICY.md`.

## Pull request process

1. Fork the repo and create a feature branch from `main`.
2. Write tests for new behavior. Maintain or improve coverage.
3. Run `python -m pytest tests/ -v` and `ruff check exo/ tests/` locally.
4. Open a PR with a clear title and description of what changed and why.
5. Keep PRs focused — one feature or fix per PR.

## Commit messages

Use conventional commit style:

```
feat(session): add suspend/resume lifecycle
fix(kernel): serialize local lease mutations
docs(agents): add root AGENTS shim
```

## Reporting bugs

Open a GitHub issue with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS
- Relevant `exo status --format json` output

## Questions?

Open a discussion or issue. We're happy to help.
