#!/usr/bin/env python3
"""
MailStore IMAP Exporter
=======================
Esporta tutte le email da un server MailStore via IMAP in file .eml locali.

Caratteristiche:
- Resume automatico dopo crash (database SQLite di stato)
- Deduplica via Message-ID
- Parallelizzazione multi-thread (una connessione IMAP per worker)
- Log dettagliato + report finale
- Nome file con data + oggetto (sanitizzato)
- Path safety: tronca nomi lunghi, normalizza Unicode, rimuove caratteri proibiti
- Scrittura atomica (file temporaneo + rename) per evitare .eml corrotti
- Batch fetch IMAP per velocizzare
- Auto-reconnect su connessioni cadute

Uso:
    # Wizard interattivo (consigliato): chiede tutto a runtime, password mascherata
    python3 mailstore_export.py

    # Oppure modalità classica con flag (password chiesta se omessa)
    python3 mailstore_export.py --host 127.0.0.1 --user admin --output ./eml

Solo dipendenze stdlib Python 3.8+. Cross-platform (macOS, Linux, Windows).
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import getpass
import hashlib
import imaplib
import logging
import os
import queue
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# IMAP ha un limite di default di 10000 caratteri per riga di risposta:
# alziamo perché alcune email hanno header lunghissimi
imaplib._MAXLINE = 10_000_000

# ---------- Costanti ----------
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r'\s+')
MAX_FILENAME_LEN = 180       # lascia margine per estensione + collision suffix
MAX_PATH_COMPONENT = 200     # per ogni segmento di path
FETCH_BATCH_SIZE = 50        # quante email scarica per round-trip IMAP
PROGRESS_INTERVAL = 2.0      # secondi tra refresh progress bar


# ============================================================
# UTILITY
# ============================================================

def sanitize_component(name: str, max_len: int = MAX_PATH_COMPONENT) -> str:
    """Pulisce un singolo segmento di path per essere safe su APFS/NTFS/ext4."""
    if not name:
        return "_"
    # Normalizza Unicode in NFC (APFS preferisce NFC, evita problemi mojibake)
    name = unicodedata.normalize('NFC', name)
    # Rimuovi caratteri proibiti
    name = INVALID_CHARS.sub('_', name)
    # Collassa whitespace
    name = WHITESPACE.sub(' ', name).strip()
    # Rimuovi punti/spazi finali (problema su Windows ma buona pratica anche su Mac)
    name = name.rstrip('. ')
    if not name:
        return "_"
    # Tronca rispettando i byte UTF-8 (non i caratteri)
    encoded = name.encode('utf-8')
    if len(encoded) > max_len:
        encoded = encoded[:max_len]
        # ricostruisci senza spezzare un carattere multibyte
        name = encoded.decode('utf-8', errors='ignore').rstrip()
    return name or "_"


def parse_imap_date(date_str: str) -> datetime | None:
    """Parsa un header Date: in datetime. Restituisce None se non parsabile."""
    if not date_str:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt
    except (TypeError, ValueError):
        return None


def decode_subject(raw_subject: str) -> str:
    """Decodifica un subject MIME (=?utf-8?Q?...?=) in stringa pulita."""
    if not raw_subject:
        return ""
    try:
        decoded_parts = email.header.decode_header(raw_subject)
        result = []
        for part, charset in decoded_parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(charset or 'utf-8', errors='replace'))
                except (LookupError, TypeError):
                    result.append(part.decode('utf-8', errors='replace'))
            else:
                result.append(part)
        return ''.join(result)
    except Exception:
        return raw_subject if isinstance(raw_subject, str) else str(raw_subject)





def build_filename(date_dt: datetime | None, subject: str, msg_hash: str) -> str:
    """Costruisce il nome file: subject_YYYYMMDD_HHMMSS_hash8.eml"""
    if date_dt:
        date_part = date_dt.strftime('%Y%m%d_%H%M%S')
    else:
        date_part = "00000000_000000"

    subject_clean = sanitize_component(subject, max_len=120) if subject else "no_subject"
    hash_part = msg_hash[:8]

    filename = f"{subject_clean}_{date_part}_{hash_part}.eml"

    # truncate finale se ancora troppo lungo
    if len(filename.encode('utf-8')) > MAX_FILENAME_LEN:
        # mantieni date e hash, taglia subject
        budget = MAX_FILENAME_LEN - len(date_part) - len(hash_part) - len('__.eml')
        subject_clean = sanitize_component(subject_clean, max_len=max(20, budget))
        filename = f"{subject_clean}_{date_part}_{hash_part}.eml"
    return filename


def folder_to_path(imap_folder: str, output_root: Path) -> Path:
    """Converte una cartella IMAP (es. 'INBOX/Lavoro/2023') in path locale safe."""
    parts = imap_folder.replace('/', os.sep).split(os.sep)
    safe_parts = [sanitize_component(p) for p in parts if p]
    return output_root.joinpath(*safe_parts) if safe_parts else output_root


def parse_list_response(line: bytes) -> str | None:
    """
    Parsa una riga di risposta LIST IMAP, gestendo:
    - '(\\HasNoChildren) "/" "INBOX"'
    - '(\\HasNoChildren) "/" INBOX'
    - '(\\HasChildren) "." "Sent Items"'
    """
    if not line:
        return None
    try:
        s = line.decode('utf-8', errors='replace')
    except Exception:
        return None

    # Match: flags  delimiter  name
    # Il nome può essere quoted o no
    m = re.match(r'\([^)]*\)\s+("[^"]*"|\S+)\s+("(.+)"|(\S+))\s*$', s)
    if not m:
        return None
    name = m.group(3) if m.group(3) is not None else m.group(4)
    # Rimuovi escape IMAP (raddoppi di backslash e quote)
    name = name.replace('\\"', '"').replace('\\\\', '\\')
    return name


def parse_namespace_prefixes(raw: bytes) -> list[str]:
    """
    Parsa una risposta IMAP NAMESPACE e ritorna i prefissi dei namespace
    'other users' e 'shared' (skippando il personale).

    Esempio di risposta:
      NAMESPACE (("" "/")) (("Other Users/" "/")) (("Public Folders/" "/"))

    Cada namespace è NIL oppure una lista di coppie (prefix, delim).
    """
    if not raw:
        return []
    try:
        s = raw.decode('utf-8', errors='replace')
    except Exception:
        return []

    # Trova tutti i gruppi (( ... )), che corrispondono ai namespace non-NIL.
    # Il primo è personal, gli altri sono other/shared.
    groups = re.findall(r'\(\((.*?)\)\)', s)
    prefixes: list[str] = []
    for group in groups[1:]:  # skip personal
        # Ogni gruppo contiene coppie ("prefix" "delim") (("prefix2" "delim2"))
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', group)
        # Coppie: prefix, delim — prendi solo gli indici pari (prefix)
        for i in range(0, len(items), 2):
            prefix = items[i]
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


# ============================================================
# CONSOLE / UI HELPERS (cross-platform)
# ============================================================

IS_MACOS = sys.platform == 'darwin'
IS_WINDOWS = sys.platform.startswith('win')


def _supports_unicode() -> bool:
    """True se stdout può scrivere caratteri non-ASCII (UTF-8 o simili)."""
    enc = (getattr(sys.stdout, 'encoding', None) or '').lower()
    return 'utf' in enc or 'utf8' in enc


_UNICODE_OK = _supports_unicode()


def sym(unicode_char: str, ascii_fallback: str) -> str:
    """Restituisce il glifo unicode se la console lo supporta, altrimenti ASCII."""
    return unicode_char if _UNICODE_OK else ascii_fallback


def term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def hr(char: str = '-') -> str:
    return char * min(term_width(), 100)


def banner(title: str):
    w = min(term_width(), 100)
    print()
    print('=' * w)
    print(title.center(w))
    print('=' * w)


def section(title: str):
    print()
    print(sym('▸', '>') + f' {title}')
    print(hr('-'))


def _read_line(prompt: str) -> str:
    """Legge una riga gestendo Ctrl+C/Ctrl+D in modo pulito."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        print('Interrotto.')
        sys.exit(130)


