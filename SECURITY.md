# Security policy

## Supported versions

The current `0.1.x` development line receives security fixes. No production-hardening or
multi-tenant security guarantee is made.

## Reporting a vulnerability

After the repository is published, use GitHub's private vulnerability-reporting channel. Do not
open a public issue containing credentials, private source code, exploit details, or provider
request data. Until a private channel is configured, keep the report local and contact the
repository owner through the account that publishes the project.

Never include real API keys in a report. Horizon reads local credentials from `.env` or the
environment; `.env` is ignored by Git. Reproduction material should use synthetic fixtures and
redacted Trace excerpts.

## Current boundary

Docker contract tests cover non-root execution, disabled networking, a read-only root filesystem,
resource limits, bounded output, and disposable workspaces. They are not an independent security
audit and do not establish safe execution of arbitrary hostile code. See the README and development
record for the precise evidence boundary.
