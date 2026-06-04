<p align="center">
  <img src="app_icon.png" alt="MailStore Export" width="120">
</p>

<h1 align="center">MailStore Export</h1>

<p align="center">
  Export your <strong>MailStore Server</strong> archives to individual <code>.eml</code> files —
  reliably, in parallel, and resumable — when the official MailStore Client export gives up.
</p>

<p align="center">
  <em>Pure Python standard library · CLI + terminal-styled GUI · macOS / Linux / Windows</em>
</p>

<p align="center">
  <a href="https://github.com/steng234/mailstore-export/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/steng234/mailstore-export/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-blue.svg">
  <img alt="Dependencies: none" src="https://img.shields.io/badge/dependencies-0-brightgreen.svg">
  <img alt="Platforms" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg">
  <a href="CONTRIBUTING.md"><img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-ff69b4.svg"></a>
</p>

```text
╭───────────────────────────────────────────────────────────────────────╮
│ mailstore@export:~$ ./mailstore-export --webapi --resume              │
│                                                                       │
│   MAILSTORE · EXPORT                                                  │
│   bulk .eml extraction for MailStore Server                           │
│   1.5 TB of mail  →  individual .eml files  ·  resumable  ·  parallel │
│                                                                       │
│   [############################--------]  72.4%   1.81M / 2.50M msg   │
│   142 msg/s   ·   ETA 1h 04m   ·   0 failed   ·   stdlib-only         │
╰───────────────────────────────────────────────────────────────────────╯
```

---

## Why this exists

The official MailStore Client export often dies on large archives with errors like
*"cannot create file or directory"* — usually overly long paths, invalid characters in
subjects, or duplicate names. Hours in, no resume, start over.

So this talks **directly to MailStore Server's Web API** (and, alternatively, its **IMAP**
interface), writing one `.eml` per message with bulletproof path handling and crash-safe
resume. It was built to move **~1.5 TB / millions of emails** off MailStore without
babysitting — and it did.

## Features

- **Web API exporter** (`mailstore_webapi_export.py`) — accesses **all archives** a
  compliance user can see, massively parallel.
- **IMAP exporter** (`mailstore_export.py`) — alternative path over IMAP.
- **Automatic resume** after a crash — an SQLite state DB tracks every exported message.
- **Optional dedup** by `Message-ID` across archives/folders.
- **Year/month filtering** — export only selected years (and months for a single year).
- **Split by year** — organize output into `archive/YYYY/` subfolders.
- **Accurate progress bar** — optional up-front message count for a fixed total.
- **Crash-safe atomic writes** (`.eml.tmp` + `fsync` + `os.replace`).
- **Path safety** — sanitization, Unicode NFC, UTF-8 truncation, collision handling.
- **Modern GUI** — dark "terminal" theme, live progress, archive search, multi-select
  year/month dropdowns, multi-language (IT / EN / ES / FR).
- **Zero dependencies** — Python standard library only.

## The GUI

A dark, terminal-styled front-end (multi-language):

![MailStore Export GUI](docs/screenshot.png)

## Requirements

- A running **MailStore Server** with the Web API (default port `8462`) and/or IMAP enabled.
- **Python 3.9+** for the CLI scripts (kept 3.9-compatible — see the note below).
- For the **GUI** you need a Python with a working Tk:
  - **macOS:** the system Python's Tk is often broken — use Homebrew:
    `brew install python-tk@3.12`
  - **Linux:** `sudo apt install python3 python3-tk` (or your distro's equivalent)
  - **Windows:** the official [python.org](https://www.python.org/downloads/) build includes Tkinter.

> **Python 3.9 note:** every module starts with `from __future__ import annotations`
> so modern type hints (`list[int]`, `X | None`) work on 3.9. Keep it that way.

## Quick start

```bash
git clone https://github.com/steng234/mailstore-export.git
cd mailstore-export
cp .env.example .env        # then fill in host/user/password
chmod 600 .env              # keep your credentials private
```

### GUI (recommended)

| Platform | Launch |
|---|---|
| macOS | double-click **`MailStore Export.app`** (or `./run_gui.command`) |
| Linux | `./run_gui.sh` (or install `mailstore-export.desktop`) |
| Windows | double-click **`run_gui.vbs`** (or `run_gui.bat`) |

The GUI loads `.env` automatically, lets you test the connection, pick archives,
filter by year/month, and watch live progress.

### CLI

```bash
# Sanity check: list archives (Web API)
python3 mailstore_webapi_export.py --analyze --analyze-all

# Full export of selected archives
python3 mailstore_webapi_export.py \
    --host 127.0.0.1 --port 8462 \
    --user admin \
    --output /Volumes/Backup/eml_export \
    --archive "admin" --archive "sales" \
    --workers 32 --split-by-year --count-first

# Resume after a crash: just run the same command again.
```

Useful flags: `--dedup-message-id`, `--year 2023 --month 12`, `--include-folder`,
`--exclude-folder`, `--count-first`. See `--help` on each script, or the full
[command reference](COMMANDS.md).

## Output layout

```
output/
├── admin/                  # one folder per archive
│   ├── 2023/               # with --split-by-year
│   │   └── Subject_20230515_143045_abc123de.eml
│   └── 2024/
└── .mailstore_webapi_export/
    ├── state.db            # resume state — do NOT delete mid-export
    └── export_*.log
```

File name: `{subject}_{YYYYMMDD_HHMMSS}_{hash8}.eml` (sanitized, max 180 bytes).

## Configuration

All settings can come from CLI flags or a `.env` file (CLI wins). See
[`.env.example`](.env.example). Your real `.env` is git-ignored — never commit it.

## Security notes

- TLS certificate verification is **disabled by default** for the Web API, since
  MailStore typically uses a self-signed cert on a LAN. Keep that in mind on untrusted
  networks.
- Credentials are read from `.env`/prompt and are never written to logs. The GUI passes
  them to the export subprocess via a private temporary env-file (mode `600`), never on
  the command line.

See [SECURITY.md](SECURITY.md) for the full threat model and reporting policy.

## Tests

Pure, side-effect-free logic (path sanitization, filename building, MIME decoding, IMAP
response parsing, `.env` loading, date helpers) is covered by a stdlib `unittest` suite —
no test runner to install, in keeping with the zero-dependency philosophy.

```bash
python3 -m unittest discover -s tests -v
```

CI runs both `py_compile` and the full suite on Python **3.9 → 3.13** (see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Project layout

| File | Purpose |
|---|---|
| `mailstore_webapi_export.py` | Web API exporter (main) |
| `mailstore_export.py` | IMAP exporter |
| `mailstore_search.py` | Full-text search across archives (Web API) |
| `mailstore_webapi_probe.py` | API discovery / diagnostics |
| `mailstore_gui.py` | Tkinter GUI front-end |
| `make_icon.py` | Generates the app icon (PNG/ICO/ICNS), pure stdlib |
| `tests/` | stdlib `unittest` suite for the pure helpers |

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). For vulnerabilities, see
[SECURITY.md](SECURITY.md). Notable changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## Author

```text
$ whoami
Stefan Anghel  ·  github.com/steng234
$ cat ./motivation
Built to move 1.5 TB of mail off MailStore Server without losing a single message.
```

Made with stubbornness and the standard library. If it saved you an export, a ⭐ is appreciated.

## License

[MIT](LICENSE) © 2026 Stefan Anghel ([@steng234](https://github.com/steng234))
