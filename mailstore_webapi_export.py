#!/usr/bin/env python3
"""
MailStore Web API Exporter
==========================
Esporta email da MailStore Server via la Web Access REST API (porta 8462 default).
A differenza dell'export IMAP (mailstore_export.py), accede a TUTTI gli archivi
visibili all'utente compliance, non solo a quello dell'utente IMAP loggato.

Auth: Bearer token OAuth2 (da incollare a inizio export — validità ~1h).
Per estrarlo dal browser:
  1. Logga in MailStore Web Access (https://HOST:8462/app/)
  2. DevTools → Network → seleziona una qualunque request /api/...
  3. Header "Authorization: Bearer XXXX" → copia XXXX

Stdlib only. Python 3.8+. Cross-platform.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import getpass
import hashlib
import http.cookiejar
import json
import logging
import os
import random
import re
import signal
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Riusa utility dallo script IMAP (sanitize, build_filename, ecc.)
try:
    from mailstore_export import (
        sanitize_component, build_filename, decode_subject, parse_imap_date,
        StateDB, Progress, sym, hr, banner, section, ask, ask_int, ask_yes_no,
        ask_password, ask_path, parse_selection, select_folders_interactive,
        _is_interactive_tty, IS_MACOS, MAX_FILENAME_LEN,
    )
except ImportError as e:
    print(f"ERRORE: mailstore_export.py deve essere nella stessa cartella. ({e})",
          file=sys.stderr)
    sys.exit(1)


# ============================================================
# .ENV LOADER
# ============================================================

def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Carica un file .env stile KEY=VALUE.

    Supporta:
      - commenti con #
      - righe vuote
      - valori tra virgolette singole/doppie
      - prefissi tipo `export KEY=VAL` (per compat con shell)

    Variabili riconosciute dallo script:
      MAILSTORE_HOST, MAILSTORE_PORT, MAILSTORE_USER, MAILSTORE_PASSWORD,
      MAILSTORE_TOKEN, MAILSTORE_OUTPUT, MAILSTORE_WORKERS
    """
    if path is None:
        path = Path(__file__).resolve().parent / '.env'
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            v = v.strip()
            # Strip surrounding quotes
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                env[k] = v
    except OSError:
        return {}
    return env


# ============================================================
# WEB API CLIENT
# ============================================================

class ApiError(Exception):
    """Errore generico dell'API MailStore."""
    def __init__(self, status: int, message: str, body: bytes = b''):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status
        self.message = message
        self.body = body


class AuthExpired(ApiError):
    """Token scaduto (HTTP 401)."""
    pass


class WebClient:
    """Client HTTP per la Web Access API di MailStore.

    Supporta due modalità di auth:
      - Bearer token statico (passa `token`)
      - Username/password con auto-refresh (passa `username` + `password`)

    I token MailStore scadono in 300s (5 min). Con user/password il client si
    rilogga automaticamente quando il token sta per scadere o riceve un 401.
    """

    def __init__(self, base_url: str, token: str | None = None,
                 username: str | None = None, password: str | None = None,
                 verify_tls: bool = False, timeout: float = 60.0):
        if not token and not (username and password):
            raise ValueError('Servono token oppure username+password')
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.username = username
        self.password = password
        self.timeout = timeout
        self._local = threading.local()  # opener per-thread
        self._verify_tls = verify_tls
        # auto-refresh state
        self._expires_at: float = 0.0  # epoch seconds
        self._auth_lock = threading.Lock()

    def authenticate(self) -> None:
        """Esegue POST /api/authenticate/internal con username+password.

        Aggiorna self.token e self._expires_at. Thread-safe.
        Solleva AuthExpired se le credenziali sono sbagliate.
        """
        if not (self.username and self.password):
            raise AuthExpired(401, 'Nessuna credenziale per il refresh', b'')
        body = {
            'username': self.username,
            'password': self.password,
            'trustedDeviceToken': None,
        }
        data = json.dumps(body).encode('utf-8')
        url = self.base_url + '/api/authenticate/internal'
        ctx = (ssl.create_default_context() if self._verify_tls
               else ssl._create_unverified_context())
        # Usiamo un opener fresco (auth è stateless con Bearer dopo)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
        )
        # MailStore Web API richiede alcuni header che il browser invia sempre
        opener.addheaders = [
            ('User-Agent', 'Mozilla/5.0 mailstore-webapi-export/1.0'),
            ('Accept', 'application/json, text/plain, */*'),
            ('Accept-Language', 'en-US,en;q=0.9'),
            ('Origin', self.base_url),
            ('Referer', f'{self.base_url}/app/login'),
            ('ms-apiurl', f'{self.base_url}/api'),
        ]
        # Content-Type va sulla request (POST con body), non sull'opener
        req = urllib.request.Request(
            url, data=data, method='POST',
            headers={'Content-Type': 'application/json'},
        )
        try:
            resp = opener.open(req, timeout=self.timeout)
            raw = resp.read()
            payload = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read()
            err_msg = err_body.decode('utf-8', errors='replace')[:400]
            try:
                j = json.loads(err_body)
                err_msg = j.get('message') or err_msg
            except Exception:
                pass
            raise AuthExpired(e.code,
                              f'Login fallito (HTTP {e.code}): {err_msg}',
                              err_body)
        except (urllib.error.URLError, OSError) as e:
            raise ApiError(0, f'Errore di rete al login: {e}', b'')

        ti = payload.get('tokenInformation') or {}
        tok = ti.get('access_token')
        ttl = ti.get('expires_in', 300)
        if not tok:
            raise AuthExpired(0, f'Login senza access_token. '
                                 f'Risposta: {payload}', b'')
        if payload.get('mfaCodeRequired'):
            raise AuthExpired(0, 'MFA richiesta — non supportato in questo script',
                              b'')
        self.token = tok
        # margine di 30s per refresh anticipato
        self._expires_at = time.time() + max(60, ttl - 30)

    def _ensure_token(self) -> None:
        """Garantisce un token valido. Chiama authenticate() se serve."""
        if not self.username or not self.password:
            return  # modalità token statico, l'utente lo gestisce
        with self._auth_lock:
            if not self.token or time.time() >= self._expires_at:
                self.authenticate()

    def _get_opener(self):
        """Restituisce un opener urllib per il thread corrente."""
        if not hasattr(self._local, 'opener'):
            ctx = (ssl.create_default_context() if self._verify_tls
                   else ssl._create_unverified_context())
            https_handler = urllib.request.HTTPSHandler(context=ctx)
            jar = http.cookiejar.CookieJar()
            cookie_handler = urllib.request.HTTPCookieProcessor(jar)
            opener = urllib.request.build_opener(https_handler, cookie_handler)
            opener.addheaders = [
                ('User-Agent', 'mailstore-webapi-export/1.0'),
                ('Accept', 'application/json, application/octet-stream, */*'),
            ]
            self._local.opener = opener
        return self._local.opener

    def request(self, method: str, path: str,
                json_body=None, params: dict | None = None,
                expect_binary: bool = False) -> tuple[int, dict, bytes]:
        # Auto-refresh prima della call se in scadenza
        self._ensure_token()

        url = path if path.startswith('http') else self.base_url + path
        if params:
            sep = '&' if '?' in url else '?'
            url = url + sep + urllib.parse.urlencode(params)

        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode('utf-8')

        def _do_request():
            headers = {'Authorization': f'Bearer {self.token}'}
            if json_body is not None:
                headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(url, data=data, headers=headers,
                                          method=method)
            return self._get_opener().open(req, timeout=self.timeout)

        last_err = None
        auth_retried = False
        for attempt in range(3):
            try:
                resp = _do_request()
                status = resp.status
                hdrs = dict(resp.headers.items())
                body = resp.read()
                return status, hdrs, body
            except urllib.error.HTTPError as e:
                status = e.code
                hdrs = dict(e.headers.items())
                body = e.read()
                if status == 401:
                    # Prova un refresh, poi un retry
                    if self.username and self.password and not auth_retried:
                        try:
                            with self._auth_lock:
                                self.authenticate()
                        except (AuthExpired, ApiError):
                            raise AuthExpired(401, 'Refresh fallito', body)
                        auth_retried = True
                        continue
                    raise AuthExpired(401, 'Token scaduto o non valido', body)
                if status in (502, 503, 504) and attempt < 2:
                    last_err = e
                    time.sleep(1.0 * (attempt + 1))
                    continue
                msg = body.decode('utf-8', errors='replace')[:300]
                try:
                    j = json.loads(body)
                    msg = j.get('message') or msg
                except Exception:
                    pass
                raise ApiError(status, msg, body)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise ApiError(0, f'Errore di rete: {e}', b'')

        raise ApiError(0, f'Esauriti i retry: {last_err}', b'')

    def _json(self, method: str, path: str, **kw):
        status, hdrs, body = self.request(method, path, **kw)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ApiError(status, f'JSON non valido in risposta: {e}', body)

    # ---- High-level methods ----

    def get_archives(self) -> list[dict]:
        return self._json('GET', '/api/archives')

    def get_folders(self, archive_name: str,
                    folder_path: str | None = None) -> list[dict]:
        params = {'archiveName': archive_name}
        if folder_path:
            params['folderPath'] = folder_path
        return self._json('GET', '/api/folders', params=params)

    def list_all_folders(self, archive_name: str) -> list[str]:
        """BFS attraverso tutte le cartelle di un archivio.

        Restituisce i fullPath (es. 'Exchange admin/Posta in arrivo')
        — senza prefisso archive_name. Archivi vuoti (404 su root) → [].
        """
        result: list[str] = []
        seen: set[str] = set()
        queue: list[str | None] = [None]  # None = root
        while queue:
            parent = queue.pop(0)
            try:
                children = self.get_folders(archive_name, parent)
            except ApiError as e:
                if parent is None and e.status == 404:
                    return []  # archivio vuoto
                if parent is None:
                    raise
                continue  # cartella interna con problemi: skip
            for f in children:
                fp = f.get('fullPath')
                if not fp or fp in seen:
                    continue
                seen.add(fp)
                result.append(fp)
                if f.get('hasChildren'):
                    queue.append(fp)
        return result

    # ---- Search lifecycle ----

    def create_search(self, folder: str, recursive: bool = False,
                      sort_criteria: str = 'd-desc') -> str:
        body = {
            'querySubject': False,
            'queryFromToCcBcc': False,
            'queryMessageBody': False,
            'queryAttachments': False,
            'queryAttachmentContents': False,
            'folder': folder,
            'folderRecurse': recursive,
            'hasAttachments': None,
        }
        data = self._json('POST', '/api/searches',
                          params={'sortCriteria': sort_criteria},
                          json_body=body)
        sid = data.get('searchId')
        if not sid:
            raise ApiError(0, f'create_search senza searchId: {data}', b'')
        return sid

    def get_search_status(self, search_id: str) -> dict:
        return self._json('GET', f'/api/searches/{search_id}')

    def wait_search_done(self, search_id: str, timeout: float = 600.0,
                         poll_interval: float = 0.5) -> int:
        """Polla fino a completamento VERO della search.

        IMPORTANTE: durante il polling `foundMessageCount` rimane 0 anche se la
        search trova messaggi. Si aggiorna solo quando lo status cambia da
        "Active" → terminale. Quindi non basta vedere progress=100, dobbiamo
        attendere il cambio di status (o usare /results per il count finale).

        Restituisce il foundMessageCount dell'ultima risposta (può essere
        inaffidabile — usare get_search_count() per il valore autorevole).
        """
        deadline = time.time() + timeout
        consecutive_100 = 0
        last_st: dict = {}
        while time.time() < deadline:
            st = self.get_search_status(search_id)
            last_st = st
            err = st.get('errorCode')
            if err and err != 'None':
                raise ApiError(0, f'Search error: {err} - {st.get("errorMessage")}', b'')
            status = (st.get('status') or '').lower()
            progress = st.get('progress', 0)
            if status != 'active':
                # Stato terminale (Done/Completed/...): la search ha finito
                return st.get('foundMessageCount', 0) or 0
            # Safety net: se progress=100 stabile per 3 cicli, esci comunque
            if progress >= 100:
                consecutive_100 += 1
                if consecutive_100 >= 3:
                    return st.get('foundMessageCount', 0) or 0
            else:
                consecutive_100 = 0
            time.sleep(poll_interval)
        raise ApiError(0, f'Timeout su search {search_id} '
                          f'(last={last_st})', b'')

    def get_search_count(self, search_id: str) -> int:
        """Restituisce il messageCount autorevole della search via /results."""
        res = self.get_search_results(search_id, 0, 0)
        return int(res.get('messageCount', 0) or 0)

    def get_search_results(self, search_id: str, start: int, count: int,
                           sort_criteria: str = 'd-desc') -> dict:
        return self._json('GET', f'/api/searches/{search_id}/results',
                          params={'messageIndexStart': start,
                                  'messageCount': count,
                                  'sortCriteria': sort_criteria})

    # ---- Message download ----

    def download_message(self, gid: int, mid: int) -> bytes:
        status, hdrs, body = self.request(
            'GET', f'/api/messages/{gid}/{mid}/download/'
        )
        return body

    def get_message_archive_info(self, gid: int, mid: int) -> dict:
        return self._json('GET', f'/api/messages/{gid}/{mid}/archiveinfo')