def ask(prompt: str, default: str | None = None, allow_empty: bool = False) -> str:
    """Prompt testuale con default opzionale."""
    suffix = f' [{default}]' if default is not None else ''
    while True:
        val = _read_line(f'{prompt}{suffix}: ').strip()
        if not val:
            if default is not None:
                return default
            if allow_empty:
                return ''
            print('  Valore richiesto.')
            continue
        return val


def ask_int(prompt: str, default: int, min_val: int | None = None,
            max_val: int | None = None) -> int:
    while True:
        raw = _read_line(f'{prompt} [{default}]: ').strip()
        if not raw:
            return default
        try:
            n = int(raw)
        except ValueError:
            print('  Inserisci un numero intero.')
            continue
        if min_val is not None and n < min_val:
            print(f'  Minimo: {min_val}')
            continue
        if max_val is not None and n > max_val:
            print(f'  Massimo: {max_val}')
            continue
        return n


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = ' [Y/n]' if default else ' [y/N]'
    while True:
        raw = _read_line(f'{prompt}{suffix}: ').strip().lower()
        if not raw:
            return default
        if raw in ('y', 'yes', 's', 'si', 'sì'):
            return True
        if raw in ('n', 'no'):
            return False
        print('  Rispondi y o n.')


def ask_password(prompt: str = 'Password') -> str:
    """Password via getpass (mascherata). Loop finché non è non-vuota."""
    while True:
        try:
            pwd = getpass.getpass(f'{prompt}: ')
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if pwd:
            return pwd
        print('  Password non può essere vuota.')


def ask_path(prompt: str, default: str | None = None,
             must_be_writable: bool = True) -> Path:
    """Prompt per un path, espande ~ e risolve. Crea la dir se non esiste (con conferma)."""
    while True:
        raw = ask(prompt, default=default)
        p = Path(raw).expanduser().resolve()
        if p.exists():
            if not p.is_dir():
                print(f'  Esiste ma non è una directory: {p}')
                continue
            return p
        if ask_yes_no(f'  La cartella {p} non esiste. Crearla?', default=True):
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except OSError as e:
                print(f'  Impossibile creare: {e}')
                continue
        # ricicla


# ============================================================
# FOLDER SELECTION
# ============================================================

def parse_selection(spec: str, n_items: int) -> set[int]:
    """
    Parsa una stringa di selezione e restituisce gli indici 1-based selezionati.

    Formati supportati:
      'a' / 'all'         → tutti
      '1,3,5'             → singoli
      '1-5'               → range
      '1,3,5-9,12'        → mix
      'n' / 'none' / ''   → nessuno
    """
    spec = spec.strip().lower()
    if spec in ('', 'n', 'none'):
        return set()
    if spec in ('a', 'all', '*'):
        return set(range(1, n_items + 1))

    result: set[int] = set()
    for chunk in spec.split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        if '-' in chunk:
            a, _, b = chunk.partition('-')
            try:
                start = int(a)
                end = int(b)
            except ValueError:
                raise ValueError(f'Range non valido: {chunk!r}')
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= n_items:
                    result.add(i)
        else:
            try:
                i = int(chunk)
            except ValueError:
                raise ValueError(f'Numero non valido: {chunk!r}')
            if 1 <= i <= n_items:
                result.add(i)
    return result


