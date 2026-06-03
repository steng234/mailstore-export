# Security Policy

## Supported versions

This project is distributed as-is. Security fixes are applied to the latest
version on the default branch.

## Reporting a vulnerability

**Please do not report security issues in public GitHub issues.**

Instead, use **GitHub's private vulnerability reporting**:
*Security → Report a vulnerability* on the repository page (GitHub Security
Advisories). If that is unavailable, open a minimal issue asking the maintainers
to enable it — without disclosing details.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (with **no real credentials or private data**).
- Affected file(s) / version.

## Scope & known design choices

- **TLS verification is disabled by default** for the MailStore Web API, because
  MailStore commonly uses a self-signed certificate on a LAN. This is a
  deliberate default for the typical deployment; on untrusted networks you
  should not rely on it. This is documented, not a vulnerability per se.
- Credentials are read from `.env`, an interactive prompt, or a static token.
  They are never written to logs, and the GUI hands them to the export
  subprocess through a private temporary env-file (mode `600`) rather than the
  command line.

## Your responsibility

Exported `.eml` files, the `state.db`, and log files may contain sensitive email
data. Store them securely and never commit them — the provided `.gitignore`
already excludes them.