# ============================================================
# STATE DB EXTENSION
# ============================================================

class WebApiStateDB(StateDB):
    """Estende StateDB con tracking per messaggi API (per gid/mid)."""

    def __init__(self, db_path: Path):
        super().__init__(db_path)
        conn = self._connect()
        # 1) Tabelle base (senza l'indice su message_id: la colonna potrebbe
        #    non esistere ancora sui DB pre-esistenti)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS exported_api (
                api_key TEXT PRIMARY KEY,    -- "gid/mid"
                gid INTEGER NOT NULL,
                mid INTEGER NOT NULL,
                archive TEXT,
                folder TEXT,
                msg_hash TEXT,
                message_id TEXT,
                filename TEXT,
                size INTEGER,
                exported_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_api_archive ON exported_api(archive);
            CREATE INDEX IF NOT EXISTS idx_api_folder ON exported_api(folder);

            CREATE TABLE IF NOT EXISTS folder_counts (
                archive TEXT NOT NULL,
                folder TEXT NOT NULL,         -- fullPath compreso archive prefix
                found_count INTEGER NOT NULL, -- quanti messaggi la search ha trovato
                last_seen REAL NOT NULL,
                PRIMARY KEY (archive, folder)
            );

            CREATE TABLE IF NOT EXISTS failed_messages (
                api_key TEXT PRIMARY KEY,    -- "gid/mid"
                gid INTEGER NOT NULL,
                mid INTEGER NOT NULL,
                archive TEXT,
                folder TEXT,
                subject TEXT,
                email_date TEXT,             -- ISO header Date: o JSON date
                error_type TEXT NOT NULL,    -- es. 'PermissionError', 'EmptyBody'
                error_msg TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_failed_archive
                ON failed_messages(archive);
            CREATE INDEX IF NOT EXISTS idx_failed_error_type
                ON failed_messages(error_type);
        """)
        # 2) Migrazione: aggiungi message_id ai DB pre-esistenti
        try:
            conn.execute("ALTER TABLE exported_api ADD COLUMN message_id TEXT")
        except sqlite3.OperationalError:
            pass  # colonna già esistente
        # 3) Solo ora l'indice su message_id può essere creato in sicurezza
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_message_id "
            "ON exported_api(message_id)"
        )
        conn.commit()

    def record_folder_count(self, archive: str, folder: str, count: int) -> None:
        conn = self._connect()
        conn.execute(
            'INSERT OR REPLACE INTO folder_counts '
            '(archive, folder, found_count, last_seen) VALUES (?, ?, ?, ?)',
            (archive, folder, int(count), time.time())
        )
        conn.commit()

    def is_api_exported(self, gid: int, mid: int) -> bool:
        key = f'{gid}/{mid}'
        cur = self._connect().execute(
            'SELECT 1 FROM exported_api WHERE api_key = ? LIMIT 1', (key,))
        return cur.fetchone() is not None

    def has_api_message_id(self, message_id: str) -> bool:
        if not message_id:
            return False
        cur = self._connect().execute(
            'SELECT 1 FROM exported_api WHERE message_id = ? LIMIT 1',
            (message_id,))
        return cur.fetchone() is not None

    def mark_api_exported(self, gid: int, mid: int, archive: str, folder: str,
                          msg_hash: str, filename: str, size: int,
                          message_id: str = ''):
        conn = self._connect()
        conn.execute(
            'INSERT OR REPLACE INTO exported_api '
            '(api_key, gid, mid, archive, folder, msg_hash, message_id, '
            'filename, size, exported_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (f'{gid}/{mid}', gid, mid, archive, folder, msg_hash, message_id,
             filename, size, time.time())
        )
        conn.commit()

    def api_stats(self) -> dict:
        cur = self._connect().execute(
            'SELECT COUNT(*), COALESCE(SUM(size), 0) FROM exported_api')
        count, total_size = cur.fetchone()
        return {'count': count or 0, 'total_size': total_size or 0}

    def get_exported_keys_for_folder(self, archive: str, folder: str) -> set[str]:
        cur = self._connect().execute(
            'SELECT api_key FROM exported_api WHERE archive = ? AND folder = ?',
            (archive, folder))
        return {row[0] for row in cur.fetchall()}

    # ---- failed_messages ----

    def mark_api_failed(self, gid: int, mid: int, archive: str, folder: str,
                        subject: str, email_date: str,
                        error_type: str, error_msg: str) -> None:
        """Registra (o aggiorna) un fallimento permanente per gid/mid.

        Se la chiave esiste già, incrementa attempts e aggiorna last_seen ed
        error_type/error_msg (l'ultimo errore vince).
        """
        key = f'{gid}/{mid}'
        now = time.time()
        conn = self._connect()
        conn.execute(
            'INSERT INTO failed_messages '
            '(api_key, gid, mid, archive, folder, subject, email_date, '
            'error_type, error_msg, first_seen, last_seen, attempts) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) '
            'ON CONFLICT(api_key) DO UPDATE SET '
            '  last_seen = excluded.last_seen, '
            '  error_type = excluded.error_type, '
            '  error_msg = excluded.error_msg, '
            '  attempts = failed_messages.attempts + 1',
            (key, gid, mid, archive, folder, subject, email_date,
             error_type, error_msg, now, now)
        )
        conn.commit()

    def is_api_failed(self, gid: int, mid: int) -> bool:
        cur = self._connect().execute(
            'SELECT 1 FROM failed_messages WHERE api_key = ? LIMIT 1',
            (f'{gid}/{mid}',))
        return cur.fetchone() is not None

    def get_failed_keys(self) -> set[str]:
        """Tutti gli api_key (gid/mid) attualmente marcati failed."""
        cur = self._connect().execute('SELECT api_key FROM failed_messages')
        return {row[0] for row in cur.fetchall()}

    def all_failed(self) -> list[dict]:
        """Ritorna tutta la tabella failed_messages come list of dict."""
        cur = self._connect().execute(
            'SELECT gid, mid, archive, folder, subject, email_date, '
            'error_type, error_msg, first_seen, last_seen, attempts '
            'FROM failed_messages ORDER BY archive, folder, email_date')
        return [{
            'gid': row[0], 'mid': row[1], 'archive': row[2], 'folder': row[3],
            'subject': row[4], 'email_date': row[5],
            'error_type': row[6], 'error_msg': row[7],
            'first_seen': datetime.fromtimestamp(row[8]).isoformat(timespec='seconds'),
            'last_seen': datetime.fromtimestamp(row[9]).isoformat(timespec='seconds'),
            'attempts': row[10],
        } for row in cur.fetchall()]

    def failed_stats(self) -> dict:
        """Conteggi e breakdown per error_type."""
        conn = self._connect()
        total = conn.execute(
            'SELECT COUNT(*) FROM failed_messages').fetchone()[0]
        by_type = conn.execute(
            'SELECT error_type, COUNT(*) FROM failed_messages '
            'GROUP BY error_type ORDER BY 2 DESC').fetchall()
        by_archive = conn.execute(
            'SELECT archive, COUNT(*) FROM failed_messages '
            'GROUP BY archive ORDER BY 2 DESC').fetchall()
        return {
            'count': total,
            'by_error_type': [{'type': r[0], 'count': r[1]} for r in by_type],
            'by_archive': [{'archive': r[0], 'count': r[1]} for r in by_archive],
        }


# ============================================================
# INTERACTIVE: ARCHIVE SELECTION
# ============================================================

def select_archives_interactive(archives: list[dict]) -> list[str]:
    """
    Selettore multi-archivio (riusa la UI di select_folders_interactive
    presentando i nomi archivi come "cartelle").
    """
    # Mostra solo gli archivi con cartelle (gli altri sono vuoti via API)
    items = [a['name'] for a in archives]
    if not items:
        print('Nessun archivio disponibile.')
        return []
    print()
    print(f'{len(items)} archivi disponibili.')
    print('Stessa interfaccia del selettore cartelle.')
    return select_folders_interactive(items)


# ============================================================
# EXPORT LOGIC
# ============================================================

def safe_local_path_for(archive: str, folder_path: str,
                        output_root: Path) -> Path:
    """
    Costruisce il path locale per un messaggio.
    folder_path è il valore in arrivo dall'API ("admin/Exchange ricoveri/Archive")
    oppure ("archive_name/sotto/cartella") — già include il nome archivio.
    """
    parts = [p for p in folder_path.split('/') if p]
    safe = [sanitize_component(p) for p in parts]
    return output_root.joinpath(*safe) if safe else output_root


def count_folder(client: WebClient, archive: str, folder_path: str,
                 logger: logging.Logger) -> int:
    """Message count of one folder (creates a search, reads its count)."""
    api_folder = f'{archive}/{folder_path}' if folder_path else archive
    for attempt in range(3):
        try:
            sid = client.create_search(folder=api_folder, recursive=False)
            client.wait_search_done(sid)
            return client.get_search_count(sid)
        except AuthExpired:
            raise
        except ApiError as e:
            if _is_cached_search_expired(e) and attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            logger.warning('count(%s) fallito: %s', api_folder, e)
            return 0
        except Exception as e:
            logger.warning('count(%s) fallito: %s', api_folder, e)
            return 0
    return 0


def precount(client: WebClient, archives: list[str], workers: int,
             stop_event: threading.Event, logger: logging.Logger,
             progress: Progress, folder_include: list[re.Pattern],
             folder_exclude: list[re.Pattern]) -> int:
    """Count ALL messages to download up front, so the progress bar has a fixed
    total. Enumerates folders, then counts them in parallel (cache-safe cap)."""
    folder_list: list[tuple[str, str]] = []
    for archive in archives:
        if stop_event.is_set():
            break
        try:
            folders = client.list_all_folders(archive)
        except AuthExpired:
            raise
        except Exception as e:
            logger.error('precount: enum %s fallita: %s', archive, e)
            continue
        if folder_include:
            folders = [f for f in folders
                       if any(r.search(f) for r in folder_include)]
        if folder_exclude:
            folders = [f for f in folders
                       if not any(r.search(f) for r in folder_exclude)]
        for f in folders:
            folder_list.append((archive, f))
    progress.update(total_folders=len(folder_list))
    logger.info('Pre-conteggio: %d cartelle da contare', len(folder_list))

    grand = 0
    pool_workers = max(1, min(workers, 8))  # search cache saturates over ~8
    with ThreadPoolExecutor(max_workers=pool_workers,
                            thread_name_prefix='cnt') as pool:
        futs = {pool.submit(count_folder, client, a, f, logger): (a, f)
                for a, f in folder_list}
        for fut in as_completed(futs):
            if stop_event.is_set():
                break
            try:
                c = fut.result()
            except AuthExpired:
                stop_event.set()
                raise
            except Exception:
                c = 0
            grand += c
            progress.inc(total_msgs=c)
    logger.info('Pre-conteggio completato: %d messaggi totali', grand)
    return grand


def export_folder(client: WebClient, archive: str, folder_path: str,
                  output_root: Path, state: WebApiStateDB, progress: Progress,
                  cfg: dict, stop_event: threading.Event, logger: logging.Logger,
                  download_pool: ThreadPoolExecutor | None = None,
                  page_size: int = 300):
    """
    Esporta tutte le mail di una cartella tramite search + download paralleli.

    Se `download_pool` è fornito, lo riusa per parallelizzare i download.
    Backpressure: massimo workers*4 future in-flight contemporanee.
    """
    if stop_event.is_set():
        return

    api_folder = f'{archive}/{folder_path}' if folder_path else archive

    progress.update(current_folder=api_folder)
    logger.info(f'Inizio cartella: {api_folder!r}')

    # Crea+polla+count con retry su search expired
    search_id = None
    total = 0
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            search_id = client.create_search(folder=api_folder, recursive=False)
            client.wait_search_done(search_id)
            # Count autoritativo via /results (foundMessageCount dal polling
            # può essere 0 anche con messaggi presenti).
            total = client.get_search_count(search_id)
            break
        except AuthExpired:
            raise
        except ApiError as e:
            last_err = e
            search_id = None
            if _is_cached_search_expired(e) and attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = e
            search_id = None
            break
    if search_id is None:
        logger.error(f'Search create/wait fallita per {api_folder!r}: {last_err}')
        return

    # Salva il count nella DB per tracciabilità
    state.record_folder_count(archive, api_folder, total)

    if total == 0:
        logger.info(f'Cartella vuota: {api_folder!r}')
        progress.inc(done_folders=1)
        return

    already = state.get_exported_keys_for_folder(archive, api_folder)
    # Set globale di gid/mid da saltare (popolato da main via cfg)
    excluded: set[str] = cfg.get('excluded_keys') or set()
    years: set[int] = cfg.get('years') or set()
    months: set[int] = cfg.get('months') or set()
    # total already counted up front when known_total -> don't add it again
    if cfg.get('known_total'):
        progress.inc(skipped_msgs=len(already), skip_resume=len(already),
                     done_msgs=len(already))
    else:
        progress.inc(total_msgs=total, skipped_msgs=len(already),
                     skip_resume=len(already), done_msgs=len(already))

    # Determina parallelismo
    workers = cfg.get('workers', 16)
    max_inflight = max(workers * 4, 32)
    use_pool = download_pool is not None

    pending: list = []  # list[Future]

    def _drain_completed(block: bool = False):
        """Drena le future completate, gestendo eccezioni + propagazione AuthExpired."""
        nonlocal pending
        still: list = []
        for fut in pending:
            if fut.done() or block:
                try:
                    fut.result(timeout=None if block else 0)
                except AuthExpired:
                    stop_event.set()
                    raise
                except Exception as ex:
                    logger.error(f'Download fallito in {api_folder!r}: {ex}')
                    progress.inc(failed_msgs=1)
            else:
                still.append(fut)
        pending = still

    def _drain_one():
        """Aspetta il completamento di almeno una future pendente."""
        nonlocal pending
        if not pending:
            return
        from concurrent.futures import wait, FIRST_COMPLETED
        done, _not_done = wait(pending, return_when=FIRST_COMPLETED)
        still: list = []
        for fut in pending:
            if fut in done:
                try:
                    fut.result()
                except AuthExpired:
                    stop_event.set()
                    raise
                except Exception as ex:
                    logger.error(f'Download fallito in {api_folder!r}: {ex}')
                    progress.inc(failed_msgs=1)
            else:
                still.append(fut)
        pending = still

    # Paginazione + dispatch al pool
    start = 0
    try:
        while start < total and not stop_event.is_set():
            res = None
            for attempt in range(3):
                try:
                    res = client.get_search_results(search_id, start, page_size)
                    break
                except AuthExpired:
                    raise
                except ApiError as e:
                    if _is_cached_search_expired(e) and attempt < 2:
                        # La search è stata espulsa dalla cache: ricreala
                        logger.warning(f'Search cache scaduta per {api_folder!r}, '
                                       f'ricreo (attempt {attempt + 1}/3)')
                        try:
                            search_id = client.create_search(folder=api_folder,
                                                              recursive=False)
                            client.wait_search_done(search_id)
                        except Exception as recreate_err:
                            logger.error(f'Ricreazione search fallita: '
                                         f'{recreate_err}')
                            break
                        continue
                    logger.error(f'get_search_results({start}) fallito per '
                                 f'{api_folder!r}: {e}')
                    break
            if res is None:
                break

            items = res.get('searchResultItems') or []
            if not items:
                break

            for item in items:
                if stop_event.is_set():
                    break
                gid = item.get('gid')
                mid = item.get('mid')
                if gid is None or mid is None:
                    continue
                key = f'{gid}/{mid}'
                if key in already:
                    continue
                if excluded and key in excluded:
                    # gid/mid in skip-list (--exclude-failed o --exclude-from)
                    progress.inc(skipped_msgs=1, skip_excluded=1)
                    continue
                # Year/month filter: applied BEFORE download using the search
                # result date. Non-matching (and undated) messages are removed
                # from the total so the progress bar still reaches 100%.
                if years or months:
                    yr, mo = _item_year_month(item)
                    if (years and yr not in years) or (months and mo not in months):
                        progress.inc(total_msgs=-1, skip_filter=1)
                        continue

                if use_pool:
                    # Backpressure: se troppi in volo, aspetta che si liberi
                    while len(pending) >= max_inflight and not stop_event.is_set():
                        _drain_one()
                    fut = download_pool.submit(
                        download_and_save, client, item, archive,
                        output_root, state, progress, cfg, logger)
                    pending.append(fut)
                else:
                    # Modalità sequenziale (fallback)
                    try:
                        download_and_save(client, item, archive, output_root,
                                          state, progress, cfg, logger)
                    except AuthExpired:
                        raise
                    except Exception as ex:
                        logger.error(f'Download {key} fallito: {ex}')
                        progress.inc(failed_msgs=1)

            start += page_size

        # Aspetta tutti i pending della cartella prima di passare alla prossima
        if use_pool:
            _drain_completed(block=True)
    finally:
        # In caso di interruzione, drena comunque le future in coda
        if use_pool and pending:
            _drain_completed(block=True)

    progress.inc(done_folders=1)
    logger.info(f'Cartella completata: {api_folder!r}')


_TMP_WRITE_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8)


# Pattern del filename: ..._YYYYMMDD_HHMMSS_HASH8.eml.tmp
_TMP_DATE_RE = re.compile(r'_(\d{8})_(\d{6})_[a-f0-9]{8}\.eml\.tmp$')


def _item_year_month(item: dict) -> tuple[int | None, int | None]:
    """(year, month) from a search-result item's date (ISO 8601). (None, None)
    if missing/unparsable — such messages are treated as non-matching by filters."""
    ds = item.get('date')
    if not ds:
        return None, None
    try:
        dt = datetime.fromisoformat(ds.replace('Z', '+00:00'))
        return dt.year, dt.month
    except (ValueError, AttributeError):
        return None, None


def _parse_email_date_from_tmp_name(name: str) -> datetime | None:
    """Estrae la email date dal nome file .tmp. None se non parsabile."""
    m = _TMP_DATE_RE.search(name)
    if not m:
        return None
    ymd, hms = m.group(1), m.group(2)
    if ymd == '00000000':  # placeholder per email senza Date header
        return None
    try:
        return datetime.strptime(ymd + hms, '%Y%m%d%H%M%S')
    except ValueError:
        return None


def _scan_orphan_tmp(root: Path, json_out: Path | None = None) -> int:
    """Dry-run: list all *.eml.tmp orphan files under root. Returns count.

    Prints a human-readable table to stdout and, if json_out is set, writes
    a JSON report with full paths. No filesystem modifications.
    """
    items: list[dict] = []
    total_bytes = 0
    email_dates: list[datetime] = []
    mtimes: list[datetime] = []
    parsed_failed = 0
    for p in sorted(root.rglob('*.eml.tmp')):
        try:
            st = p.stat()
        except OSError:
            continue
        mtime_dt = datetime.fromtimestamp(st.st_mtime)
        email_dt = _parse_email_date_from_tmp_name(p.name)
        if email_dt is None:
            parsed_failed += 1
        else:
            email_dates.append(email_dt)
        mtimes.append(mtime_dt)
        items.append({
            'path': str(p),
            'size': st.st_size,
            'mtime': mtime_dt.isoformat(timespec='seconds'),
            'email_date': email_dt.isoformat(timespec='seconds') if email_dt else None,
        })
        total_bytes += st.st_size

    if not items:
        print(f'Nessun .eml.tmp orfano sotto {root}')
        return 0

    def _fmt(dt: datetime | None) -> str:
        return dt.isoformat(timespec='seconds') if dt else 'n/a'

    email_min = min(email_dates) if email_dates else None
    email_max = max(email_dates) if email_dates else None
    mtime_min = min(mtimes) if mtimes else None
    mtime_max = max(mtimes) if mtimes else None

    print(f'Trovati {len(items)} .eml.tmp orfani sotto {root}\n')
    print(f'{"MTIME":<19}  {"SIZE":>10}  PATH')
    print('-' * 80)
    for it in items:
        print(f'{it["mtime"]:<19}  {it["size"]:>10}  {it["path"]}')
    print('-' * 80)
    print(f'\n=== RIEPILOGO ===')
    print(f'File .tmp orfani:    {len(items):,}')
    print(f'Dimensione totale:   {total_bytes:,} byte '
          f'({total_bytes / 1024 / 1024:.2f} MB)')
    print(f'Email date  - prima: {_fmt(email_min)}')
    print(f'              ultima: {_fmt(email_max)}')
    print(f'Mtime file  - prima: {_fmt(mtime_min)}')
    print(f'              ultima: {_fmt(mtime_max)}')
    if parsed_failed:
        print(f'(date email non parsabili da {parsed_failed} nomi file)')

    if json_out is not None:
        json_out = json_out.expanduser().resolve()
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump({
                'root': str(root),
                'scanned_at': datetime.now().isoformat(timespec='seconds'),
                'count': len(items),
                'total_bytes': total_bytes,
                'email_date_min': _fmt(email_min) if email_min else None,
                'email_date_max': _fmt(email_max) if email_max else None,
                'mtime_min': _fmt(mtime_min) if mtime_min else None,
                'mtime_max': _fmt(mtime_max) if mtime_max else None,
                'email_date_unparsed_count': parsed_failed,
                'files': items,
            }, f, indent=2, ensure_ascii=False)
        print(f'\nReport JSON salvato: {json_out}')

    return len(items)


def _dump_failed_messages(state: WebApiStateDB, json_out: Path | None) -> int:
    """Dumpa failed_messages in JSON (stdout o file) con stats. Ritorna il count."""
    rows = state.all_failed()
    stats = state.failed_stats()

    # Calcolo data min/max dalle email_date parsabili
    email_dates: list[str] = []
    for r in rows:
        ed = r.get('email_date') or ''
        if ed:
            email_dates.append(ed[:19])  # tronca ai secondi
    email_min = min(email_dates) if email_dates else None
    email_max = max(email_dates) if email_dates else None

    report = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'count': stats['count'],
        'email_date_min': email_min,
        'email_date_max': email_max,
        'by_error_type': stats['by_error_type'],
        'by_archive': stats['by_archive'],
        'messages': rows,
    }

    if json_out is not None:
        json_out = json_out.expanduser().resolve()
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f'Report salvato: {json_out}')
        print(f'  Messaggi falliti:   {stats["count"]:,}')
        if email_min and email_max:
            print(f'  Email date min/max: {email_min}  /  {email_max}')
        if stats['by_error_type']:
            print('  Per tipo errore:')
            for e in stats['by_error_type']:
                print(f'    {e["count"]:>6}  {e["type"]}')
        if stats['by_archive']:
            print('  Per archivio:')
            for e in stats['by_archive']:
                print(f'    {e["count"]:>6}  {e["archive"]}')
    else:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write('\n')

    return stats['count']


def _load_excluded_from_json(path: Path) -> set[str]:
    """Carica un set di api_key "gid/mid" da un JSON nel formato di --list-failed.

    Accetta sia il formato esteso ({"messages": [{"gid":..,"mid":..,...}]}) sia
    una lista piatta di {"gid","mid"} o una lista di stringhe "gid/mid".
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    keys: set[str] = set()
    items = data.get('messages') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError(f'Formato non riconosciuto in {path}')
    for it in items:
        if isinstance(it, str) and '/' in it:
            keys.add(it)
        elif isinstance(it, dict) and 'gid' in it and 'mid' in it:
            keys.add(f"{it['gid']}/{it['mid']}")
    return keys


def _cleanup_orphan_tmp(root: Path, logger: logging.Logger) -> None:
    """Best-effort: remove leftover *.eml.tmp orphan files under root.

    Crashed previous runs leave .tmp behind; deleting them up-front prevents
    spurious "skip orfano" warnings during the new run. Failures (locked by
    AV/sync) are logged but non-fatal — those files will be skipped later.
    """
    deleted = 0
    failed = 0
    try:
        for p in root.rglob('*.eml.tmp'):
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                failed += 1
                logger.debug('Cleanup: impossibile eliminare %s (errno=%s)',
                             p, e.errno)
    except OSError as e:
        logger.warning('Cleanup .tmp interrotto: %s', e)
    if deleted or failed:
        logger.info('Cleanup .tmp orfani: eliminati=%d, falliti=%d',
                    deleted, failed)


def _atomic_write_with_retry(tmp: Path, target: Path, raw: bytes,
                             logger: logging.Logger) -> int:
    """Write raw to tmp + fsync + rename to target, with retry on transient OSError.

    On Windows, antivirus realtime scan and sync agents (Defender, OneDrive)
    hold brief locks on newly-created files. With many concurrent workers writing
    to one directory this surfaces as EACCES on open() or os.replace().
    Returns number of retries needed (0 = first attempt succeeded).
    """
    for attempt in range(len(_TMP_WRITE_BACKOFF) + 1):
        try:
            with open(tmp, 'wb') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            return attempt
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < len(_TMP_WRITE_BACKOFF):
                logger.debug('Retry %d/%d per %s (errno=%s)',
                             attempt + 1, len(_TMP_WRITE_BACKOFF),
                             target.name, e.errno)
                # jitter: spalma le retry di N worker simultanei
                time.sleep(_TMP_WRITE_BACKOFF[attempt] + random.uniform(0, 0.02))
                continue
            raise
    return len(_TMP_WRITE_BACKOFF)


def download_and_save(client: WebClient, item: dict, archive: str,
                      output_root: Path, state: WebApiStateDB,
                      progress: Progress, cfg: dict, logger: logging.Logger):
    """Scarica un singolo messaggio e lo salva atomicamente.

    In caso di fallimento definitivo, registra (gid, mid) in failed_messages
    prima di rilanciare, così il prossimo run con --exclude-failed lo skippa.
    """
    gid = item['gid']
    mid = item['mid']
    folder = item.get('folder') or f'{archive}/_unknown'
    try:
        _do_download_and_save(client, item, archive, gid, mid, folder,
                              output_root, state, progress, cfg, logger)
    except AuthExpired:
        raise
    except Exception as e:
        try:
            subject = decode_subject(item.get('subject') or '') or ''
            email_date = item.get('date') or ''
            state.mark_api_failed(gid, mid, archive, folder,
                                  subject, email_date,
                                  type(e).__name__, str(e)[:500])
        except Exception as db_err:
            logger.error('mark_api_failed fallito per %s/%s: %s',
                         gid, mid, db_err)
        raise


def _do_download_and_save(client: WebClient, item: dict, archive: str,
                          gid: int, mid: int, folder: str,
                          output_root: Path, state: WebApiStateDB,
                          progress: Progress, cfg: dict,
                          logger: logging.Logger) -> None:
    """Body interno di download_and_save (refactor per error handling pulito)."""
    raw = client.download_message(gid, mid)
    if not raw:
        raise RuntimeError('download_message ha restituito body vuoto')

    msg_hash = hashlib.sha256(raw).hexdigest()

    # Parse minimale headers per filename + dedup
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        date_header = msg.get('Date', '')
        subject_raw = msg.get('Subject', '')
        message_id = (msg.get('Message-ID') or '').strip()
    except Exception:
        date_header = ''
        subject_raw = item.get('subject') or ''
        message_id = ''

    # Dedup via Message-ID (opzionale): skippa la scrittura ma marca nel DB
    # così il resume non riprocessa lo stesso (gid, mid).
    if cfg.get('dedup_message_id') and message_id and \
            state.has_api_message_id(message_id):
        state.mark_api_exported(gid, mid, archive, folder, msg_hash,
                                filename='(dup-skipped)', size=len(raw),
                                message_id=message_id)
        progress.inc(skipped_msgs=1, skip_dedup=1)
        return

    subject = decode_subject(subject_raw) or item.get('subject') or ''
    date_dt = parse_imap_date(date_header)
    if date_dt is None:
        # fallback: usa il campo date dal JSON dell'API
        date_str = item.get('date')
        if date_str:
            try:
                # ISO 8601 con Z finale
                date_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            except ValueError:
                date_dt = None

    filename = build_filename(date_dt, subject, msg_hash)

    # Una sola sottocartella per archivio (es. "li - liquidatori/").
    # Le sotto-cartelle IMAP interne dell'archivio restano flat al suo interno.
    local_folder = output_root / sanitize_component(archive)
    # Optional: split into per-year subfolders inside the archive, based on the
    # message Date header (e.g. "li - liquidatori/2023/"). Undated mail -> "0000".
    if cfg.get('split_by_year'):
        year_dir = f'{date_dt.year:04d}' if date_dt is not None else '0000'
        local_folder = local_folder / year_dir
    local_folder.mkdir(parents=True, exist_ok=True)

    target = local_folder / filename
    if target.exists():
        n = 1
        while True:
            alt = local_folder / f'{target.stem}_{n}.eml'
            if not alt.exists():
                target = alt
                break
            n += 1
            if n > 1000:
                raise RuntimeError(f'Troppe collisioni per {filename}')

    tmp = target.with_suffix('.eml.tmp')
    # Se un .tmp è già presente è orfano di un crash precedente (o lockato
    # da AV/sync): saltiamo l'email senza marcarla in state.db, così il
    # prossimo resume la riproverà a freddo dopo il cleanup all'avvio.
    if tmp.exists():
        logger.warning('Skip .tmp orfano: %s', tmp.name)
        progress.inc(skipped_msgs=1, skip_tmp=1)
        return
    _atomic_write_with_retry(tmp, target, raw, logger)

    state.mark_api_exported(gid, mid, archive, folder, msg_hash,
                            filename=target.name,
                            size=len(raw),
                            message_id=message_id)
    progress.inc(done_msgs=1, bytes_written=len(raw))


# ============================================================
# BENCHMARK MODE
# ============================================================

def _pick_benchmark_folder(client: WebClient, archives: list[str],
                           min_messages: int, logger: logging.Logger
                           ) -> tuple[str, str, int]:
    """Cerca la prima cartella (archive, folder, total) con >= min_messages.

    Scansiona gli archivi nell'ordine fornito, prendendo la prima cartella
    grande abbastanza. Restituisce (archive, folder_full_path, total).
    """
    for archive in archives:
        try:
            folders = client.list_all_folders(archive)
        except Exception as e:
            logger.warning(f'list_all_folders({archive!r}) fallita: {e}')
            continue
        for fp in folders:
            api_folder = f'{archive}/{fp}'
            try:
                sid = client.create_search(folder=api_folder, recursive=False)
                client.wait_search_done(sid, timeout=120)
                total = client.get_search_count(sid)
            except Exception as e:
                logger.warning(f'count({api_folder!r}) fallita: {e}')
                continue
            if total >= min_messages:
                return archive, fp, total
    raise RuntimeError(f'Nessuna cartella con almeno {min_messages} messaggi '
                       f'tra gli archivi forniti.')


def run_benchmark(client: WebClient, archive: str, folder_path: str,
                  samples: int, worker_counts: list[int],
                  json_path: Path, logger: logging.Logger) -> dict:
    """
    Misura msg/s e MB/s al variare del numero di worker per download.

    Scarica `samples` messaggi della cartella indicata, ripetutamente con
    diversi worker count. I .eml NON vengono scritti su disco — solo
    misurazione throughput puro lato download.
    """
    api_folder = f'{archive}/{folder_path}' if folder_path else archive
    print()
    print('=' * 70)
    print('BENCHMARK WORKER COUNT')
    print('=' * 70)
    print(f'Cartella di test:  {api_folder}')
    print(f'Campioni:          {samples}')
    print(f'Worker counts:     {worker_counts}')
    print()

    # Crea la search e recupera la lista (gid, mid)
    print('Preparo la lista di messaggi...')
    sid = client.create_search(folder=api_folder, recursive=False)
    client.wait_search_done(sid, timeout=600)
    total = client.get_search_count(sid)
    if total < samples:
        print(f'  La cartella ha solo {total} messaggi (richiesti {samples}). '
              f'Uso {total}.')
        samples = total
    if samples == 0:
        raise RuntimeError(f'La cartella {api_folder!r} è vuota.')

    res = client.get_search_results(sid, 0, samples)
    items = [(it['gid'], it['mid']) for it in res.get('searchResultItems', [])
             if it.get('gid') is not None and it.get('mid') is not None]
    if not items:
        raise RuntimeError('Nessun messaggio recuperato dalla search.')
    print(f'  Pronti {len(items)} messaggi per il benchmark.')
    print()

    # Warmup (no measurement): scalda TLS+keepalive con pochi download
    print('Warmup (2 download)...')
    for gid, mid in items[:2]:
        try:
            client.download_message(gid, mid)
        except Exception as e:
            logger.warning(f'Warmup ha fallito: {e}')
    print()

    # Header tabella
    print(f'{"Workers":>8}  {"Tempo":>7}  {"OK":>5}  {"Fail":>5}  '
          f'{"msg/s":>8}  {"MB/s":>7}  {"Latency p50/p95 ms":>20}')
    print('-' * 80)

    results: list[dict] = []
    for w in worker_counts:
        # Reset del download — usa una copia degli items
        latencies: list[float] = []
        lat_lock = threading.Lock()

        def _dl(gm):
            t1 = time.time()
            try:
                body = client.download_message(*gm)
                lat = time.time() - t1
                with lat_lock:
                    latencies.append(lat)
                return len(body)
            except Exception:
                return -1

        t0 = time.time()
        ok = 0
        fail = 0
        total_bytes = 0
        with ThreadPoolExecutor(max_workers=w,
                                 thread_name_prefix=f'bench{w}') as pool:
            futures = [pool.submit(_dl, gm) for gm in items]
            for fut in as_completed(futures):
                try:
                    size = fut.result()
                except Exception:
                    size = -1
                if size >= 0:
                    ok += 1
                    total_bytes += size
                else:
                    fail += 1
        elapsed = time.time() - t0
        msg_per_s = ok / elapsed if elapsed else 0
        mb_per_s = (total_bytes / (1024 * 1024)) / elapsed if elapsed else 0
        p50 = p95 = 0.0
        if latencies:
            ls = sorted(latencies)
            p50 = ls[len(ls) // 2] * 1000
            p95 = ls[min(len(ls) - 1, int(len(ls) * 0.95))] * 1000
        results.append({
            'workers': w, 'samples': len(items), 'ok': ok, 'fail': fail,
            'elapsed_s': round(elapsed, 2),
            'msg_per_s': round(msg_per_s, 2),
            'mb_per_s': round(mb_per_s, 2),
            'latency_p50_ms': round(p50, 1),
            'latency_p95_ms': round(p95, 1),
        })
        print(f'{w:>8}  {elapsed:>6.2f}s  {ok:>5}  {fail:>5}  '
              f'{msg_per_s:>8.1f}  {mb_per_s:>7.2f}  '
              f'{p50:>9.0f} / {p95:>6.0f}')

    print()
    print('-' * 80)

    # Trova ottimo: massimo msg/s con fail rate < 5%
    valid = [r for r in results if r['samples'] > 0
             and r['fail'] / r['samples'] < 0.05]
    if valid:
        best = max(valid, key=lambda r: r['msg_per_s'])
        # Cerca anche un "sweet spot": il più piccolo worker count che dà
        # almeno il 90% del massimo (di solito più stabile).
        threshold = best['msg_per_s'] * 0.9
        sweet = next((r for r in valid if r['msg_per_s'] >= threshold), best)
        print(f'{sym("✓","OK")} Picco assoluto:  {best["workers"]:>4} worker '
              f'→ {best["msg_per_s"]:.1f} msg/s, {best["mb_per_s"]:.2f} MB/s')
        print(f'{sym("✓","OK")} Sweet spot 90%:  {sweet["workers"]:>4} worker '
              f'→ {sweet["msg_per_s"]:.1f} msg/s, {sweet["mb_per_s"]:.2f} MB/s '
              f'(consigliato: meno worker, throughput simile, meno carico server)')
    else:
        best = None
        sweet = None
        print(f'{sym("⚠","!")} Nessun risultato valido (troppi errori).')

    summary = {
        'benchmarked_at': datetime.now(timezone.utc).isoformat(),
        'host': client.base_url,
        'archive': archive,
        'folder': folder_path,
        'samples': samples,
        'results': results,
        'optimum_peak': best,
        'optimum_sweet': sweet,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print()
    print(f'Risultati salvati in: {json_path}')
    return summary


# ============================================================
# ANALYZE-ONLY MODE
# ============================================================

def _is_cached_search_expired(err: Exception) -> bool:
    """True se l'errore è 'search cache scaduta' lato MailStore."""
    if not isinstance(err, ApiError):
        return False
    msg = (err.message or '').lower()
    return err.status == 500 and 'cached search' in msg


def analyze_folder(client: WebClient, archive: str, folder_path: str,
                   logger: logging.Logger,
                   max_retries: int = 3) -> tuple[str, int]:
    """Conta i messaggi di una cartella SENZA scaricarli.

    Retry automatico se MailStore espelle la search dalla cache (500).
    Usa get_search_count() (via /results) per il valore autoritativo.
    """
    api_folder = f'{archive}/{folder_path}' if folder_path else archive
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            sid = client.create_search(folder=api_folder, recursive=False)
            client.wait_search_done(sid, timeout=600)
            count = client.get_search_count(sid)
            return folder_path, count
        except AuthExpired:
            raise
        except ApiError as e:
            last_err = e
            if _is_cached_search_expired(e) and attempt < max_retries - 1:
                time.sleep(0.3 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = e
            break
    logger.error(f'Analyze fallita per {api_folder!r}: {last_err}')
    return folder_path, -1


def analyze_archive(client: WebClient, archive: str, pool: ThreadPoolExecutor,
                    progress: Progress, stop_event: threading.Event,
                    logger: logging.Logger,
                    folder_include: list[re.Pattern],
                    folder_exclude: list[re.Pattern]) -> dict:
    """Analizza la struttura di un archivio in parallelo. Ritorna dict di stats."""
    logger.info(f'=== ANALYZE: {archive} ===')
    try:
        folders = client.list_all_folders(archive)
    except AuthExpired:
        raise
    except ApiError as e:
        logger.error(f'Folder list fallito per {archive!r}: {e}')
        return {'name': archive, 'error': str(e), 'total_folders': 0,
                'total_messages': 0, 'folders': []}

    if folder_include:
        folders = [f for f in folders if any(r.search(f) for r in folder_include)]
    if folder_exclude:
        folders = [f for f in folders if not any(r.search(f) for r in folder_exclude)]

    progress.inc(total_folders=len(folders))

    futures = {pool.submit(analyze_folder, client, archive, fp, logger): fp
               for fp in folders}
    folder_counts: list[dict] = []
    for fut in as_completed(futures):
        if stop_event.is_set():
            break
        try:
            fp, cnt = fut.result()
        except AuthExpired:
            raise
        except Exception as e:
            logger.error(f'Analyze future exception: {e}')
            cnt = -1
            fp = futures[fut]
        if cnt >= 0:
            folder_counts.append({'path': fp, 'count': cnt})
            progress.inc(done_msgs=cnt, total_msgs=cnt)
        progress.inc(done_folders=1)
        progress.update(current_folder=f'{archive}/{fp}')

    folder_counts.sort(key=lambda x: x['count'], reverse=True)
    return {
        'name': archive,
        'total_folders': len(folder_counts),
        'total_messages': sum(f['count'] for f in folder_counts),
        'folders': folder_counts,
    }


def run_analyze(client: WebClient, archives: list[str], workers: int,
                output_root: Path, stop_event: threading.Event,
                logger: logging.Logger, json_path: Path,
                folder_include: list[re.Pattern],
                folder_exclude: list[re.Pattern]) -> dict:
    """Esegue l'analisi su tutti gli archivi e salva il JSON."""
    progress = Progress()
    progress.start()
    t0 = time.time()

    result_archives: list[dict] = []
    aborted = False

    try:
        with ThreadPoolExecutor(max_workers=workers,
                                 thread_name_prefix='ana') as pool:
            for archive in archives:
                if stop_event.is_set():
                    break
                try:
                    info = analyze_archive(client, archive, pool, progress,
                                           stop_event, logger,
                                           folder_include, folder_exclude)
                    result_archives.append(info)
                except AuthExpired:
                    aborted = True
                    stop_event.set()
                    break
                except Exception as e:
                    logger.exception(f'Errore su archivio {archive!r}: {e}')
    finally:
        progress.stop()

    elapsed = time.time() - t0

    # Top folders globali
    all_folders: list[dict] = []
    for arc in result_archives:
        for f in arc.get('folders', []):
            all_folders.append({
                'archive': arc['name'],
                'folder': f['path'],
                'count': f['count'],
            })
    all_folders.sort(key=lambda x: x['count'], reverse=True)

    summary = {
        'scanned_at': datetime.now(timezone.utc).isoformat(),
        'duration_seconds': round(elapsed, 2),
        'host': getattr(client, 'base_url', ''),
        'workers': workers,
        'aborted': aborted or stop_event.is_set(),
        'archive_count': len(result_archives),
        'total_folders': sum(a.get('total_folders', 0) for a in result_archives),
        'total_messages': sum(a.get('total_messages', 0) for a in result_archives),
        'top_folders': all_folders[:50],
        'archives': result_archives,
    }

    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Report a video
    print()
    print('=' * 70)
    print('ANALISI COMPLETATA')
    print('=' * 70)
    print(f'Tempo:                 {elapsed/60:,.2f} minuti ({elapsed:,.1f}s)')
    print(f'Archivi analizzati:    {len(result_archives)}')
    print(f'Cartelle totali:       {summary["total_folders"]:,}')
    print(f'Messaggi totali:       {summary["total_messages"]:,}')
    avg_per_folder = (summary['total_messages'] / summary['total_folders']
                      if summary['total_folders'] else 0)
    print(f'Media per cartella:    {avg_per_folder:,.0f}')
    print()
    print('Stima tempo download a 50 msg/s: '
          f'{summary["total_messages"]/50/3600:,.1f} h')
    print('Stima tempo download a 200 msg/s: '
          f'{summary["total_messages"]/200/3600:,.1f} h')
    print()
    print('--- Top 20 cartelle per messaggi ---')
    for i, f in enumerate(all_folders[:20], 1):
        line = f'{i:>3}. {f["count"]:>8,}  {f["archive"]}/{f["folder"]}'
        print(line[:130])
    print()
    print('--- Top 20 archivi per messaggi ---')
    arc_sorted = sorted(result_archives, key=lambda a: a.get('total_messages', 0),
                        reverse=True)
    for i, arc in enumerate(arc_sorted[:20], 1):
        print(f'{i:>3}. {arc.get("total_messages", 0):>8,}  '
              f'{arc.get("total_folders", 0):>4} cartelle  {arc["name"]}')
    print()
    print(f'JSON salvato in: {json_path}')

    return summary


def export_archive(client: WebClient, archive: str, output_root: Path,
                   state: WebApiStateDB, progress: Progress, cfg: dict,
                   stop_event: threading.Event, logger: logging.Logger,
                   folder_include: list[re.Pattern],
                   folder_exclude: list[re.Pattern],
                   download_pool: ThreadPoolExecutor | None = None):
    """Enumera tutte le cartelle di un archivio e le esporta."""
    if stop_event.is_set():
        return

    logger.info(f'=== ARCHIVIO: {archive} ===')
    try:
        folders = client.list_all_folders(archive)
    except AuthExpired:
        raise
    except ApiError as e:
        logger.error(f'Enumerazione cartelle fallita per archivio {archive!r}: {e}')
        return

    if folder_include:
        folders = [f for f in folders if any(r.search(f) for r in folder_include)]
    if folder_exclude:
        folders = [f for f in folders if not any(r.search(f) for r in folder_exclude)]

    logger.info(f'{archive}: {len(folders)} cartelle dopo filtri')
    if not cfg.get('known_total'):
        progress.inc(total_folders=len(folders))

    for folder_path in folders:
        if stop_event.is_set():
            break
        export_folder(client, archive, folder_path, output_root, state,
                      progress, cfg, stop_event, logger,
                      download_pool=download_pool)


# ============================================================
# WIZARD
# ============================================================

def ask_bearer_token() -> str:
    """Prompt mascherato per il Bearer token."""
    print()
    print('Per ottenere il token:')
    print('  1. Logga in MailStore Web Access (https://HOST:8462/app/)')
    print('  2. DevTools → Network → seleziona una request /api/...')
    print('  3. Header "Authorization: Bearer XXXX" → copia XXXX')
    print()
    while True:
        try:
            t = getpass.getpass('Bearer token (input mascherato): ').strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(130)
        if t.lower().startswith('bearer '):
            t = t[7:].strip()
        if t:
            return t
        print('  Token vuoto, riprova.')


def run_wizard(defaults: dict) -> dict:
    banner('MailStore Export via Web API — Wizard interattivo')

    section('1/4 — Connessione Web API')
    host = ask('Host MailStore', default=defaults.get('host') or '127.0.0.1')
    port = ask_int('Porta Web Access', default=defaults.get('port') or 8462,
                   min_val=1, max_val=65535)
    base_url = f'https://{host}:{port}'

    username = defaults.get('username') or ask('Username MailStore',
                                               default='admin')
    password = defaults.get('password') or ask_password('Password')

    client = WebClient(base_url, username=username, password=password,
                       verify_tls=False)

    print()
    print(f'  Login e verifica API a {base_url}...')
    try:
        client.authenticate()
    except AuthExpired as e:
        print(f'  {sym("✗", "X")} Login fallito: {e.message}')
        sys.exit(2)
    except ApiError as e:
        print(f'  {sym("✗", "X")} Errore di rete: {e}')
        sys.exit(2)

    try:
        archives = client.get_archives()
    except AuthExpired:
        print(f'  {sym("✗", "X")} Token non valido (errore inatteso post-login).')
        sys.exit(2)
    except ApiError as e:
        print(f'  {sym("✗", "X")} API error: {e}')
        sys.exit(2)
    print(f'  {sym("✓", "OK")} Connesso come {username}. Trovati {len(archives)} archivi.')

    section('2/4 — Selezione archivi')
    chosen = select_archives_interactive(archives)
    if not chosen:
        print('Nessun archivio selezionato. Esco.')
        sys.exit(0)

    print()
    print('Filtri opzionali sulle cartelle (regex, vuoto per disattivare):')
    include_re_str = ask('Includi solo cartelle che matchano regex', default='',
                         allow_empty=True) if False else ''  # default off
    # Più semplice: chiediamo solo l'exclude (caso più comune: trash/spam)
    exclude_re_str = ask('Pattern regex per ESCLUDERE cartelle (vuoto = nessuna esclusione)',
                         default='', allow_empty=True)

    section('3/4 — Cartella di output')
    default_out = defaults.get('output') or str(Path.cwd() / 'mailstore_webapi_export')
    output = ask_path('Path cartella output', default=default_out)

    section('4/4 — Opzioni')
    workers = ask_int('Worker paralleli (download, consigliato 16-64 su LAN; '
                      'oltre 200 il server tende a saturare)',
                      default=defaults.get('workers') or 32,
                      min_val=1, max_val=None)
    dedup = ask_yes_no('Deduplicare via Message-ID '
                       '(salta duplicati cross-archive/folder)?',
                       default=bool(defaults.get('dedup_message_id')))
    split_by_year = ask_yes_no('Suddividere in sottocartelle per anno dentro '
                               'ogni archivio (es. "archivio/2023/")?',
                               default=bool(defaults.get('split_by_year')))

    section('Riepilogo')
    print(f'  URL         : {base_url}')
    print(f'  Output      : {output}')
    print(f'  Archivi     : {len(chosen)} selezionati')
    if exclude_re_str:
        print(f'  Exclude     : {exclude_re_str}')
    print(f'  Workers     : {workers}')
    print(f'  Dedup Msg-ID: {"sì" if dedup else "no"}')
    print(f'  Split anno  : {"sì" if split_by_year else "no"}')
    print()
    if not ask_yes_no('Avviare l\'export?', default=True):
        print('Annullato.')
        sys.exit(0)

    return {
        'host': host, 'port': port,
        'username': username, 'password': password,
        'output': output, 'workers': workers,
        'archives': chosen,
        'folder_exclude': [exclude_re_str] if exclude_re_str else [],
        'folder_include': [include_re_str] if include_re_str else [],
        'dedup_message_id': dedup,
        'split_by_year': split_by_year,
    }


# ============================================================
# LOGGING
# ============================================================

def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger('mailstore_webapi_export')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s'))
    logger.addHandler(fh)
    return logger


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Export MailStore via Web Access REST API -> .eml locali',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--host', default=None, help='Host MailStore')
    parser.add_argument('--port', type=int, default=None,
                        help='Porta Web Access (default 8462)')
    parser.add_argument('--user', '--username', dest='username', default=None,
                        help='Username MailStore')
    parser.add_argument('--password', default=None,
                        help='Password (se omessa viene chiesta a runtime)')
    parser.add_argument('--token', default=None,
                        help='Bearer token statico (alternativa a user/password '
                             'ma scade in 5 min)')
    parser.add_argument('--output', type=Path, default=None,
                        help='Cartella output')
    parser.add_argument('--archive', action='append', default=[],
                        help='Nome archivio da esportare (ripetibile)')
    parser.add_argument('--exclude-folder', action='append', default=[],
                        help='Regex per escludere cartelle')
    parser.add_argument('--include-folder', action='append', default=[],
                        help='Regex per includere solo certe cartelle')
    parser.add_argument('--workers', type=int, default=32,
                        help='Worker paralleli per download (default 32, '
                             'consigliato 16-64 su LAN gigabit)')
    parser.add_argument('--dedup-message-id', action='store_true',
                        help='Salta email con Message-ID già visto '
                             '(deduplica cross-archive/folder)')
    parser.add_argument('--split-by-year', action='store_true',
                        help='Suddividi in sottocartelle per anno dentro ogni '
                             'archivio (es. "li - liquidatori/2023/"). L\'anno '
                             'viene dall\'header Date; mail senza data -> "0000".')
    parser.add_argument('--count-first', action='store_true',
                        help='Conta TUTTI i messaggi prima di scaricare, così la '
                             'progress bar ha un totale fisso (no aggiornamenti '
                             'continui). Aggiunge una pre-passata di conteggio.')
    parser.add_argument('--year', type=int, action='append', default=[],
                        help='Esporta solo le email di questo anno (ripetibile, '
                             'es. --year 2023 --year 2024). Filtro applicato '
                             'PRIMA del download. Mail senza data vengono escluse.')
    parser.add_argument('--month', type=int, action='append', default=[],
                        help='Esporta solo le email di questo mese 1-12 '
                             '(ripetibile). Ha senso con un solo --year.')
    parser.add_argument('--env-file', type=Path, default=None,
                        help='File .env da caricare (default: ./.env)')
    parser.add_argument('-i', '--interactive', action='store_true',
                        help='Forza il wizard')
    parser.add_argument('--no-interactive', action='store_true',
                        help='Disabilita il wizard')
    parser.add_argument('--analyze', action='store_true',
                        help='Modalità analisi: enumera cartelle e conta '
                             'messaggi SENZA scaricare. Salva un JSON di stats.')
    parser.add_argument('--analyze-output', type=Path, default=None,
                        help='Path del JSON di output per --analyze '
                             '(default: ./mailstore_analysis_YYYYMMDD_HHMMSS.json)')
    parser.add_argument('--analyze-all', action='store_true',
                        help='Con --analyze, scansiona TUTTI gli archivi '
                             '(salta la selezione interattiva)')
    parser.add_argument('--benchmark', action='store_true',
                        help='Modalità benchmark: misura msg/s a vari worker '
                             'count e suggerisce l\'ottimo. NON salva i .eml.')
    parser.add_argument('--benchmark-folder', default=None,
                        help='Cartella di test (formato "archive/path/sub"). '
                             'Se omessa, auto-detect.')
    parser.add_argument('--benchmark-samples', type=int, default=200,
                        help='Numero di messaggi da scaricare per ogni step '
                             '(default 200)')
    parser.add_argument('--benchmark-workers', default='4,8,16,32,48,64,96,128',
                        help='Lista CSV di worker count da provare '
                             '(default: 4,8,16,32,48,64,96,128)')
    parser.add_argument('--benchmark-output', type=Path, default=None,
                        help='Path JSON di output per --benchmark '
                             '(default: ./mailstore_benchmark_YYYYMMDD_HHMMSS.json)')
    parser.add_argument('--scan-tmp', action='store_true',
                        help='Dry-run: scansiona --output e lista i .eml.tmp '
                             'orfani senza cancellare nulla. Non richiede '
                             'credenziali MailStore.')
    parser.add_argument('--scan-tmp-output', type=Path, default=None,
                        help='Con --scan-tmp, salva il report come JSON nel '
                             'path indicato.')
    parser.add_argument('--list-failed', action='store_true',
                        help='Dumpa la tabella failed_messages dello state.db '
                             'in JSON (stdout o file). Non richiede credenziali.')
    parser.add_argument('--list-failed-output', type=Path, default=None,
                        help='Con --list-failed, salva il JSON nel path '
                             'indicato (default: stdout).')
    parser.add_argument('--exclude-failed', action='store_true',
                        help='Durante l\'export, salta i gid/mid presenti '
                             'nella tabella failed_messages dello state.db.')
    parser.add_argument('--exclude-from', type=Path, default=None,
                        help='Durante l\'export, salta i gid/mid elencati nel '
                             'JSON indicato (formato output di --list-failed).')
    args = parser.parse_args()

    # Carica .env (CLI args hanno priorità su env)
    env = load_env_file(args.env_file)
    env_host = env.get('MAILSTORE_HOST')
    env_port = env.get('MAILSTORE_PORT')
    env_user = env.get('MAILSTORE_USER')
    env_pass = env.get('MAILSTORE_PASSWORD')
    env_token = env.get('MAILSTORE_TOKEN')
    env_output = env.get('MAILSTORE_OUTPUT')
    env_workers = env.get('MAILSTORE_WORKERS')
    env_archives = env.get('MAILSTORE_ARCHIVES')  # comma-separated

    eff_host = args.host or env_host
    eff_port = args.port or (int(env_port) if env_port else None)
    eff_username = args.username or env_user
    eff_password = args.password or env_pass
    eff_token = args.token or env_token
    eff_output = args.output or (Path(env_output) if env_output else None)
    eff_workers = args.workers if args.workers != 32 else (
        int(env_workers) if env_workers else 32)
    eff_archives = list(args.archive) if args.archive else (
        [s.strip() for s in env_archives.split(',') if s.strip()]
        if env_archives else [])

    # --scan-tmp: dry-run filesystem-only, no credenziali necessarie.
    if args.scan_tmp:
        if eff_output is None:
            print('ERROR: --scan-tmp richiede --output (o MAILSTORE_OUTPUT in .env)',
                  file=sys.stderr)
            sys.exit(2)
        root = eff_output.expanduser().resolve()
        if not root.exists():
            print(f'ERROR: directory non esiste: {root}', file=sys.stderr)
            sys.exit(2)
        count = _scan_orphan_tmp(root, args.scan_tmp_output)
        # exit code: 0 se nessun orfano, 1 se trovati (utile per script)
        sys.exit(0 if count == 0 else 1)

    # --list-failed: dumpa la tabella failed_messages dello state.db. Solo
    # filesystem (lettura DB), no credenziali API.
    if args.list_failed:
        if eff_output is None:
            print('ERROR: --list-failed richiede --output (per trovare lo state.db)',
                  file=sys.stderr)
            sys.exit(2)
        root = eff_output.expanduser().resolve()
        state_dir = root / '.mailstore_webapi_export'
        db_path = state_dir / 'state.db'
        if not db_path.exists():
            print(f'ERROR: state.db non trovato in {db_path}', file=sys.stderr)
            sys.exit(2)
        state = WebApiStateDB(db_path)
        count = _dump_failed_messages(state, args.list_failed_output)
        sys.exit(0 if count == 0 else 1)

    has_creds = bool(eff_token) or bool(eff_username)
    # In modalità --analyze --analyze-all, gli archivi vengono presi dopo il login.
    # In --benchmark, basta avere le credenziali — output e archivi opzionali.
    if args.benchmark:
        if eff_output is None:
            eff_output = Path.cwd() / 'mailstore_webapi_export'  # solo per state/log
        missing = not has_creds
    elif args.analyze and args.analyze_all:
        if eff_output is None:
            eff_output = Path.cwd() / 'mailstore_analysis_output'
        missing = not has_creds
    else:
        missing = not has_creds or eff_output is None or not eff_archives
    use_wizard = args.interactive or (
        missing and not args.no_interactive and _is_interactive_tty()
    )

    if env:
        print(f'.env caricato: {len(env)} variabili '
              f'({", ".join(k for k in env if not k.endswith("PASSWORD"))})')

    username = None
    password = None
    token = None

    if use_wizard:
        cfg = run_wizard({
            'host': eff_host, 'port': eff_port,
            'username': eff_username, 'password': eff_password,
            'output': str(eff_output) if eff_output else None,
            'workers': eff_workers,
            'dedup_message_id': args.dedup_message_id,
            'split_by_year': args.split_by_year,
        })
        host = cfg['host']; port = cfg['port']
        username = cfg['username']; password = cfg['password']
        output_root = Path(cfg['output'])
        archives = cfg['archives']
        workers = cfg['workers']
        folder_exclude = cfg['folder_exclude']
        folder_include = cfg['folder_include']
        dedup = cfg['dedup_message_id']
        split_by_year = cfg['split_by_year']
    else:
        if missing:
            print('ERRORE: servono (--user [--password] OPPURE --token) + '
                  '--output + almeno un --archive in modalità non interattiva. '
                  '(Puoi anche usare un file .env)', file=sys.stderr)
            sys.exit(2)
        host = eff_host or '127.0.0.1'
        port = eff_port or 8462
        token = eff_token
        username = eff_username
        password = eff_password
        if username and not password:
            if _is_interactive_tty():
                password = ask_password(f'Password per {username}@{host}')
            else:
                print('ERRORE: password mancante e nessun TTY.',
                      file=sys.stderr)
                sys.exit(2)
        output_root = eff_output
        archives = eff_archives
        workers = eff_workers
        folder_exclude = args.exclude_folder
        folder_include = args.include_folder
        dedup = args.dedup_message_id
        split_by_year = args.split_by_year

    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    if IS_MACOS:
        marker = output_root / '.metadata_never_index'
        if not marker.exists():
            try:
                marker.touch()
            except OSError:
                pass

    state_dir = output_root / '.mailstore_webapi_export'
    state_dir.mkdir(exist_ok=True)
    db_path = state_dir / 'state.db'
    log_path = state_dir / f'export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

    logger = setup_logging(log_path)
    logger.info('=' * 60)
    logger.info(f'Avvio export Web API: host={host}:{port} '
                f'archives={len(archives)} workers={workers} output={output_root}')

    # Cleanup .eml.tmp orfani da run precedenti: open('wb') li sovrascriverebbe
    # solo se non lockati, e con 48 worker il rumore in log esplode. Meglio
    # ripulire ora (best-effort) — i file rimasti lockati verranno skippati.
    _cleanup_orphan_tmp(output_root, logger)

    base_url = f'https://{host}:{port}'
    client = WebClient(base_url, token=token, username=username,
                       password=password, verify_tls=False)

    # In analyze mode possiamo skippare la selezione e usare tutti gli archivi
    if args.analyze and args.analyze_all:
        try:
            all_arc = client.get_archives()
            # Skip archivi vuoti (containsFolders: false): API ritorna 404 sulle
            # cartelle di archivi senza messaggi.
            archives = [a['name'] for a in all_arc if a.get('containsFolders')]
            skipped = len(all_arc) - len(archives)
            print(f'Analisi globale: {len(archives)} archivi non vuoti '
                  f'(skip {skipped} vuoti su {len(all_arc)} totali).')
        except AuthExpired:
            print(f'{sym("✗", "X")} Autenticazione fallita.', file=sys.stderr)
            sys.exit(2)
        except ApiError as e:
            print(f'{sym("✗", "X")} API error: {e}', file=sys.stderr)
            sys.exit(2)

    state = WebApiStateDB(db_path)

    include_re = [re.compile(p) for p in folder_include]
    exclude_re = [re.compile(p) for p in folder_exclude]

    # Signal handling
    stop_event = threading.Event()
    warn = sym('⚠', '!')
    def handle_sig(signum, frame):
        if not stop_event.is_set():
            print(f'\n\n{warn}  Interruzione richiesta, completo l\'operazione in corso e chiudo...\n',
                  file=sys.stderr)
            stop_event.set()
        else:
            print(f'\n{warn}  Seconda interruzione, exit forzato.\n', file=sys.stderr)
            os._exit(130)
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    # === BENCHMARK MODE ===
    if args.benchmark:
        bench_json = args.benchmark_output or (
            Path.cwd() / f'mailstore_benchmark_'
                         f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
        try:
            client._ensure_token()
        except AuthExpired as e:
            print(f'{sym("✗", "X")} Login fallito: {e.message}', file=sys.stderr)
            sys.exit(2)

        # Determina cartella di test
        if args.benchmark_folder:
            parts = args.benchmark_folder.split('/', 1)
            bench_archive = parts[0]
            bench_folder = parts[1] if len(parts) > 1 else ''
        else:
            # Auto-detect: pesca la prima cartella >= benchmark_samples
            print('Auto-detect della cartella di test (cerco la prima con '
                  f'>= {args.benchmark_samples} messaggi)...')
            candidates = archives if archives else [
                a['name'] for a in client.get_archives() if a.get('containsFolders')
            ]
            try:
                bench_archive, bench_folder, total = _pick_benchmark_folder(
                    client, candidates, args.benchmark_samples, logger)
                print(f'Trovata: {bench_archive}/{bench_folder} '
                      f'({total} messaggi totali)')
            except RuntimeError as e:
                print(f'{sym("✗", "X")} {e}', file=sys.stderr)
                sys.exit(2)

        try:
            worker_list = [int(x.strip()) for x in
                           args.benchmark_workers.split(',') if x.strip()]
        except ValueError:
            print('ERRORE: --benchmark-workers deve essere CSV di interi',
                  file=sys.stderr)
            sys.exit(2)

        run_benchmark(client, bench_archive, bench_folder,
                      args.benchmark_samples, worker_list, bench_json, logger)
        return

    # === ANALYZE MODE ===
    if args.analyze:
        json_path = args.analyze_output or (
            output_root / f'mailstore_analysis_'
                          f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
        # I worker per analyze sono più conservativi: la search-cache di
        # MailStore satura facilmente con >8 search concorrenti.
        analyze_workers = min(workers, 8)
        if analyze_workers != workers:
            logger.info(f'Workers ridotti per analyze: {workers} -> '
                        f'{analyze_workers} (limite cache search MailStore)')
        logger.info(f'ANALYZE MODE: workers={analyze_workers} '
                    f'archives={len(archives)} output={json_path}')
        try:
            client._ensure_token()  # login se serve
        except AuthExpired as e:
            print(f'{sym("✗", "X")} Login fallito: {e.message}', file=sys.stderr)
            sys.exit(2)
        run_analyze(client, archives, analyze_workers, output_root,
                    stop_event, logger, json_path, include_re, exclude_re)
        return

    progress = Progress()
    progress.start()

    # Skip-list: unione di failed_messages (se --exclude-failed) e JSON esterno
    excluded_keys: set[str] = set()
    if args.exclude_failed:
        excluded_keys |= state.get_failed_keys()
        logger.info('--exclude-failed: %d gid/mid in skip-list da state.db',
                    len(excluded_keys))
    if args.exclude_from:
        loaded = _load_excluded_from_json(args.exclude_from)
        excluded_keys |= loaded
        logger.info('--exclude-from %s: +%d gid/mid (totale skip-list: %d)',
                    args.exclude_from, len(loaded), len(excluded_keys))

    cfg = {'workers': workers, 'dedup_message_id': dedup,
           'split_by_year': split_by_year,
           'years': set(args.year or []),
           'months': set(args.month or []),
           'excluded_keys': excluded_keys,
           'known_total': bool(args.count_first)}
    if cfg['years'] or cfg['months']:
        logger.info('Filtro periodo: anni=%s mesi=%s',
                    sorted(cfg['years']) or 'tutti', sorted(cfg['months']) or 'tutti')

    # Optional pre-count: fixes the progress-bar total before downloading.
    if args.count_first:
        print('Pre-conteggio messaggi (la barra avrà un totale fisso)…')
        try:
            grand = precount(client, archives, workers, stop_event, logger,
                             progress, include_re, exclude_re)
            print(f'  totale da scaricare: {grand:,} messaggi')
        except AuthExpired:
            print(f'{sym("✗", "X")} Autenticazione fallita.', file=sys.stderr)
            sys.exit(2)

    t0 = time.time()
    aborted_auth = False
    try:
        # Pool di download condiviso, riusato attraverso tutti gli archivi/cartelle
        with ThreadPoolExecutor(max_workers=workers,
                                 thread_name_prefix='dl') as download_pool:
            for archive in archives:
                if stop_event.is_set():
                    break
                try:
                    export_archive(client, archive, output_root, state,
                                   progress, cfg, stop_event, logger,
                                   include_re, exclude_re,
                                   download_pool=download_pool)
                except AuthExpired:
                    logger.error('Autenticazione fallita durante l\'export.')
                    aborted_auth = True
                    stop_event.set()
                    break
                except Exception as e:
                    logger.exception(f'Errore su archivio {archive!r}: {e}')
    finally:
        progress.stop()

    elapsed = time.time() - t0
    stats = state.api_stats()
    print()
    print('=' * 60)
    print('REPORT FINALE')
    print('=' * 60)
    print(f'Tempo totale:          {elapsed/60:,.1f} minuti ({elapsed:,.0f}s)')
    print(f'Cartelle processate:   {progress.done_folders}/{progress.total_folders}')
    print(f'Messaggi esportati:    {progress.done_msgs:,}')
    print(f'Messaggi saltati:      {progress.skipped_msgs:,}')
    print(f'  • già esportati:     {progress.skip_resume:,} (resume da state.db)')
    print(f'  • duplicati Msg-ID:  {progress.skip_dedup:,} (--dedup-message-id)')
    print(f'  • in skip-list:      {progress.skip_excluded:,} (--exclude-failed/--exclude-from)')
    print(f'  • .tmp orfani:       {progress.skip_tmp:,} (lock/crash precedente)')
    if progress.skip_filter:
        print(f'Esclusi da filtro anno/mese: {progress.skip_filter:,}')
    print(f'Messaggi falliti:      {progress.failed_msgs:,}')
    print(f'Volume in DB:          {stats["total_size"] / (1024**3):,.2f} GB '
          f'({stats["count"]:,} email)')
    rate = progress.done_msgs / elapsed if elapsed else 0
    print(f'Velocità media:        {rate:,.0f} msg/s')
    print(f'Log:                   {log_path}')
    print(f'State DB:              {db_path}')
    print()
    if aborted_auth:
        print(f'{warn}  Autenticazione fallita. Verifica credenziali e rilancia '
              f'(lo state.db riprende da dove si era fermato).')
    elif stop_event.is_set():
        print(f'{warn}  Export interrotto. Rilancia lo stesso comando per riprendere.')
    else:
        print(f'{sym("✓", "OK")}  Export completato.')
    logger.info('Export terminato.')


if __name__ == '__main__':
    main()
