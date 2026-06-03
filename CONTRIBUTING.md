# Contributing to MailStore Export

Thanks for your interest in improving this project! Contributions of all kinds
are welcome — bug reports, fixes, features, and documentation.

## Ground rules

- **Standard library only.** The project intentionally has **zero external
  dependencies**. Please don't add `pip` packages without opening an issue to
  discuss it first.
- **Python 3.9 compatible.** The CLI scripts must keep running on Python 3.9.
  Use `from __future__ import annotations` at the top of any file using modern
  type hints (`list[int]`, `X | None`, …).
- **Never commit secrets or real data.** No credentials, hostnames, email
  content, archive names, or absolute personal paths. `.env` is git-ignored —
  keep it that way.

## Development setup

```bash
git clone https://github.com/<you>/mailstore-export.git
cd mailstore-export
cp .env.example .env   # fill in your own test server
```

The GUI needs a Python with a working Tk (see the README for per-OS notes).

## Before opening a pull request

1. **Compile-check on both interpreters** you target:
   ```bash
   python3 -m py_compile mailstore_*.py
   ```
2. **Smoke-test what you touched.** For GUI changes, launch the app and exercise
   the affected widgets. For exporter changes, run a small export against a test
   archive.
3. Keep changes focused and described clearly in the PR.
4. Match the surrounding code style (naming, comments in English, log messages
   in English).

## Reporting bugs

Open an issue using the bug template. Include your OS, Python version, the
command/flow that triggered it, and the relevant log lines from
`.mailstore_webapi_export/export_*.log` — **with any private data removed**.

## Security issues

Please **do not** open a public issue for vulnerabilities. See
[SECURITY.md](SECURITY.md).
