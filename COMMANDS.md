# MailStore Export — Cheat Sheet Comandi

Tutti i comandi pronti da copia/incollare. Lanciali dalla cartella del progetto:

```bash
cd /path/to/mailstore-export
```

---

## 1. Setup iniziale (una volta)

```bash
# Copia template .env e compila credenziali
cp .env.example .env
chmod 600 .env   # permessi restrittivi: solo tu leggi/scrivi

# Apri e compila: MAILSTORE_HOST, MAILSTORE_PORT, MAILSTORE_USER, MAILSTORE_PASSWORD
# (e opzionalmente MAILSTORE_WORKERS, MAILSTORE_OUTPUT, MAILSTORE_ARCHIVES)
open .env
```

Variabili `.env` riconosciute:

| Variabile | Default | Note |
|---|---|---|
| `MAILSTORE_HOST` | (richiesto) | es. `127.0.0.1` |
| `MAILSTORE_PORT` | `8462` | Porta Web Access |
| `MAILSTORE_USER` | (richiesto) | es. `admin` |
| `MAILSTORE_PASSWORD` | (richiesto) | password admin |
| `MAILSTORE_TOKEN` | _vuoto_ | Bearer statico (alternativa a user/password) |
| `MAILSTORE_WORKERS` | `32` | Worker paralleli per download |
| `MAILSTORE_OUTPUT` | _vuoto_ | Cartella output |
| `MAILSTORE_ARCHIVES` | _vuoto_ | CSV di archivi, es. `admin,b.montanari` |

---

## 2. Script A — Web API (consigliato, accede a TUTTI gli archivi)

File: `mailstore_webapi_export.py`. Comunica via REST API porta 8462.

### Wizard interattivo (modalità base)

```bash
python3 mailstore_webapi_export.py
```

Ti guida: connessione → selezione archivi → output → workers → conferma.

### Skip wizard (tutto da .env)

```bash
python3 mailstore_webapi_export.py --no-interactive
```

Richiede in `.env`: credenziali + `MAILSTORE_OUTPUT` + `MAILSTORE_ARCHIVES`.

### Solo analisi (conta i messaggi senza scaricare)

```bash
# Analisi di TUTTI i 51 archivi → JSON con statistiche + Top folders
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

# Analisi di archivi specifici (entra nel wizard per la selezione)
python3 mailstore_webapi_export.py --analyze

# Custom path per il JSON
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive \
  --analyze-output /tmp/mailstore_stats.json
```

Output: tabella a video + JSON in `mailstore_analysis_output/mailstore_analysis_YYYYMMDD_HHMMSS.json` con:
- Tempo totale
- Totali archivi/cartelle/messaggi
- Stima ore di download a 50/200 msg/s
- Top 20 cartelle per messaggi
- Top 20 archivi per messaggi
- Struttura completa per archivio

### Benchmark (trova il numero ottimale di worker)

```bash
# Sweep standard: 4,8,16,32,48,64,96,128 worker su 200 messaggi
python3 mailstore_webapi_export.py --benchmark --no-interactive

# Sweep custom
python3 mailstore_webapi_export.py --benchmark --no-interactive \
  --benchmark-workers "8,16,32,48,64" \
  --benchmark-samples 500

# Su cartella specifica (formato: "archive_name/folder/path")
python3 mailstore_webapi_export.py --benchmark --no-interactive \
  --benchmark-folder "b.montanari/Exchange b.montanari/Posta in arrivo"
```

Output: tabella con msg/s, MB/s, latenza p50/p95 + "Picco assoluto" e "Sweet spot 90%". JSON in `./mailstore_benchmark_YYYYMMDD_HHMMSS.json`.

**Suggerimento operativo**: dopo il benchmark, metti il numero ottimo in `.env`:
```
MAILSTORE_WORKERS=32
```

### Export normale (download .eml)

