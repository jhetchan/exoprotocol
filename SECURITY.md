# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in ExoProtocol, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, please email security concerns to the maintainers directly or use GitHub's private vulnerability reporting feature on this repository.

## What counts as a security issue

- Governance bypass (agent circumventing kernel policy checks)
- Lock/lease spoofing or escalation
- Audit log tampering or injection
- Path traversal in scope enforcement
- Command injection via ticket/intent fields
- Sensitive data exposure in logs or mementos

## Scope

This policy covers the `exo/kernel/`, `exo/control/`, `exo/stdlib/`, and `exo/orchestrator/` packages.

## Response timeline

We aim to acknowledge reports within 48 hours and provide a fix or mitigation plan within 7 days for critical issues.

## Supported versions

Only the latest release on `main` is actively supported with security patches.
