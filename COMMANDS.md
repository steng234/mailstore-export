# MailStore Export — Command Cheat Sheet

All commands ready to copy/paste. Run them from the project folder:

```bash
cd /path/to/mailstore-export
```

---

## 1. Initial setup (once)

```bash
# Copy the .env template and fill in your credentials
cp .env.example .env
chmod 600 .env   # restrictive permissions: only you can read/write

# Open and fill in: MAILSTORE_HOST, MAILSTORE_PORT, MAILSTORE_USER, MAILSTORE_PASSWORD
# (and optionally MAILSTORE_WORKERS, MAILSTORE_OUTPUT, MAILSTORE_ARCHIVES)
open .env
```

Recognized `.env` variables:

| Variable | Default | Notes |
|---|---|---|
| `MAILSTORE_HOST` | (required) | e.g. `127.0.0.1` |
| `MAILSTORE_PORT` | `8462` | Web Access port |
| `MAILSTORE_USER` | (required) | e.g. `admin` |
| `MAILSTORE_PASSWORD` | (required) | admin password |
| `MAILSTORE_TOKEN` | _empty_ | static Bearer token (alternative to user/password) |
| `MAILSTORE_WORKERS` | `32` | parallel download workers |
| `MAILSTORE_OUTPUT` | _empty_ | output folder |
| `MAILSTORE_ARCHIVES` | _empty_ | CSV of archives, e.g. `admin,b.montanari` |

---

## 2. Script A — Web API (recommended, accesses ALL archives)

File: `mailstore_webapi_export.py`. Talks to the REST API on port 8462.

### Interactive wizard (basic mode)

```bash
python3 mailstore_webapi_export.py
```

Guides you through: connection → archive selection → output → workers → confirmation.

### Skip the wizard (everything from .env)

```bash
python3 mailstore_webapi_export.py --no-interactive
```

Requires in `.env`: credentials + `MAILSTORE_OUTPUT` + `MAILSTORE_ARCHIVES`.

### Analysis only (count messages without downloading)

```bash
# Analyze ALL archives → JSON with statistics + top folders
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

# Analyze specific archives (enters the wizard for selection)
python3 mailstore_webapi_export.py --analyze

# Custom path for the JSON
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive \
  --analyze-output /tmp/mailstore_stats.json
```

Output: on-screen table + JSON in `mailstore_analysis_output/mailstore_analysis_YYYYMMDD_HHMMSS.json` with:
- Total time
- Totals for archives/folders/messages
- Estimated download hours at 50/200 msg/s
- Top 20 folders by message count
- Top 20 archives by message count
- Full per-archive structure

### Benchmark (find the optimal worker count)

```bash
# Standard sweep: 4,8,16,32,48,64,96,128 workers over 200 messages
python3 mailstore_webapi_export.py --benchmark --no-interactive

# Custom sweep
python3 mailstore_webapi_export.py --benchmark --no-interactive \
  --benchmark-workers "8,16,32,48,64" \
  --benchmark-samples 500

# Against a specific folder (format: "archive_name/folder/path")
python3 mailstore_webapi_export.py --benchmark --no-interactive \
  --benchmark-folder "b.montanari/Exchange b.montanari/Posta in arrivo"
```

Output: table with msg/s, MB/s, p50/p95 latency + "Absolute peak" and "90% sweet spot". JSON in `./mailstore_benchmark_YYYYMMDD_HHMMSS.json`.

**Practical tip**: after the benchmark, put the optimal number in `.env`:
```
MAILSTORE_WORKERS=32
```

### Regular export (download .eml)

```bash
# Via wizard
python3 mailstore_webapi_export.py

# Full CLI mode (no wizard, everything from flags or .env)
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin --archive b.montanari --archive "ca - amministrazione" \
  --output /Volumes/Backup/eml_export \
  --workers 32

# Force the wizard even when everything is configured
python3 mailstore_webapi_export.py -i

# Filter folders (regex)
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin \
  --output ./eml \
  --exclude-folder "Cestino|Spam|Posta eliminata"

# Include only certain folders
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin \
  --output ./eml \
  --include-folder "Posta in arrivo|Posta inviata"
```