```bash
# Da wizard
python3 mailstore_webapi_export.py

# Modalità CLI piena (no wizard, tutto da flag o .env)
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin --archive b.montanari --archive "ca - amministrazione" \
  --output /Volumes/Backup/eml_export \
  --workers 32

# Forza il wizard anche se hai tutto
python3 mailstore_webapi_export.py -i

# Filtra cartelle (regex)
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin \
  --output ./eml \
  --exclude-folder "Cestino|Spam|Posta eliminata"

# Includi solo certe cartelle
python3 mailstore_webapi_export.py --no-interactive \
  --archive admin \
  --output ./eml \
  --include-folder "Posta in arrivo|Posta inviata"
```

### Tutti i flag del WebAPI script

```bash
python3 mailstore_webapi_export.py --help
```

| Flag | Note |
|---|---|
| `--host HOST` | IP/hostname MailStore |
| `--port PORT` | default 8462 |
| `--user USERNAME` | username login |
| `--password PASSWORD` | (chiesta a runtime se omessa) |
| `--token TOKEN` | Bearer token statico (scade in 5 min) |
| `--output PATH` | cartella `.eml` |
| `--archive NAME` | ripetibile, archivio da esportare |
| `--workers N` | download paralleli |
| `--exclude-folder REGEX` | ripetibile |
| `--include-folder REGEX` | ripetibile |
| `-i`, `--interactive` | forza wizard |
| `--no-interactive` | disabilita wizard |
| `--env-file PATH` | file `.env` custom (default: `./.env`) |
| `--analyze` | modalità analisi (no download) |
| `--analyze-all` | con `--analyze`, scansiona TUTTI gli archivi |
| `--analyze-output PATH` | path JSON output |
| `--benchmark` | modalità benchmark worker count |
| `--benchmark-folder ARCHIVE/PATH` | cartella di test |
| `--benchmark-samples N` | campioni per step |
| `--benchmark-workers "1,4,8,..."` | CSV worker da provare |
| `--benchmark-output PATH` | path JSON output |

---

## 3. Script B — IMAP (vede solo l'archivio loggato)

File: `mailstore_export.py`. Comunica via IMAP porta 143/993. Limitato all'archivio dell'utente IMAP.

```bash
# Wizard interattivo
python3 mailstore_export.py

# CLI completa
python3 mailstore_export.py --host 127.0.0.1 --port 993 --ssl \
  --user admin --output ./eml --workers 4

# Solo lista cartelle (no download)
python3 mailstore_export.py --user admin --output ./eml --list-only

# Diagnostica IMAP (utile per capire cosa il server espone)
python3 mailstore_export.py --diag

# Modalità non interattiva (per cron)
python3 mailstore_export.py --no-interactive \
  --user admin --password XXX --output ./eml
```

Tutti i flag:

```bash
python3 mailstore_export.py --help
```

---

## 4. Script C — Probe Web API (reverse-engineering)

File: `mailstore_webapi_probe.py`. Strumento interattivo per esplorare l'API. Da usare solo se devi scoprire nuovi endpoint.

```bash
python3 mailstore_webapi_probe.py
```

Comandi REPL dopo il login:
- `get /api/PATH` — GET
- `post /api/PATH` — POST vuoto
- `postj /api/PATH` — POST + chiede body JSON
- `cookies` / `base` / `json` / `raw` / `save FILE`
- `probe` — ri-esegue il probe automatico
- `help` / `quit`

---

## 5. Workflow consigliati

### Prima volta su questo MailStore

```bash
# 1. Setup
cp .env.example .env && chmod 600 .env
# (compila .env)

# 2. Analisi: vedi cosa c'è, stima tempi
python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

# 3. Benchmark: trova il workers ottimale
python3 mailstore_webapi_export.py --benchmark --no-interactive

# 4. Metti il workers ottimale in .env
echo "MAILSTORE_WORKERS=32" >> .env   # sostituisci 32 col risultato del benchmark

# 5. Export vero (parti con UN archivio per test)
python3 mailstore_webapi_export.py
# → seleziona 1 archivio piccolo, conferma, aspetta che finisca

# 6. Quando sicuro, lancia su tutti
# (seleziona tutti gli archivi nel wizard, oppure metti MAILSTORE_ARCHIVES in .env)
```

### Riprendere un export interrotto

Stesso comando di prima. Lo state.db tiene traccia automaticamente:

```bash
python3 mailstore_webapi_export.py
```

Tutti i messaggi già scaricati vengono saltati.

### Export schedulato/incrementale

