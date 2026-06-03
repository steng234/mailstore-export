# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-06-03

### Added

- **Web API exporter** (`mailstore_webapi_export.py`): exports all reachable
  archives in parallel, with automatic resume via an SQLite state DB.
- **IMAP exporter** (`mailstore_export.py`) as an alternative path.
- **Full-text search** across archives (`mailstore_search.py`) and an API
  **probe/diagnostics** tool (`mailstore_webapi_probe.py`).
- **GUI** (`mailstore_gui.py`): dark terminal theme, live progress bar, archive
  search, multi-select year/month dropdowns, multi-language UI (IT / EN / ES / FR).
- **Export options**: optional `Message-ID` dedup, `--split-by-year`, year/month
  filtering, and `--count-first` for a fixed progress-bar total.
- **Crash-safe atomic writes**, Unicode-safe path handling, and collision
  resolution.
- **Cross-platform launchers**: macOS `.app`, `run_gui.command`, `run_gui.sh`
  (macOS/Linux), `run_gui.bat` / `run_gui.vbs` (Windows).
- **Icon generator** (`make_icon.py`) producing PNG / ICO / ICNS — pure stdlib.

[1.0.0]: https://github.com/<you>/mailstore-export/releases/tag/v1.0.0