### All WebAPI script flags

```bash
python3 mailstore_webapi_export.py --help
```

| Flag | Notes |
|---|---|
| `--host HOST` | MailStore IP/hostname |
| `--port PORT` | default 8462 |
| `--user USERNAME` | login username |
| `--password PASSWORD` | (prompted at runtime if omitted) |
| `--token TOKEN` | static Bearer token (expires in 5 min) |
| `--output PATH` | `.eml` output folder |
| `--archive NAME` | repeatable, archive to export |
| `--workers N` | parallel downloads |
| `--exclude-folder REGEX` | repeatable |
| `--include-folder REGEX` | repeatable |
| `-i`, `--interactive` | force the wizard |
| `--no-interactive` | disable the wizard |
| `--env-file PATH` | custom `.env` file (default: `./.env`) |
| `--analyze` | analysis mode (no download) |
| `--analyze-all` | with `--analyze`, scans ALL archives |
| `--analyze-output PATH` | JSON output path |
| `--benchmark` | worker-count benchmark mode |
| `--benchmark-folder ARCHIVE/PATH` | test folder |
| `--benchmark-samples N` | samples per step |
| `--benchmark-workers "1,4,8,..."` | CSV of worker counts to try |
| `--benchmark-output PATH` | JSON output path |

---

## 3. Script B — IMAP (sees only the logged-in archive)

File: `mailstore_export.py`. Talks IMAP on port 143/993. Limited to the IMAP user's own archive.

```bash
# Interactive wizard
python3 mailstore_export.py

# Full CLI
python3 mailstore_export.py --host 127.0.0.1 --port 993 --ssl \
  --user admin --output ./eml --workers 4

# Folder list only (no download)
python3 mailstore_export.py --user admin --output ./eml --list-only

# IMAP diagnostics (useful to see what the server exposes)
python3 mailstore_export.py --diag

# Non-interactive mode (for cron)
python3 mailstore_export.py --no-interactive \
  --user admin --password XXX --output ./eml
```

All flags:

```bash
python3 mailstore_export.py --help
```

---

## 4. Script C — Web API probe (reverse-engineering)

File: `mailstore_webapi_probe.py`. Interactive tool to explore the API. Only needed when discovering new endpoints.

```bash
python3 mailstore_webapi_probe.py
```

REPL commands after login:
- `get /api/PATH` — GET
- `post /api/PATH` — empty POST
- `postj /api/PATH` — POST + prompts for a JSON body
- `cookies` / `base` / `json` / `raw` / `save FILE`
- `probe` — re-runs the automatic probe
- `help` / `quit`

---

## 5. Recommended workflows

### First time against this MailStore

```bash
# 1. Setup
cp .env.example .env && chmod 600 .env
# (fill in .env)

# 2. Analyze: see what's there, estimate time
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

# 3. Benchmark: find the optimal worker count
python3 mailstore_webapi_export.py --benchmark --no-interactive

# 4. Put the optimal worker count in .env
echo "MAILSTORE_WORKERS=32" >> .env   # replace 32 with the benchmark result

# 5. Real export (start with ONE archive as a test)
python3 mailstore_webapi_export.py
# → select 1 small archive, confirm, wait for it to finish

# 6. Once confident, run against everything
# (select all archives in the wizard, or set MAILSTORE_ARCHIVES in .env)
```

### Resume an interrupted export

Same command as before. The state.db keeps track automatically:

```bash
python3 mailstore_webapi_export.py
```

All previously downloaded messages are skipped.

### Scheduled/incremental export

```bash
# Cron: every night at 02:00, export everything
0 2 * * *  cd /path/to/mailstore-export && \
           python3 mailstore_webapi_export.py --no-interactive >> cron.log 2>&1
```

