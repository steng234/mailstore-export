#!/usr/bin/env python3
"""
MailStore Web Access API — Probe / Discovery Tool
=================================================
Strumento interattivo per esplorare la Web Access API di MailStore Server
(porta default 8462, HTTPS, cert self-signed). Gli endpoint non sono pubblici
quindi vanno scoperti per reverse engineering.

Cosa fa:
  1. Login con vari pattern noti (tenta automaticamente)
  2. Salva i cookie di sessione
  3. Dump degli endpoint comuni
  4. REPL interattiva per testare endpoint custom

Una volta capita la struttura, replicheremo qui la logica di enumerazione
e download delle email.

Stdlib only, Python 3.8+, cross-platform.
"""

from __future__ import annotations

import getpass
import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ============================================================
# Helpers
# ============================================================

def _supports_unicode() -> bool:
    enc = (getattr(sys.stdout, 'encoding', None) or '').lower()
    return 'utf' in enc


_UNICODE_OK = _supports_unicode()


def sym(u: str, a: str) -> str:
    return u if _UNICODE_OK else a


def hr(c: str = '=') -> str:
    return c * 80


def ask(prompt: str, default: str | None = None) -> str:
    sfx = f' [{default}]' if default is not None else ''
    while True:
        try:
            v = input(f'{prompt}{sfx}: ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if v:
            return v
        if default is not None:
            return default
        print('  Valore richiesto.')


def ask_password(prompt: str = 'Password') -> str:
    try:
        return getpass.getpass(f'{prompt}: ')
    except (EOFError, KeyboardInterrupt):
        sys.exit(130)


# ============================================================
# HTTP client
# ============================================================

class WebClient:
    """HTTPS client con cookie jar e tolleranza cert self-signed."""

    def __init__(self, base_url: str, verify_tls: bool = False, timeout: float = 30.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()

        ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        cookie_handler = urllib.request.HTTPCookieProcessor(self.jar)
        # Non seguire i redirect automaticamente — vogliamo vederli
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        self.opener = urllib.request.build_opener(https_handler, cookie_handler,
                                                   NoRedirect())
        self.opener.addheaders = [
            ('User-Agent', 'Mozilla/5.0 mailstore-probe/1.0'),
            ('Accept', 'application/json, text/html, */*'),
        ]
        self.last_response = None  # type: tuple | None

    def request(self, method: str, path: str,
                json_body: Any | None = None,
                form_body: dict | None = None,
                raw_body: bytes | None = None,
                extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
        url = path if path.startswith('http') else self.base_url + path

        data: bytes | None = None
        headers: dict[str, str] = {}
        if json_body is not None:
            data = json.dumps(json_body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif raw_body is not None:
            data = raw_body
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            status = resp.status
            hdrs = dict(resp.headers.items())
            body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            hdrs = dict(e.headers.items())
            body = e.read()
        except Exception as e:
            status = 0
            hdrs = {'X-Client-Error': str(e)}
            body = b''

        self.last_response = (status, hdrs, body)
        return status, hdrs, body

    def get(self, path: str, **kw):
        return self.request('GET', path, **kw)

    def post(self, path: str, **kw):
        return self.request('POST', path, **kw)

    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self.jar}


# ============================================================
# Pretty print
# ============================================================

def pretty(status: int, hdrs: dict, body: bytes, max_body: int = 1200) -> None:
    print(f'  status: {status}')
    interesting = ['Content-Type', 'Location', 'Set-Cookie', 'X-Client-Error',
                   'WWW-Authenticate']
    for k in interesting:
        if k in hdrs:
            print(f'  {k}: {hdrs[k]}')
    ct = hdrs.get('Content-Type', '')
    raw = body[:max_body]
    decoded = raw.decode('utf-8', errors='replace')
    if 'json' in ct.lower():
        try:
            obj = json.loads(body.decode('utf-8', errors='replace'))
            text = json.dumps(obj, indent=2, ensure_ascii=False)
            if len(text) > max_body:
                text = text[:max_body] + '\n  ... [troncato]'
            print('  body (JSON):')
            for line in text.splitlines():
                print(f'    {line}')
            return
        except Exception:
            pass
    print(f'  body[{len(body)} bytes, mostrati {len(raw)}]:')
    for line in decoded.splitlines()[:30]:
        print(f'    {line}')
    if len(body) > max_body:
        print('    ... [troncato]')


# ============================================================
# Login attempts
# ============================================================

LOGIN_CANDIDATES: list[tuple[str, str, str]] = [
    # (descrizione, path, body_kind)
    # body_kind in {'json:userName', 'json:username', 'form:userName', 'form:username'}
    ('REST MailStore Web Access classic', '/api/users/login', 'json:userName'),
    ('REST variant',                       '/api/login',        'json:userName'),
    ('REST snake_case',                    '/api/login',        'json:username'),
    ('REST capitalized',                   '/api/Login',        'json:userName'),
    ('REST auth namespace',                '/api/auth/login',   'json:username'),
    ('Form-encoded login',                 '/api/login',        'form:userName'),
    ('Form-encoded /Login',                '/Login',            'form:userName'),
    ('Form-encoded /login',                '/login',            'form:userName'),
]


def try_login(client: WebClient, user: str, password: str) -> str | None:
    print()
    print(hr('='))
    print('FASE 1 — DISCOVERY ENDPOINT DI LOGIN')
    print(hr('='))
    for desc, path, kind in LOGIN_CANDIDATES:
        if kind.startswith('json:'):
            key = kind.split(':', 1)[1]
            body = {key: user, 'password': password}
            kwargs = {'json_body': body}
        else:
            key = kind.split(':', 1)[1]
            body = {key: user, 'password': password}
            kwargs = {'form_body': body}

        print()
        print(f'> {desc}')
        print(f'  POST {path}  body_kind={kind}')
        status, hdrs, content = client.request('POST', path, **kwargs)
        pretty(status, hdrs, content, max_body=400)

        cookies = client.cookies()
        # Euristica "successo":
        # - status 200 con cookies sessione settati
        # - oppure 302 con Set-Cookie session
        ok_cookie_set = any('session' in n.lower() or 'auth' in n.lower() or
                             'asp' in n.lower() or 'mailstore' in n.lower()
                             for n in cookies)
        looks_like_html_login = b'login' in content.lower() and b'<form' in content.lower()

        if (status in (200, 204) and ok_cookie_set and not looks_like_html_login):
            print(f'  {sym("✓", "OK")} Possibile successo. Cookies: {list(cookies.keys())}')
            return path
    print()
    print(f'{sym("✗", "X")} Nessun login candidato ha dato un successo evidente.')
    print('  Provare con --browser-cookie (vedi sotto) oppure manualmente.')
    return None


# ============================================================
# Endpoint probe
# ============================================================

PROBE_PATHS = [
    # path                              hint
    '/api',
    '/api/help',
    '/api/info',
    '/api/me',
    '/api/users',
    '/api/users/me',
    '/api/archives',
    '/api/folders',
    '/api/folders/root',
    '/api/folder',
    '/api/folder/root',
    '/api/messages',
    '/api/messages/search',
    '/api/search',
    '/api/server/info',
    '/api/serverinfo',
    '/api/version',
    '/api/configuration',
]


def probe_endpoints(client: WebClient) -> None:
    print()
    print(hr('='))
    print('FASE 2 — PROBE ENDPOINT')
    print(hr('='))
    for path in PROBE_PATHS:
        print()
        print(f'GET {path}')
        status, hdrs, body = client.get(path)
        pretty(status, hdrs, body, max_body=600)


# ============================================================
# Interactive REPL
# ============================================================

REPL_HELP = """
Comandi:
  get PATH                       GET su PATH (relativo al base_url)
  post PATH                      POST vuoto su PATH
  postj PATH                     POST + chiede body JSON (singola riga)
  postf PATH key=val key=val     POST form-urlencoded
  cookies                        mostra cookies correnti
  base                           mostra base_url
  save FILE                      salva l'ultimo response body su FILE
  raw                            stampa l'ultimo response body completo
  json                           stampa l'ultimo response body come JSON pretty
  probe                          ri-esegui il probe automatico
  help                           questo messaggio
  quit / exit / q                esci
"""


def repl(client: WebClient) -> None:
    print()
    print(hr('='))
    print('FASE 3 — REPL INTERATTIVA')
    print(hr('='))
    print(REPL_HELP)
    while True:
        try:
            cmd = input(f'webapi> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not cmd:
            continue
        parts = cmd.split(maxsplit=1)
        verb = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ''

        if verb in ('quit', 'exit', 'q'):
            return
        if verb == 'help':
            print(REPL_HELP)
            continue
        if verb == 'cookies':
            print('  ', client.cookies())
            continue
        if verb == 'base':
            print('  ', client.base_url)
            continue
        if verb == 'probe':
            probe_endpoints(client)
            continue
        if verb == 'raw':
            if client.last_response:
                _, _, body = client.last_response
                sys.stdout.buffer.write(body)
                print()
            continue
        if verb == 'json':
            if client.last_response:
                _, _, body = client.last_response
                try:
                    print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
                except Exception as e:
                    print(f'  Not JSON: {e}')
            continue
        if verb == 'save':
            if not rest:
                print('  Uso: save NOME_FILE')
                continue
            if client.last_response:
                _, _, body = client.last_response
                try:
                    with open(rest, 'wb') as f:
                        f.write(body)
                    print(f'  Salvato {len(body)} byte in {rest}')
                except OSError as e:
                    print(f'  Errore: {e}')
            continue
        if verb == 'get':
            if not rest:
                print('  Uso: get /api/...')
                continue
            status, hdrs, body = client.get(rest)
            pretty(status, hdrs, body, max_body=2000)
            continue
        if verb == 'post':
            if not rest:
                print('  Uso: post /api/...')
                continue
            status, hdrs, body = client.post(rest)
            pretty(status, hdrs, body, max_body=2000)
            continue
        if verb == 'postj':
            if not rest:
                print('  Uso: postj /api/...')
                continue
            raw = input('  body JSON: ').strip()
            try:
                obj = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                print(f'  JSON non valido: {e}')
                continue
            status, hdrs, body = client.post(rest, json_body=obj)
            pretty(status, hdrs, body, max_body=2000)
            continue
        if verb == 'postf':
            if not rest:
                print('  Uso: postf /api/... key=val key=val')
                continue
            tokens = rest.split()
            path = tokens[0]
            fields = {}
            for tok in tokens[1:]:
                if '=' in tok:
                    k, v = tok.split('=', 1)
                    fields[k] = v
            status, hdrs, body = client.post(path, form_body=fields)
            pretty(status, hdrs, body, max_body=2000)
            continue

        print(f'  Comando non riconosciuto: {verb!r}. Digita "help".')


# ============================================================
# Main
# ============================================================

def main():
    print()
    print(hr('='))
    print('MailStore Web Access API — Probe / Discovery'.center(80))
    print(hr('='))

    host = ask('Host MailStore', default='127.0.0.1')
    port = ask('Porta (default Web Access)', default='8462')
    scheme = 'https'
    base_url = f'{scheme}://{host}:{port}'
    user = ask('Username MailStore', default='admin')
    password = ask_password()

    print()
    print(f'Base URL: {base_url}')

    client = WebClient(base_url, verify_tls=False)

    # Pre-flight: tocca la root per vedere se il server risponde
    print()
    print('Ping root...')
    status, hdrs, body = client.get('/')
    pretty(status, hdrs, body, max_body=400)
    if status == 0:
        print()
        print(f'{sym("✗", "X")} Impossibile contattare {base_url}: '
              f'{hdrs.get("X-Client-Error")}')
        sys.exit(2)

    # Tentativi di login
    login_path = try_login(client, user, password)
    if not login_path:
        print()
        print('Login non riuscito automaticamente.')
        print('Puoi comunque entrare nella REPL e provare manualmente.')
        if not (ask('Continuare con REPL? [Y/n]', default='Y').lower()
                in ('y', 'yes')):
            return

    # Probe automatico
    if ask('Eseguire il probe automatico degli endpoint? [Y/n]',
           default='Y').lower() in ('y', 'yes'):
        probe_endpoints(client)

    # REPL
    repl(client)


if __name__ == '__main__':
    main()