def _print_folder_list(folders: list[str], selected: set[int], page: int,
                       page_size: int):
    total_pages = max(1, (len(folders) + page_size - 1) // page_size)
    start = page * page_size
    end = min(start + page_size, len(folders))
    print()
    print(f'  Pagina {page + 1}/{total_pages} '
          f'({len(folders)} cartelle, {len(selected)} selezionate)')
    print(hr('-'))
    width = len(str(len(folders)))
    for i in range(start, end):
        idx = i + 1
        mark = sym('●', '*') if idx in selected else sym('○', ' ')
        print(f'  [{mark}] {idx:>{width}}. {folders[i]}')
    print(hr('-'))


def select_folders_interactive(folders: list[str]) -> list[str]:
    """
    Menù interattivo per selezionare quali cartelle IMAP esportare.

    Comandi (case insensitive):
      <numeri>  selezione (es. '1,3,5-9'). Inverte lo stato dei target.
      a         seleziona tutto
      n         deseleziona tutto
      r REGEX   seleziona per regex (es. 'r ^INBOX')
      x REGEX   deseleziona per regex
      f         filtra la vista per regex
      c         azzera filtro vista
      p / >     pagina successiva
      b / <     pagina precedente
      l         mostra lista corrente
      ok        conferma e procede
      q         annulla
    """
    if not folders:
        print('Nessuna cartella disponibile.')
        return []

    selected: set[int] = set(range(1, len(folders) + 1))  # default: tutte
    view = folders[:]
    view_map = list(range(len(folders)))  # view[i] -> folders[view_map[i]]
    page = 0
    # Adattiamo al numero di righe del terminale (sottratto header/footer del menù).
    # Min 20, max 100. Su un terminale tipico (24-50 righe) → ~18-44 per pagina.
    try:
        rows = shutil.get_terminal_size((100, 30)).lines
    except Exception:
        rows = 30
    page_size = max(20, min(100, rows - 10))

    print()
    print('Seleziona le cartelle da esportare.')
    print('Comandi: numeri/range, a=tutte, n=nessuna, r REGEX, x REGEX,')
    print('         f REGEX (filtra vista), c (clear filtro), p/b (pag. su/giù),')
    print('         l (lista), ok (conferma), q (annulla)')

    while True:
        _print_folder_list(view, {view_map.index(i - 1) + 1
                                  for i in selected if (i - 1) in view_map},
                           page, page_size)
        try:
            cmd = _read_line('\nSelezione> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []

        if not cmd:
            continue

        low = cmd.lower()

        if low in ('q', 'quit', 'exit'):
            if ask_yes_no('Annullare la selezione e uscire?', default=False):
                sys.exit(0)
            continue

        if low == 'ok':
            chosen = sorted(selected)
            if not chosen:
                if not ask_yes_no('Nessuna cartella selezionata. Confermi?',
                                  default=False):
                    continue
            return [folders[i - 1] for i in chosen]

        if low in ('p', '>', 'next'):
            total_pages = max(1, (len(view) + page_size - 1) // page_size)
            page = min(page + 1, total_pages - 1)
            continue
        if low in ('b', '<', 'prev'):
            page = max(0, page - 1)
            continue
        if low == 'l':
            page = 0
            continue
        if low == 'a':
            selected = set(range(1, len(folders) + 1))
            print(f'  Tutte selezionate ({len(folders)}).')
            continue
        if low == 'n':
            selected.clear()
            print('  Tutte deselezionate.')
            continue
        if low == 'c':
            view = folders[:]
            view_map = list(range(len(folders)))
            page = 0
            print('  Filtro vista rimosso.')
            continue

        # Comandi con argomento: 'r REGEX', 'x REGEX', 'f REGEX'
        if len(cmd) >= 2 and cmd[0].lower() in ('r', 'x', 'f') and cmd[1] == ' ':
            op = cmd[0].lower()
            pattern = cmd[2:].strip()
            if not pattern:
                print('  Pattern mancante.')
                continue
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                print(f'  Regex non valida: {e}')
                continue
            if op == 'r':
                added = 0
                for i, name in enumerate(folders, start=1):
                    if rx.search(name) and i not in selected:
                        selected.add(i)
                        added += 1
                print(f'  Selezionate {added} cartelle aggiuntive.')
            elif op == 'x':
                removed = 0
                for i, name in enumerate(folders, start=1):
                    if rx.search(name) and i in selected:
                        selected.discard(i)
                        removed += 1
                print(f'  Deselezionate {removed} cartelle.')
            else:  # f
                view_map = [i for i, name in enumerate(folders) if rx.search(name)]
                view = [folders[i] for i in view_map]
                page = 0
                if not view:
                    print('  Nessuna cartella matcha il filtro.')
                else:
                    print(f'  Filtro applicato: {len(view)} cartelle visibili.')
            continue

        # Selezione numerica → toggle dello stato
        try:
            picked = parse_selection(cmd, len(view))
        except ValueError as e:
            print(f'  {e}. Riprova.')
            continue
        if not picked:
            continue
        toggled_on = toggled_off = 0
        for view_idx in picked:
            real_idx = view_map[view_idx - 1] + 1
            if real_idx in selected:
                selected.discard(real_idx)
                toggled_off += 1
            else:
                selected.add(real_idx)
                toggled_on += 1
        print(f'  Toggle: +{toggled_on} / -{toggled_off}. '
              f'Totale selezionate: {len(selected)}.')


# ============================================================
# WIZARD
# ============================================================

def run_wizard(defaults: dict) -> dict:
    """
    Wizard interattivo che raccoglie tutta la config necessaria.
    `defaults` può contenere valori precompilati (da CLI args).
    Ritorna un dict pronto per main().
    """
    banner('MailStore Export — Wizard interattivo')

    # ---- Step 1: connessione ----
    section('1/4 — Connessione IMAP')
    host = ask('Host MailStore', default=defaults.get('host') or '127.0.0.1')
    use_ssl = ask_yes_no('Usare IMAPS (SSL/TLS)?',
                        default=bool(defaults.get('ssl')))
    default_port = defaults.get('port') or (993 if use_ssl else 143)
    port = ask_int('Porta IMAP', default=default_port, min_val=1, max_val=65535)
    user = ask('Username', default=defaults.get('user'))
    password = defaults.get('password') or ask_password('Password')

    print()
    print(f'  Verifico la connessione a {host}:{port} (timeout 15s)...')
    test_conn = ImapConnection(host=host, port=port, user=user,
                               password=password, use_ssl=use_ssl,
                               timeout=15.0)
    try:
        try:
            test_conn.connect()
            folders = test_conn.list_folders()
        except KeyboardInterrupt:
            print()
            print('  Interrotto durante la connessione.')
            sys.exit(130)
        except (OSError, TimeoutError) as e:
            print(f'  {sym("✗", "X")} Connessione fallita: {e}')
            print()
            print('  Controlla:')
            print(f'    - host/porta corretti? (ora: {host}:{port})')
            print(f'    - MailStore Server è raggiungibile da questo Mac?')
            print(f'    - se il server è remoto, hai una VPN/route attiva?')
            print(f'    - se è in locale, MailStore Server è in esecuzione e ha IMAP attivo?')
            sys.exit(2)
        except imaplib.IMAP4.error as e:
            print(f'  {sym("✗", "X")} Login IMAP fallito: {e}')
            print('  Verifica username e password.')
            sys.exit(2)
        except Exception as e:
            print(f'  {sym("✗", "X")} Errore: {e}')
            sys.exit(2)
    finally:
        try:
            test_conn.close()
        except Exception:
            pass
    print(f'  {sym("✓", "OK")} Connesso. Trovate {len(folders)} cartelle.')

    # ---- Step 2: selezione cartelle ----
    section('2/4 — Selezione cartelle')
    chosen_folders = select_folders_interactive(folders)
    if not chosen_folders:
        print('Nessuna cartella selezionata. Esco.')
        sys.exit(0)

    # ---- Step 3: output ----
    section('3/4 — Cartella di output')
    default_out = defaults.get('output') or str(Path.cwd() / 'mailstore_export')
    output = ask_path('Path cartella output', default=default_out)

    # ---- Step 4: opzioni ----
    section('4/4 — Opzioni')
    workers = ask_int('Numero worker paralleli (= connessioni IMAP)',
                      default=defaults.get('workers') or 4,
                      min_val=1, max_val=32)
    dedup = ask_yes_no('Deduplicare via Message-ID (salta duplicati cross-folder)?',
                       default=bool(defaults.get('dedup_message_id')))

    # ---- Riepilogo ----
    section('Riepilogo')
    print(f'  Host        : {host}:{port} ({"SSL" if use_ssl else "plain"})')
    print(f'  Utente      : {user}')
    print(f'  Output      : {output}')
    print(f'  Cartelle    : {len(chosen_folders)} selezionate')
    print(f'  Workers     : {workers}')
    print(f'  Dedup Msg-ID: {"sì" if dedup else "no"}')
    print()
    if not ask_yes_no('Avviare l\'export?', default=True):
        print('Annullato.')
        sys.exit(0)

    return {
        'host': host, 'port': port, 'user': user, 'password': password,
        'ssl': use_ssl, 'output': output, 'workers': workers,
        'dedup_message_id': dedup, 'selected_folders': chosen_folders,
    }


# ============================================================
# STATE DATABASE
# ============================================================

class StateDB:
    """SQLite database per resume e deduplica.

    Una connessione per thread (sqlite3 non è thread-safe per default).
    """
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        # init schema
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS exported (
                msg_hash TEXT PRIMARY KEY,
                folder TEXT NOT NULL,
                uid INTEGER NOT NULL,
                message_id TEXT,
                filename TEXT,
                size INTEGER,
                exported_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_message_id ON exported(message_id);
            CREATE INDEX IF NOT EXISTS idx_folder_uid ON exported(folder, uid);

            CREATE TABLE IF NOT EXISTS folders (
                folder TEXT PRIMARY KEY,
                total_uids INTEGER,
                completed INTEGER DEFAULT 0,
                last_update REAL
            );
        """)
        conn.commit()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def is_exported(self, msg_hash: str) -> bool:
        cur = self._connect().execute(
            "SELECT 1 FROM exported WHERE msg_hash = ? LIMIT 1", (msg_hash,)
        )
        return cur.fetchone() is not None

    def has_message_id(self, message_id: str) -> bool:
        if not message_id:
            return False
        cur = self._connect().execute(
            "SELECT 1 FROM exported WHERE message_id = ? LIMIT 1", (message_id,)
        )
        return cur.fetchone() is not None

    def mark_exported(self, msg_hash: str, folder: str, uid: int,
                      message_id: str, filename: str, size: int):
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO exported "
            "(msg_hash, folder, uid, message_id, filename, size, exported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_hash, folder, uid, message_id, filename, size, time.time())
        )
        conn.commit()

    def get_exported_uids_for_folder(self, folder: str) -> set[int]:
        cur = self._connect().execute(
            "SELECT uid FROM exported WHERE folder = ?", (folder,)
        )
        return {row[0] for row in cur.fetchall()}

    def set_folder_total(self, folder: str, total: int):
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO folders (folder, total_uids, last_update) "
            "VALUES (?, ?, ?)",
            (folder, total, time.time())
        )
        conn.commit()

    def stats(self) -> dict:
        conn = self._connect()
        cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM exported")
        count, total_size = cur.fetchone()
        return {'count': count or 0, 'total_size': total_size or 0}


# ============================================================
# PROGRESS REPORTER
# ============================================================

class Progress:
    """Progress reporter thread-safe con refresh periodico."""
    def __init__(self):
        self.lock = threading.Lock()
        self.total_folders = 0
        self.done_folders = 0
        self.total_msgs = 0
        self.done_msgs = 0
        self.skipped_msgs = 0
        # Skip breakdown by reason (used by the Web API export report; the IMAP
        # export leaves them at 0). inc() only touches existing attributes.
        self.skip_resume = 0      # già esportate (resume da state.db)
        self.skip_excluded = 0    # in skip-list (--exclude-failed / --exclude-from)
        self.skip_dedup = 0       # duplicato Message-ID (--dedup-message-id)
        self.skip_tmp = 0         # .eml.tmp orfano (lock/crash precedente)
        self.skip_filter = 0      # escluso dal filtro anno/mese (--year/--month)
        self.failed_msgs = 0
        self.bytes_written = 0
        self.current_folder = ""
        self.start_time = time.time()
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2.0)
        self._render(final=True)

    def update(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def inc(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, getattr(self, k) + v)

    def _loop(self):
        while not self._stop:
            time.sleep(PROGRESS_INTERVAL)
            self._render()

    def _render(self, final: bool = False):
        with self.lock:
            elapsed = time.time() - self.start_time
            rate = self.done_msgs / elapsed if elapsed > 0 else 0
            mb = self.bytes_written / (1024 * 1024)
            mb_per_s = mb / elapsed if elapsed > 0 else 0

            pct = (self.done_msgs / self.total_msgs * 100) if self.total_msgs else 0

            line = (
                f"\r[{self.done_folders}/{self.total_folders} folders] "
                f"{self.done_msgs:,}/{self.total_msgs:,} msg ({pct:5.1f}%) | "
                f"skip:{self.skipped_msgs:,} fail:{self.failed_msgs:,} | "
                f"{mb:,.0f}MB ({mb_per_s:,.1f}MB/s, {rate:,.0f}msg/s) | "
                f"{self.current_folder[:40]}"
            )
            # padding per coprire la riga precedente
            sys.stdout.write(line.ljust(120))
            sys.stdout.flush()
            if final:
                sys.stdout.write('\n')


# ============================================================
# IMAP WORKER
# ============================================================

class ImapConnection:
    """Wrapper con auto-reconnect su connessioni IMAP cadute."""
    def __init__(self, host: str, port: int, user: str, password: str,
                 use_ssl: bool, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.conn: imaplib.IMAP4 | None = None
        self.current_folder: str | None = None

    def connect(self):
        if self.use_ssl:
            self.conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        else:
            self.conn = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
        self.conn.login(self.user, self.password)

    def reconnect(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None
        time.sleep(1.0)
        self.connect()
        if self.current_folder:
            self.conn.select(self._quote(self.current_folder), readonly=True)

    @staticmethod
    def _quote(folder: str) -> str:
        # IMAP folder name: quote + escape backslash e quote interne
        escaped = folder.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'

    def select_folder(self, folder: str) -> int:
        """Seleziona cartella in readonly, restituisce il numero di messaggi."""
        self.current_folder = folder
        status, data = self.conn.select(self._quote(folder), readonly=True)
        if status != 'OK':
            raise RuntimeError(f"SELECT failed for {folder}: {data}")
        try:
            return int(data[0])
        except (ValueError, IndexError):
            return 0

    def list_folders(self) -> list[str]:
        """
        Lista tutte le cartelle IMAP, includendo i namespace non-personali
        (Other Users / Shared / Public). MailStore e Exchange-like espongono gli
        archivi degli altri utenti sotto namespace separati e il classico
        LIST "" "*" restituisce solo il namespace personale.
        """
        seen: set[str] = set()
        folders: list[str] = []

        def _add_from_list(reference: str, pattern: str):
            try:
                status, lines = self.conn.list(reference, pattern)
            except Exception:
                return
            if status != 'OK' or not lines:
                return
            for line in lines:
                # Le risposte LIST possono essere singole bytes o tuple su alcuni server
                if isinstance(line, tuple):
                    line = line[0]
                if not isinstance(line, (bytes, bytearray)):
                    continue
                name = parse_list_response(bytes(line))
                if name and name not in seen:
                    seen.add(name)
                    folders.append(name)

        # 1) Namespace personale (LIST classico)
        _add_from_list('""', '"*"')

        # 2) Discovery namespace addizionali via NAMESPACE
        extra_prefixes: list[str] = []
        try:
            status, ns_data = self.conn.namespace()
            if status == 'OK' and ns_data:
                raw = ns_data[0] if isinstance(ns_data, (list, tuple)) else ns_data
                if isinstance(raw, (bytes, bytearray)):
                    extra_prefixes = parse_namespace_prefixes(bytes(raw))
        except Exception:
            extra_prefixes = []

        # Se NAMESPACE non funziona o non ritorna nulla, prova nomi comuni
        if not extra_prefixes:
            extra_prefixes = [
                'Other Users/', 'Other Archives/', 'Public Folders/',
                'Public/', 'Shared/', 'shared/', 'Archivi/',
            ]

        for prefix in extra_prefixes:
            # Quote il reference: anche prefissi con spazi devono essere tra virgolette
            ref = f'"{prefix}"'
            _add_from_list(ref, '"*"')

        if not folders:
            raise RuntimeError("LIST: nessuna cartella restituita")
        return folders

    def search_all_uids(self) -> list[int]:
        status, data = self.conn.uid('search', None, 'ALL')
        if status != 'OK':
            return []
        return [int(x) for x in data[0].split()] if data and data[0] else []

    def fetch_batch(self, uids: list[int]) -> dict[int, bytes]:
        """Scarica un batch di email per UID. Restituisce dict {uid: raw_bytes}."""
        if not uids:
            return {}
        uid_set = ','.join(str(u) for u in uids)
        status, data = self.conn.uid('fetch', uid_set, '(RFC822)')
        if status != 'OK':
            raise RuntimeError(f"FETCH failed: {data}")

        result = {}
        # imaplib restituisce alternanze: tuple(header, body), poi b')', ecc.
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            header_part = item[0]
            body_part = item[1]
            if not isinstance(header_part, bytes) or not isinstance(body_part, bytes):
                continue
            m = re.search(rb'UID\s+(\d+)', header_part)
            if not m:
                continue
            uid = int(m.group(1))
            result[uid] = body_part
        return result

    def close(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None


def process_folder(
    folder: str,
    output_root: Path,
    state: StateDB,
    progress: Progress,
    cfg: dict,
    stop_event: threading.Event,
    logger: logging.Logger,
):
    """Esporta tutte le email di una cartella IMAP."""
    if stop_event.is_set():
        return

    conn = ImapConnection(cfg['host'], cfg['port'], cfg['user'],
                          cfg['password'], cfg['ssl'])
    try:
        conn.connect()
    except Exception as e:
        logger.error(f"Connessione IMAP fallita per cartella {folder!r}: {e}")
        return

    # Ricrea la gerarchia IMAP su filesystem; la sottocartella per anno
    # viene aggiunta a livello del singolo messaggio in save_email().
    local_folder = folder_to_path(folder, output_root)

    progress.update(current_folder=folder)
    logger.info(f"Inizio cartella: {folder!r} -> {local_folder}")

    try:
        total = conn.select_folder(folder)
    except Exception as e:
        logger.error(f"SELECT fallito per {folder!r}: {e}")
        conn.close()
        return

    if total == 0:
        logger.info(f"Cartella vuota: {folder!r}")
        progress.inc(done_folders=1)
        conn.close()
        return

    try:
        uids = conn.search_all_uids()
    except Exception as e:
        logger.error(f"SEARCH fallito per {folder!r}: {e}")
        conn.close()
        return

    state.set_folder_total(folder, len(uids))
    already_done = state.get_exported_uids_for_folder(folder)
    todo = [u for u in uids if u not in already_done]

    progress.inc(total_msgs=len(uids), skipped_msgs=len(already_done),
                 done_msgs=len(already_done))

    if not todo:
        logger.info(f"Cartella già completa: {folder!r} ({len(uids)} msg)")
        progress.inc(done_folders=1)
        conn.close()
        return

    logger.info(f"Cartella {folder!r}: {len(todo)}/{len(uids)} da esportare")

    # Processa in batch
    consecutive_errors = 0
    i = 0
    while i < len(todo) and not stop_event.is_set():
        batch = todo[i:i + FETCH_BATCH_SIZE]
        try:
            fetched = conn.fetch_batch(batch)
        except (imaplib.IMAP4.abort, OSError, RuntimeError) as e:
            consecutive_errors += 1
            logger.warning(f"FETCH batch error in {folder!r} (try {consecutive_errors}): {e}")
            if consecutive_errors >= 5:
                logger.error(f"Troppi errori consecutivi in {folder!r}, skip cartella")
                break
            try:
                conn.reconnect()
            except Exception as re_err:
                logger.error(f"Reconnect fallito: {re_err}")
                time.sleep(5.0)
            continue

        consecutive_errors = 0

        for uid in batch:
            if stop_event.is_set():
                break
            raw = fetched.get(uid)
            if raw is None:
                logger.warning(f"UID {uid} in {folder!r}: nessun dato")
                progress.inc(failed_msgs=1)
                continue

            try:
                save_email(raw, folder, uid, local_folder, state, cfg, logger)
                progress.inc(done_msgs=1, bytes_written=len(raw))
            except Exception as e:
                logger.error(f"Save fallito UID {uid} in {folder!r}: {e}")
                progress.inc(failed_msgs=1)

        i += FETCH_BATCH_SIZE

    progress.inc(done_folders=1)
    conn.close()
    logger.info(f"Cartella completata: {folder!r}")


def save_email(raw: bytes, folder: str, uid: int, local_folder: Path,
               state: StateDB, cfg: dict, logger: logging.Logger):
    """Salva una email come .eml. Idempotente + atomico."""
    msg_hash = hashlib.sha256(raw).hexdigest()

    if state.is_exported(msg_hash):
        return

    # Parse minimale per estrarre Date/Subject/Message-ID
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        date_header = msg.get('Date', '')
        subject_raw = msg.get('Subject', '')
        message_id = (msg.get('Message-ID') or '').strip()
    except Exception:
        date_header = ''
        subject_raw = ''
        message_id = ''

    # Dedup via Message-ID (opzionale)
    if cfg.get('dedup_message_id') and message_id and state.has_message_id(message_id):
        state.mark_exported(msg_hash, folder, uid, message_id,
                            filename='(dup-skipped)', size=len(raw))
        return

    date_dt = parse_imap_date(date_header)
    subject = decode_subject(subject_raw)

    # Sottocartella per anno (sempre attiva): YYYY oppure 0000 se data mancante.
    year_dir = f"{date_dt.year:04d}" if date_dt else "0000"
    local_folder = local_folder / year_dir
    local_folder.mkdir(parents=True, exist_ok=True)

    filename = build_filename(date_dt, subject, msg_hash)
    target = local_folder / filename

    # Collisione (rara, ma può succedere se due msg hanno stessa data+subject+hash[:8])
    if target.exists():
        # Aggiungi un suffisso incrementale
        n = 1
        while True:
            stem = target.stem
            alt = local_folder / f"{stem}_{n}.eml"
            if not alt.exists():
                target = alt
                break
            n += 1
            if n > 1000:
                raise RuntimeError(f"Troppe collisioni per {filename}")

    # Scrittura atomica: tmp + rename
    tmp = target.with_suffix('.eml.tmp')
    try:
        with open(tmp, 'wb') as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError as e:
        # Cleanup tmp se è rimasto
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    state.mark_exported(msg_hash, folder, uid, message_id,
                        filename=target.name,
                        size=len(raw))


# ============================================================
# MAIN
# ============================================================

def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger('mailstore_export')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s'
    ))
    logger.addHandler(fh)
    # niente stream handler: la progress bar usa stdout
    return logger


def _run_diag(args):
    """
    Modalità diagnostica: connetti a IMAP e stampa output raw dei comandi
    standard. Serve per capire cosa il server espone davvero.
    """
    banner('MailStore — Diagnostica IMAP')
    host = args.host or ask('Host', default='127.0.0.1')
    use_ssl = args.ssl if args.ssl else ask_yes_no('IMAPS (SSL/TLS)?', default=True)
    default_port = args.port or (993 if use_ssl else 143)
    port = args.port or ask_int('Porta', default=default_port, min_val=1, max_val=65535)
    user = args.user or ask('Username')
    password = args.password or ask_password()

    print()
    print(f'Connetto a {host}:{port} (ssl={use_ssl})...')
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port, timeout=15.0)
    else:
        conn = imaplib.IMAP4(host, port, timeout=15.0)
    conn.login(user, password)
    print('Login OK.')

    def _dump(label: str, fn, *fargs):
        print()
        print(hr('='))
        print(f'== {label}  -> {fargs!r}')
        print(hr('='))
        try:
            status, data = fn(*fargs)
            print(f'status: {status}')
            for i, line in enumerate(data or []):
                print(f'  [{i}] {line!r}')
        except Exception as e:
            print(f'ERROR: {e!r}')

    _dump('CAPABILITY', conn.capability)
    _dump('NAMESPACE', conn.namespace)
    _dump('LIST "" "*"', conn.list, '""', '"*"')
    _dump('LIST "" "%"', conn.list, '""', '"%"')
    _dump('LIST "*" "*"', conn.list, '"*"', '"*"')
    _dump('LSUB "" "*"', conn.lsub, '""', '"*"')

    # Prova prefissi comuni
    for prefix in ('Other Users/', 'Other Archives/', 'Public Folders/',
                   'Public/', 'Shared/', 'Archivi/', 'Archive/'):
        _dump(f'LIST "{prefix}" "*"', conn.list, f'"{prefix}"', '"*"')

    print()
    print(hr('='))
    print('Diagnostica completata. Copia/incolla l\'output qua sopra per analisi.')
    try:
        conn.logout()
    except Exception:
        pass


def _is_interactive_tty() -> bool:
    """True se stdin/stdout sono un TTY (utente reale, non pipe/cron)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Export MailStore IMAP -> file .eml locali",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Lanciato senza argomenti (o con -i) avvia il wizard interattivo."
    )
    parser.add_argument('--host', default=None, help='IMAP host MailStore')
    parser.add_argument('--port', type=int, default=None, help='IMAP port')
    parser.add_argument('--user', default=None, help='Username MailStore')
    parser.add_argument('--password', default=None,
                        help='Password MailStore (se omessa viene chiesta a runtime)')
    parser.add_argument('--ssl', action='store_true', help='Usa IMAPS (porta tipica 993)')
    parser.add_argument('--output', default=None, type=Path,
                        help='Cartella di output per i .eml (default: ./mailstore_export)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Numero di thread paralleli (= connessioni IMAP)')
    parser.add_argument('--dedup-message-id', action='store_true',
                        help='Salta email con Message-ID già visto (deduplica cross-folder)')
    parser.add_argument('--include', action='append', default=[],
                        help='Pattern regex per INCLUDERE solo certe cartelle (multi)')
    parser.add_argument('--exclude', action='append', default=[],
                        help='Pattern regex per ESCLUDERE certe cartelle (multi)')
    parser.add_argument('--list-only', action='store_true',
                        help='Elenca solo le cartelle e termina')
    parser.add_argument('-i', '--interactive', action='store_true',
                        help='Forza il wizard interattivo')
    parser.add_argument('--no-interactive', action='store_true',
                        help='Disabilita il wizard (utile per script/cron)')
    parser.add_argument('--diag', action='store_true',
                        help='Diagnostica IMAP: stampa CAPABILITY, NAMESPACE, '
                             'LIST e LSUB raw e termina')
    args = parser.parse_args()

    if args.diag:
        _run_diag(args)
        return

    # Decide se entrare nel wizard:
    # - sempre se --interactive
    # - mai se --no-interactive
    # - altrimenti: se mancano user/output e abbiamo un TTY, sì
    missing_required = args.user is None or args.output is None
    use_wizard = args.interactive or (
        missing_required and not args.no_interactive and _is_interactive_tty()
    )

    if use_wizard:
        wiz = run_wizard({
            'host': args.host, 'port': args.port, 'user': args.user,
            'password': args.password, 'ssl': args.ssl,
            'output': str(args.output) if args.output else None,
            'workers': args.workers,
            'dedup_message_id': args.dedup_message_id,
        })
        host = wiz['host']
        port = wiz['port']
        user = wiz['user']
        password = wiz['password']
        use_ssl = wiz['ssl']
        output_root = Path(wiz['output'])
        workers = wiz['workers']
        dedup = wiz['dedup_message_id']
        selected_folders: list[str] | None = wiz['selected_folders']
    else:
        # Modalità non interattiva: tutti i parametri devono esserci.
        if missing_required:
            print('ERRORE: --user e --output sono richiesti in modalità non interattiva.',
                  file=sys.stderr)
            sys.exit(2)
        host = args.host or '127.0.0.1'
        port = args.port or (993 if args.ssl else 143)
        user = args.user
        # Password: se mancante prova a chiederla via getpass (anche in modalità "classica").
        password = args.password
        if not password:
            if _is_interactive_tty():
                password = ask_password(f'Password per {user}@{host}')
            else:
                print('ERRORE: --password mancante e nessun TTY disponibile.',
                      file=sys.stderr)
                sys.exit(2)
        use_ssl = args.ssl
        output_root = args.output
        workers = args.workers
        dedup = args.dedup_message_id
        selected_folders = None  # sarà determinato dai filtri include/exclude

    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    # Spotlight marker / Time Machine: solo su macOS
    if IS_MACOS:
        spotlight_marker = output_root / '.metadata_never_index'
        if not spotlight_marker.exists():
            try:
                spotlight_marker.touch()
            except OSError:
                pass

    state_dir = output_root / '.mailstore_export'
    state_dir.mkdir(exist_ok=True)
    db_path = state_dir / 'state.db'
    log_path = state_dir / f'export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

    logger = setup_logging(log_path)
    logger.info("=" * 60)
    logger.info(f"Avvio export: host={host}:{port} ssl={use_ssl} "
                f"workers={workers} output={output_root}")

    state = StateDB(db_path)
    cfg = {
        'host': host, 'port': port,
        'user': user, 'password': password,
        'ssl': use_ssl, 'dedup_message_id': dedup,
    }

    if selected_folders is not None:
        # Wizard ha già selezionato le cartelle e la connessione è stata testata.
        folders = selected_folders
    else:
        # Modalità non interattiva: lista cartelle e applica filtri include/exclude.
        setup_conn = ImapConnection(host=host, port=port, user=user,
                                    password=password, use_ssl=use_ssl)
        try:
            setup_conn.connect()
        except Exception as e:
            print(f"\nERRORE: connessione IMAP fallita: {e}", file=sys.stderr)
            sys.exit(2)
        try:
            folders = setup_conn.list_folders()
        finally:
            setup_conn.close()

        if args.include:
            inc_re = [re.compile(p) for p in args.include]
            folders = [f for f in folders if any(r.search(f) for r in inc_re)]
        if args.exclude:
            exc_re = [re.compile(p) for p in args.exclude]
            folders = [f for f in folders if not any(r.search(f) for r in exc_re)]

        print(f"\nTrovate {len(folders)} cartelle IMAP da processare:\n")
        for f in folders[:50]:
            print(f"  - {f}")
        if len(folders) > 50:
            print(f"  ... e altre {len(folders) - 50}")
        print()

    if args.list_only:
        return

    # Setup signal handlers per shutdown pulito
    stop_event = threading.Event()
    warn = sym('⚠', '!')
    def handle_sig(signum, frame):
        if not stop_event.is_set():
            print(f"\n\n{warn}  Interruzione richiesta, completo i batch in corso e chiudo...\n",
                  file=sys.stderr)
            stop_event.set()
        else:
            print(f"\n{warn}  Seconda interruzione, exit forzato.\n", file=sys.stderr)
            os._exit(130)
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    progress = Progress()
    progress.update(total_folders=len(folders))
    progress.start()

    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers,
                                 thread_name_prefix='worker') as pool:
            futures = [
                pool.submit(process_folder, folder, output_root, state,
                            progress, cfg, stop_event, logger)
                for folder in folders
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    logger.exception(f"Worker exception: {e}")
    finally:
        progress.stop()

    elapsed = time.time() - t0
    stats = state.stats()
    print()
    print("=" * 60)
    print("REPORT FINALE")
    print("=" * 60)
    print(f"Tempo totale:          {elapsed/60:,.1f} minuti ({elapsed:,.0f}s)")
    print(f"Cartelle processate:   {progress.done_folders}/{progress.total_folders}")
    print(f"Messaggi esportati:    {progress.done_msgs:,}")
    print(f"Messaggi saltati:      {progress.skipped_msgs:,} (già presenti)")
    print(f"Messaggi falliti:      {progress.failed_msgs:,}")
    print(f"Volume totale in DB:   {stats['total_size'] / (1024**3):,.2f} GB "
          f"({stats['count']:,} email)")
    print(f"Velocità media:        {progress.done_msgs/elapsed if elapsed else 0:,.0f} msg/s")
    print(f"Log file:              {log_path}")
    print(f"State DB:              {db_path}")
    print()
    if stop_event.is_set():
        print(f"{warn}  Export interrotto. Rilancia lo stesso comando per riprendere.")
    else:
        print(f"{sym('✓', 'OK')}  Export completato.")
    logger.info("Export terminato.")


if __name__ == '__main__':
    main()