Requires `MAILSTORE_ARCHIVES` and `MAILSTORE_OUTPUT` in `.env`.

---

## 6. Querying the state.db

All state data lives in `<output>/.mailstore_webapi_export/state.db` (SQLite).

```bash
DB=/Volumes/Backup/eml_export/.mailstore_webapi_export/state.db

# Total exported message count
sqlite3 "$DB" "SELECT COUNT(*) FROM exported_api;"

# Total volume in MB
sqlite3 "$DB" "SELECT printf('%.1f MB', SUM(size)/1024.0/1024.0) FROM exported_api;"

# Messages per archive
sqlite3 -header -column "$DB" \
  "SELECT archive, COUNT(*) AS msg, SUM(size)/1024/1024 AS mb
   FROM exported_api GROUP BY archive ORDER BY msg DESC;"

# Top 20 folders by messages found by the search
sqlite3 -header -column "$DB" \
  "SELECT archive, folder, found_count
   FROM folder_counts ORDER BY found_count DESC LIMIT 20;"

# Comparison: messages found by the search vs actually downloaded per folder
sqlite3 -header -column "$DB" \
  "SELECT fc.archive, fc.folder, fc.found_count AS expected,
          COUNT(e.api_key) AS actual,
          fc.found_count - COUNT(e.api_key) AS missing
   FROM folder_counts fc
   LEFT JOIN exported_api e ON e.archive = fc.archive AND e.folder = fc.folder
   GROUP BY fc.archive, fc.folder
   HAVING missing > 0
   ORDER BY missing DESC;"
```

---

## 7. Quick troubleshooting

### Error: "Login failed (HTTP 401)"

```bash
# Check credentials
cat .env | grep -v PASSWORD

# Test the API manually
curl -k -u admin:YOURPASSWORD -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"YOURPASSWORD","trustedDeviceToken":null}' \
  https://127.0.0.1:8462/api/authenticate/internal
```

### "cached search result is not or no longer available" errors

Too much concurrency. Lower the workers:

```bash
python3 mailstore_webapi_export.py --no-interactive --workers 8
```

### System slow, RAM full, heavy swapping

Workers too high. Kill with Ctrl+C, restart with fewer workers:

```bash
# Check the Python process memory/threads
top -pid $(pgrep -n python3)

# Restart with a conservative value
python3 mailstore_webapi_export.py --workers 16
```

### Reset and start from scratch

```bash
# Delete state.db (you lose the resume!)
rm -i /Volumes/Backup/eml_export/.mailstore_webapi_export/state.db

# Delete ALL output (files + state)
rm -rf /Volumes/Backup/eml_export
```

### Check the logs of the last run

```bash
ls -lt /Volumes/Backup/eml_export/.mailstore_webapi_export/export_*.log | head -1
tail -100 $(ls -t /Volumes/Backup/eml_export/.mailstore_webapi_export/export_*.log | head -1)
```

---

## 8. Network diagnostic commands

```bash
# Which MailStore ports are reachable?
for p in 143 993 8460 8461 8462 8463 8474; do
  echo -n "Port $p: "
  nc -zv -G 2 127.0.0.1 $p 2>&1 | grep -E "succeeded|refused|timed out"
done

# Quick API test
curl -k https://127.0.0.1:8462/

# Test login + GetArchives
TOKEN=$(curl -ks -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$MAILSTORE_PASSWORD\",\"trustedDeviceToken\":null}" \
  https://127.0.0.1:8462/api/authenticate/internal | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['tokenInformation']['access_token'])")
curl -k -H "Authorization: Bearer $TOKEN" https://127.0.0.1:8462/api/archives | \
  python3 -m json.tool | head -40
```

---

## 9. The three commands you'll use most often

```bash
# 1. Count everything without downloading
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

# 2. Find the optimal worker count
python3 mailstore_webapi_export.py --benchmark --no-interactive

# 3. Real export, once the right worker count is in .env
python3 mailstore_webapi_export.py
```