```bash
# Cron: ogni notte alle 02:00, export di tutto
0 2 * * *  cd /path/to/mailstore-export && \
           python3 mailstore_webapi_export.py --no-interactive >> cron.log 2>&1
```

Richiede `MAILSTORE_ARCHIVES` e `MAILSTORE_OUTPUT` in `.env`.

---

## 6. Interrogare lo state.db

Tutti i dati di stato sono in `<output>/.mailstore_webapi_export/state.db` (SQLite).

```bash
DB=/Volumes/Backup/eml_export/.mailstore_webapi_export/state.db

# Conteggio totale messaggi esportati
sqlite3 "$DB" "SELECT COUNT(*) FROM exported_api;"

# Volume totale in MB
sqlite3 "$DB" "SELECT printf('%.1f MB', SUM(size)/1024.0/1024.0) FROM exported_api;"

# Messaggi per archivio
sqlite3 -header -column "$DB" \
  "SELECT archive, COUNT(*) AS msg, SUM(size)/1024/1024 AS mb
   FROM exported_api GROUP BY archive ORDER BY msg DESC;"

# Top 20 cartelle per numero di messaggi trovati dalla search
sqlite3 -header -column "$DB" \
  "SELECT archive, folder, found_count
   FROM folder_counts ORDER BY found_count DESC LIMIT 20;"

# Confronto: messaggi trovati dalla search vs effettivamente scaricati per cartella
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

## 7. Troubleshooting rapido

### Errore: "Login fallito (HTTP 401)"

```bash
# Verifica credenziali
cat .env | grep -v PASSWORD

# Testa manualmente l'API
curl -k -u admin:LATUAPASSWORD -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"LATUAPASSWORD","trustedDeviceToken":null}' \
  https://127.0.0.1:8462/api/authenticate/internal
```

### Errori "cached search result is not or no longer available"

Troppo concorrenza. Abbassa workers:

```bash
python3 mailstore_webapi_export.py --no-interactive --workers 8
```

### Sistema lento, RAM piena, swap altissimo

Workers troppo alti. Killa con Ctrl+C, rilancia con meno worker:

```bash
# Verifica memoria/thread del processo Python
top -pid $(pgrep -n python3)

# Riparti con valore conservativo
python3 mailstore_webapi_export.py --workers 16
```

### Vuoi azzerare e ripartire da zero

```bash
# Cancella state.db (perdi il resume!)
rm -i /Volumes/Backup/eml_export/.mailstore_webapi_export/state.db

# Cancella TUTTO l'output (file + state)
rm -rf /Volumes/Backup/eml_export
```

### Controllare i log dell'ultima run

```bash
ls -lt /Volumes/Backup/eml_export/.mailstore_webapi_export/export_*.log | head -1
tail -100 $(ls -t /Volumes/Backup/eml_export/.mailstore_webapi_export/export_*.log | head -1)
```

---

## 8. Comandi diagnostici di rete

```bash
# Quali porte MailStore sono raggiungibili?
for p in 143 993 8460 8461 8462 8463 8474; do
  echo -n "Port $p: "
  nc -zv -G 2 127.0.0.1 $p 2>&1 | grep -E "succeeded|refused|timed out"
done

# Test rapido API
curl -k https://127.0.0.1:8462/

# Test login + GetArchives
TOKEN=$(curl -ks -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$MAILSTORE_PASSWORD\",\"trustedDeviceToken\":null}" \
  https://127.0.0.1:8462/api/authenticate/internal | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['tokenInformation']['access_token'])")
curl -k -H "Authorization: Bearer $TOKEN" https://127.0.0.1:8462/api/archives | \
  python3 -m json.tool | head -40
```




 I tre comandi che userai più spesso da adesso:

  # 1. Conta tutto senza scaricare (dovrebbe darti i ~3M che ti aspetti)
  python3 mailstore_webapi_export.py --analyze --analyze-all --no-interactive

  # 2. Trova il numero ottimo di worker (dopo aver killato il run a 1048)
  python3 mailstore_webapi_export.py --benchmark --no-interactive

  # 3. Export vero, una volta che hai il workers giusto in .env
  python3 mailstore_webapi_export.py
