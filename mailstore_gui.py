#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MailStore Web API Export — GUI wrapper (Tkinter, stdlib only).

A modern, dark, "Tailwind-like" front-end around `mailstore_webapi_export.py`,
with genuinely rounded widgets (cards, buttons, inputs, progress bar) drawn on
Canvas with smoothed polygons — Tk has no native border-radius.

Design:
  * Short, in-process actions (test connection / list archives) import WebClient
    directly and run in a worker thread.
  * Long actions (export / analyze / benchmark / scan-tmp / list-failed) are run
    as a SUBPROCESS of the CLI script, so all the battle-tested logic stays in
    one place (state.db resume, thread pool, signal handling). Their stdout is
    streamed live into the log pane, and the progress line is parsed to drive
    the rounded progress bar.

Security:
  * Credentials (password / token) are NEVER placed on the subprocess argv
    (they would show up in `ps`). They are written to a private temporary
    env-file (chmod 600) passed via --env-file, and deleted when the run ends.

Python 3.9 compatible (uses `from __future__ import annotations`). Stdlib only.
"""

from __future__ import annotations

import codecs
import datetime
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:  # pragma: no cover - environment without Tk
    sys.stderr.write(
        'ERRORE: Tkinter non disponibile in questo Python.\n'
        f'  ({exc})\n'
        'Su macOS prova con il Python di Homebrew (brew install python-tk@3.12)\n'
    )
    sys.exit(1)


APP_DIR = Path(__file__).resolve().parent
CLI_SCRIPT = APP_DIR / 'mailstore_webapi_export.py'

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8462
DEFAULT_WORKERS = 32

YEAR_MIN = 2000
YEAR_MAX = datetime.date.today().year
MONTHS_IT = ['Gen', 'Feb', 'Mar', 'Apr', 'Mag', 'Giu',
             'Lug', 'Ago', 'Set', 'Ott', 'Nov', 'Dic']

# ---- i18n -----------------------------------------------------------------
LANG_ORDER = ['it', 'en', 'es', 'fr']
LANG_LABEL = {'it': 'IT', 'en': 'EN', 'es': 'ES', 'fr': 'FR'}
T = {
    'it': {
        'card_conn': 'Connessione', 'card_opt': 'Output e opzioni',
        'card_period': 'Periodo  ·  anni / mesi', 'card_arc': 'Archivi',
        'card_out': 'Output',
        'host': 'Host', 'port': 'Porta', 'user': 'Utente', 'pass': 'Password',
        'token': 'Token (opz.)', 'outdir': 'Cartella output', 'workers': 'Worker',
        'browse': '[ sfoglia… ]', 'dedup': 'Dedup per Message-ID',
        'split': 'Suddividi per anno (archivio/AAAA/)',
        'countfirst': 'Conta prima (barra precisa)',
        'inc': 'Include cartelle (regex)', 'exc': 'Escludi cartelle (regex)',
        'years': 'Anni', 'months': 'Mesi', 'all_years': 'Tutti gli anni',
        'all_months': 'Tutti i mesi',
        'date_from': 'Data da', 'date_to': 'Data a',
        'date_hint': 'Range esatto YYYY-MM-DD (anche un solo giorno). '
                     'Ha precedenza su anni/mesi; scarica diretto la finestra.',
        'sel_all': '[ sel. tutti ]',
        'desel_all': '[ desel. tutti ]', 'search': 'cerca',
        'arc_hint': '(premi "Test & carica archivi" per popolare)',
        'no_match': 'nessun archivio corrisponde', 'load_env': '[ carica .env ]',
        'test': '[ test & carica archivi ]', 'export': '[ ▶ EXPORT ]',
        'analyze': '[ analizza ]', 'benchmark': '[ benchmark ]',
        'scantmp': '[ scan .tmp ]', 'failed': '[ lista falliti ]',
        'doctor': '[ doctor ]', 'reconcile': '[ riconcilia ]',
        'retry': '[ ↻ ritenta falliti ]', 'reset_state': '[ ⟲ reset state.db ]',
        'stop': '[ ■ STOP ]', 'ready': 'Pronto.',
        'waiting': "In attesa di un'operazione…", 'lang': 'Lingua',
        'months_abbr': ['Gen', 'Feb', 'Mar', 'Apr', 'Mag', 'Giu',
                        'Lug', 'Ago', 'Set', 'Ott', 'Nov', 'Dic'],
    },
    'en': {
        'card_conn': 'Connection', 'card_opt': 'Output & options',
        'card_period': 'Period  ·  years / months', 'card_arc': 'Archives',
        'card_out': 'Output',
        'host': 'Host', 'port': 'Port', 'user': 'User', 'pass': 'Password',
        'token': 'Token (opt.)', 'outdir': 'Output folder', 'workers': 'Workers',
        'browse': '[ browse… ]', 'dedup': 'Dedup by Message-ID',
        'split': 'Split by year (archive/YYYY/)',
        'countfirst': 'Count first (accurate bar)',
        'inc': 'Include folders (regex)', 'exc': 'Exclude folders (regex)',
        'years': 'Years', 'months': 'Months', 'all_years': 'All years',
        'all_months': 'All months',
        'date_from': 'Date from', 'date_to': 'Date to',
        'date_hint': 'Exact range YYYY-MM-DD (even a single day). Takes '
                     'precedence over years/months; fetches the window directly.',
        'sel_all': '[ select all ]',
        'desel_all': '[ deselect all ]', 'search': 'search',
        'arc_hint': '(press "test & load archives" to populate)',
        'no_match': 'no archive matches', 'load_env': '[ load .env ]',
        'test': '[ test & load archives ]', 'export': '[ ▶ EXPORT ]',
        'analyze': '[ analyze ]', 'benchmark': '[ benchmark ]',
        'scantmp': '[ scan .tmp ]', 'failed': '[ failed list ]',
        'doctor': '[ doctor ]', 'reconcile': '[ reconcile ]',
        'retry': '[ ↻ retry failed ]', 'reset_state': '[ ⟲ reset state.db ]',
        'stop': '[ ■ STOP ]', 'ready': 'Ready.',
        'waiting': 'Waiting for an operation…', 'lang': 'Language',
        'months_abbr': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
    },
    'es': {
        'card_conn': 'Conexión', 'card_opt': 'Salida y opciones',
        'card_period': 'Periodo  ·  años / meses', 'card_arc': 'Archivos',
        'card_out': 'Salida',
        'host': 'Host', 'port': 'Puerto', 'user': 'Usuario', 'pass': 'Contraseña',
        'token': 'Token (opc.)', 'outdir': 'Carpeta de salida',
        'workers': 'Workers', 'browse': '[ explorar… ]',
        'dedup': 'Dedup por Message-ID',
        'split': 'Dividir por año (archivo/AAAA/)',
        'countfirst': 'Contar primero (barra precisa)',
        'inc': 'Incluir carpetas (regex)', 'exc': 'Excluir carpetas (regex)',
        'years': 'Años', 'months': 'Meses', 'all_years': 'Todos los años',
        'all_months': 'Todos los meses',
        'date_from': 'Fecha desde', 'date_to': 'Fecha hasta',
        'date_hint': 'Rango exacto YYYY-MM-DD (incluso un solo día). Tiene '
                     'prioridad sobre años/meses; baja la ventana directamente.',
        'sel_all': '[ sel. todo ]',
        'desel_all': '[ desel. todo ]', 'search': 'buscar',
        'arc_hint': '(pulsa "probar y cargar archivos" para poblar)',
        'no_match': 'ningún archivo coincide', 'load_env': '[ cargar .env ]',
        'test': '[ probar y cargar archivos ]', 'export': '[ ▶ EXPORTAR ]',
        'analyze': '[ analizar ]', 'benchmark': '[ benchmark ]',
        'scantmp': '[ scan .tmp ]', 'failed': '[ lista fallidos ]',
        'doctor': '[ doctor ]', 'reconcile': '[ reconciliar ]',
        'retry': '[ ↻ reintentar ]', 'reset_state': '[ ⟲ reset state.db ]',
        'stop': '[ ■ STOP ]', 'ready': 'Listo.',
        'waiting': 'Esperando una operación…', 'lang': 'Idioma',
        'months_abbr': ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun',
                        'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic'],
    },
    'fr': {
        'card_conn': 'Connexion', 'card_opt': 'Sortie et options',
        'card_period': 'Période  ·  années / mois', 'card_arc': 'Archives',
        'card_out': 'Sortie',
        'host': 'Hôte', 'port': 'Port', 'user': 'Utilisateur',
        'pass': 'Mot de passe', 'token': 'Token (opt.)',
        'outdir': 'Dossier de sortie', 'workers': 'Workers',
        'browse': '[ parcourir… ]', 'dedup': 'Dédup par Message-ID',
        'split': 'Séparer par année (archive/AAAA/)',
        'countfirst': 'Compter d’abord (barre précise)',
        'inc': 'Inclure dossiers (regex)', 'exc': 'Exclure dossiers (regex)',
        'years': 'Années', 'months': 'Mois', 'all_years': 'Toutes les années',
        'all_months': 'Tous les mois',
        'date_from': 'Date de', 'date_to': 'Date à',
        'date_hint': 'Plage exacte YYYY-MM-DD (même un seul jour). Prioritaire '
                     'sur années/mois ; télécharge directement la fenêtre.',
        'sel_all': '[ tout sél. ]',
        'desel_all': '[ tout désél. ]', 'search': 'rechercher',
        'arc_hint': '(cliquez "tester & charger archives" pour remplir)',
        'no_match': 'aucune archive ne correspond', 'load_env': '[ charger .env ]',
        'test': '[ tester & charger archives ]', 'export': '[ ▶ EXPORT ]',
        'analyze': '[ analyser ]', 'benchmark': '[ benchmark ]',
        'scantmp': '[ scan .tmp ]', 'failed': '[ liste échecs ]',
        'doctor': '[ doctor ]', 'reconcile': '[ réconcilier ]',
        'retry': '[ ↻ réessayer ]', 'reset_state': '[ ⟲ reset state.db ]',
        'stop': '[ ■ STOP ]', 'ready': 'Prêt.',
        'waiting': 'En attente d’une opération…', 'lang': 'Langue',
        'months_abbr': ['Jan', 'Fév', 'Mar', 'Avr', 'Mai', 'Jun',
                        'Jul', 'Aoû', 'Sep', 'Oct', 'Nov', 'Déc'],
    },
}

# ============================================================
# THEME SYSTEM — curated "legacy but elegant" palettes, persisted to a small
# config file and switchable at runtime from the header. Default: refined
# phosphor green (less neon, sharp corners). The user can pick another.
# ============================================================
THEMES = {
    'green': {  # refined phosphor green (default) — mature, not neon
        'BG': '#000000', 'CARD': '#0a0f0a', 'FIELD': '#0e160e',
        'FIELD_HI': '#172517', 'FG': '#42c873', 'MUTED': '#2f8a50',
        'BORDER': '#245c37', 'ACCENT': '#54e487', 'ACCENT_HI': '#8ff0b0',
        'ACCENT_LO': '#34ab60', 'DANGER': '#e06a6a', 'DANGER_HI': '#f29494',
    },
    'amber': {  # VT220 amber CRT — warm monochrome
        'BG': '#000000', 'CARD': '#120c00', 'FIELD': '#1b1200',
        'FIELD_HI': '#271a00', 'FG': '#ffb000', 'MUTED': '#9c6f12',
        'BORDER': '#5a3d00', 'ACCENT': '#ffc233', 'ACCENT_HI': '#ffd970',
        'ACCENT_LO': '#cc8c00', 'DANGER': '#ff6a4d', 'DANGER_HI': '#ff9580',
    },
    'mono': {  # minimal mainframe — soft grey-green, restrained
        'BG': '#0c0f0d', 'CARD': '#12160f', 'FIELD': '#191e15',
        'FIELD_HI': '#232a1d', 'FG': '#b9c4b0', 'MUTED': '#6f7c69',
        'BORDER': '#384230', 'ACCENT': '#d4ddc9', 'ACCENT_HI': '#edf2e6',
        'ACCENT_LO': '#9aa790', 'DANGER': '#d98a7a', 'DANGER_HI': '#e8a89a',
    },
    'ice': {  # cool cyan mainframe — elegant cold tone
        'BG': '#000406', 'CARD': '#03100f', 'FIELD': '#06181a',
        'FIELD_HI': '#0c2528', 'FG': '#5fd0d8', 'MUTED': '#3a8f96',
        'BORDER': '#1c4f54', 'ACCENT': '#67e8e8', 'ACCENT_HI': '#a5f4f4',
        'ACCENT_LO': '#3fb6b6', 'DANGER': '#ff6b6b', 'DANGER_HI': '#ff9595',
    },
}
THEME_ORDER = ['green', 'amber', 'mono', 'ice']
THEME_LABEL = {'green': 'grn', 'amber': 'amb', 'mono': 'mno', 'ice': 'ice'}

SHARP = True  # legacy: sharp corners (no rounding) on every canvas panel

_CONFIG_PATH = Path.home() / '.mailstore_export_gui.json'


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_config(**kw) -> None:
    cfg = _load_config()
    cfg.update(kw)
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# Active palette as module globals (read by widgets at draw/call time).
BG = CARD = FIELD = FIELD_HI = FG = MUTED = BORDER = ACCENT = '#000000'
ACCENT_HI = ACCENT_LO = OK = DANGER = DANGER_HI = LOG_BG = '#000000'


def apply_theme(name: str) -> str:
    """Set the module-level palette globals from THEMES[name]. Returns the
    resolved theme name (falls back to 'green' if unknown)."""
    global BG, CARD, FIELD, FIELD_HI, FG, MUTED, BORDER, ACCENT
    global ACCENT_HI, ACCENT_LO, OK, DANGER, DANGER_HI, LOG_BG
    if name not in THEMES:
        name = 'green'
    p = THEMES[name]
    BG, CARD, FIELD, FIELD_HI = p['BG'], p['CARD'], p['FIELD'], p['FIELD_HI']
    FG, MUTED, BORDER = p['FG'], p['MUTED'], p['BORDER']
    ACCENT, ACCENT_HI, ACCENT_LO = p['ACCENT'], p['ACCENT_HI'], p['ACCENT_LO']
    OK, DANGER, DANGER_HI, LOG_BG = p['ACCENT'], p['DANGER'], p['DANGER_HI'], p['BG']
    return name


# Apply the saved (or default) theme NOW, before the widget classes below use
# these globals as default argument values.
apply_theme(_load_config().get('theme', 'green'))

PROG_RE = re.compile(
    r'\[(?P<df>\d+)/(?P<tf>\d+) folders\]\s+'
    r'(?P<dm>[\d,]+)/(?P<tm>[\d,]+) msg \(\s*(?P<pct>[\d.]+)%\)\s*\|\s*'
    r'skip:(?P<skip>[\d,]+) fail:(?P<fail>[\d,]+)\s*\|\s*'
    r'(?P<mb>[\d,]+)MB \((?P<mbps>[\d,.]+)MB/s, (?P<rate>[\d,]+)msg/s\)'
)


def _to_int(s: str) -> int:
    try:
        return int(s.replace(',', ''))
    except (ValueError, AttributeError):
        return 0


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:
        return '—'
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}h {m:02d}m'
    if m:
        return f'{m}m {s:02d}s'
    return f'{s}s'


def _round_pts(x1, y1, x2, y2, r):
    """Control points for a rounded rectangle drawn as a smoothed polygon."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def _panel(cv, x1, y1, x2, y2, r=0, *, fill, outline=None, width=1, tags=None):
    """Draw a filled panel on a Canvas. Legacy mode (SHARP) => crisp rectangle
    with square corners; otherwise a smoothed rounded polygon. Same call shape
    for both so widgets don't care which is active."""
    outline = fill if outline is None else outline
    if SHARP:
        kw = dict(fill=fill, outline=outline, width=width)
        if tags is not None:
            kw['tags'] = tags
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    kw = dict(smooth=True, fill=fill, outline=outline, width=width)
    if tags is not None:
        kw['tags'] = tags
    return cv.create_polygon(_round_pts(x1, y1, x2, y2, r), **kw)


# ============================================================
# Rounded widget toolkit (Canvas-based)
# ============================================================

class Card(tk.Frame):
    """A rounded card. Add child widgets to `.body` (bg = CARD)."""

    def __init__(self, master, title=None, *, parent_bg=None, bg=None,
                 radius=14, padding=16, title_font=None):
        parent_bg = BG if parent_bg is None else parent_bg
        bg = CARD if bg is None else bg
        super().__init__(master, bg=parent_bg)
        self._bg = bg
        self._r = radius
        self._pad = padding
        self._canvas_h = -1
        self.canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill='both', expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        if title:
            # grid (not pack) so callers can grid their own widgets into body too.
            tk.Label(self.body, text=f'▌ {title.upper()}', bg=bg,
                     fg=ACCENT_HI, font=title_font).grid(
                row=0, column=0, columnspan=99, sticky='w', pady=(0, 8))
        self._win = self.canvas.create_window(padding, padding, window=self.body,
                                              anchor='nw')
        self.canvas.bind('<Configure>', self._on_canvas)
        self.body.bind('<Configure>', self._on_body)

    def _draw(self, w, h):
        if w <= 2 or h <= 2:
            return
        self.canvas.delete('bg')
        # inset by 2px so the border is never clipped at the edges
        _panel(self.canvas, 2, 2, w - 2, h - 2, self._r,
               fill=self._bg, outline=BORDER, width=1, tags='bg')
        self.canvas.tag_lower('bg')

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width - 2 * self._pad)
        self._draw(e.width, e.height)

    def _on_body(self, e):
        h = e.height + 2 * self._pad
        if self._canvas_h != h:
            self._canvas_h = h
            self.canvas.configure(height=h)
        w = self.canvas.winfo_width() or (e.width + 2 * self._pad)
        self._draw(w, h)


class RBtn(tk.Canvas):
    """A rounded, hoverable button."""

    def __init__(self, master, text, command=None, *, parent_bg=None, bg=None,
                 fg=None, hover=None, pressed=None, radius=8, padx=16, pady=9,
                 font=None):
        parent_bg = CARD if parent_bg is None else parent_bg
        bg = FIELD if bg is None else bg
        fg = FG if fg is None else fg
        hover = FIELD_HI if hover is None else hover
        self._font = font or tkfont.nametofont('TkDefaultFont')
        w = self._font.measure(text) + 2 * padx
        h = self._font.metrics('linespace') + 2 * pady
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._text = text
        self._cmd = command
        self._bg = bg
        self._fg = fg
        self._hover = hover
        self._pressed = pressed or hover
        self._r = radius
        self._enabled = True
        self._bw, self._bh = w, h  # NB: never use self._w (tkinter widget path!)
        self._render(bg)
        self.bind('<Enter>', lambda e: self._enabled and self._render(self._hover))
        self.bind('<Leave>', lambda e: self._enabled and self._render(self._bg))
        self.bind('<Button-1>', lambda e: self._enabled and self._render(self._pressed))
        self.bind('<ButtonRelease-1>', self._on_release)

    def _render(self, color):
        self.delete('all')
        fg = self._fg if self._enabled else MUTED
        bg = color if self._enabled else FIELD
        # Subtle 1px outline gives the legacy "key" look on neutral buttons;
        # filled accent/danger buttons keep a flush edge.
        outline = BORDER if bg in (FIELD, FIELD_HI) else bg
        _panel(self, 0, 0, self._bw - 1, self._bh - 1, self._r,
               fill=bg, outline=outline, width=1)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text, fill=fg,
                         font=self._font)

    def _on_release(self, e):
        if not self._enabled:
            return
        self._render(self._hover)
        if 0 <= e.x <= self._bw and 0 <= e.y <= self._bh and self._cmd:
            self._cmd()

    def set_state(self, enabled: bool):
        self._enabled = enabled
        self._render(self._bg)
        self.configure(cursor='' if not enabled else 'hand2')


class REntry(tk.Canvas):
    """A rounded text entry wrapping a borderless tk.Entry."""

    def __init__(self, master, textvariable, *, parent_bg=None, bg=None, fg=None,
                 show=None, radius=8, height=32, font=None, width=160, icon=None):
        parent_bg = CARD if parent_bg is None else parent_bg
        bg = FIELD if bg is None else bg
        fg = FG if fg is None else fg
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._bg = bg
        self._r = radius
        self._icon = icon  # 'search' draws a magnifier on the left (input-group)
        self.entry = tk.Entry(self, textvariable=textvariable, show=show,
                              relief='flat', bd=0, bg=bg, fg=fg, insertbackground=fg,
                              highlightthickness=0, font=font,
                              disabledbackground=bg)
        self._win = self.create_window(0, 0, window=self.entry, anchor='w')
        self.bind('<Configure>', self._on)

    def _draw_lens(self, x, cy):
        r = 4
        self.create_oval(x - r, cy - r, x + r, cy + r, outline=MUTED, width=2,
                         tags='icon')
        self.create_line(x + r * 0.7, cy + r * 0.7, x + r * 1.9, cy + r * 1.9,
                         fill=MUTED, width=2, capstyle='round', tags='icon')

    def _on(self, e):
        self.delete('shape')
        self.delete('icon')
        _panel(self, 1, 1, e.width - 1, e.height - 1, self._r,
               fill=self._bg, outline=BORDER, width=1, tags='shape')
        self.tag_lower('shape')
        left = 12
        if self._icon == 'search':
            self._draw_lens(14, e.height / 2)
            left = 28  # leave room for the magnifier
        self.coords(self._win, left, e.height / 2)
        self.itemconfigure(self._win, width=max(10, e.width - left - 12))


class RProgress(tk.Canvas):
    """A rounded progress bar with determinate + indeterminate (marquee) modes."""

    def __init__(self, master, *, parent_bg=None, trough=None, fill=None,
                 height=14, radius=0):
        parent_bg = BG if parent_bg is None else parent_bg
        trough = FIELD if trough is None else trough
        fill = ACCENT if fill is None else fill
        super().__init__(master, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._trough = trough
        self._fill = fill
        self._r = radius
        self._h = height
        self._value = 0.0
        self._max = 100.0
        self._mode = 'determinate'
        self._pos = 0.0
        self._anim = None
        self.bind('<Configure>', lambda e: self._redraw())

    def set(self, value: float):
        self._value = max(0.0, min(self._max, value))
        self._redraw()

    def configure_mode(self, mode: str):
        self._mode = mode
        self._redraw()

    def start(self, _interval=None):
        self._mode = 'indeterminate'
        if self._anim is None:
            self._animate()

    def stop(self):
        if self._anim is not None:
            try:
                self.after_cancel(self._anim)
            except tk.TclError:
                pass
            self._anim = None
        self._mode = 'determinate'
        self._redraw()

    def _animate(self):
        self._pos = (self._pos + 0.018) % 1.0
        self._redraw()
        self._anim = self.after(30, self._animate)

    def _fill_rect(self, x1, x2):
        if x2 - x1 < 2:
            return
        _panel(self, x1, 0, x2, self._h, self._r,
               fill=self._fill, outline=self._fill)

    def _redraw(self):
        w = self.winfo_width() or 1
        self.delete('all')
        _panel(self, 0, 0, w, self._h, self._r,
               fill=self._trough, outline=BORDER, width=1)
        if self._mode == 'determinate':
            self._fill_rect(0, w * (self._value / self._max))
        else:
            seg = w * 0.28
            x = self._pos * (w + seg) - seg
            self._fill_rect(max(0, x), min(w, x + seg))


class RScroll(tk.Canvas):
    """A thin, rounded, draggable scrollbar. `command` is a widget's yview."""

    def __init__(self, master, command, *, parent_bg=None, width=10,
                 thumb=None, thumb_hi=None, trough=None, radius=0):
        parent_bg = CARD if parent_bg is None else parent_bg
        # height=10: a tk.Canvas defaults to ~7c (~265px), which would inflate any
        # grid row it sits in. Keep it tiny; sticky='ns' stretches it to fit.
        super().__init__(master, width=width, height=10, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._cmd = command
        self._first, self._last = 0.0, 1.0
        self._bw = width
        self._thumb = ACCENT if thumb is None else thumb
        self._thumb_hi = ACCENT_HI if thumb_hi is None else thumb_hi
        self._trough = trough or parent_bg
        self._r = radius
        self._drag = None
        self._hot = False
        self.bind('<Configure>', lambda e: self._redraw())
        self.bind('<Button-1>', self._on_press)
        self.bind('<B1-Motion>', self._on_drag)
        self.bind('<ButtonRelease-1>', self._on_up)
        self.bind('<Enter>', lambda e: self._redraw(True))
        self.bind('<Leave>', lambda e: self._redraw(False))

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._redraw()

    def _geom(self):
        h = self.winfo_height() or 1
        return h, self._first * h, self._last * h

    def _redraw(self, hot=None):
        if hot is not None:
            self._hot = hot
        h, y1, y2 = self._geom()
        w = self._bw
        self.delete('all')
        _panel(self, 0, 0, w, h, self._r,
               fill=self._trough, outline=self._trough)
        if self._last - self._first >= 0.999:
            return  # nothing to scroll
        col = self._thumb_hi if (self._hot or self._drag is not None) else self._thumb
        m = 2
        _panel(self, m, y1 + m, w - m, y2 - m, (w - 2 * m) / 2,
               fill=col, outline=col)

    def _on_press(self, e):
        h, y1, y2 = self._geom()
        if y1 <= e.y <= y2:
            self._drag = e.y - y1
        else:
            self._cmd('moveto', max(0.0, min(1.0, e.y / max(1, h))))
            self._drag = None
        self._redraw()

    def _on_drag(self, e):
        if self._drag is None:
            return
        h, _, _ = self._geom()
        span = self._last - self._first
        frac = (e.y - self._drag) / max(1, h)
        self._cmd('moveto', max(0.0, min(1.0 - span, frac)))

    def _on_up(self, _e):
        self._drag = None
        self._redraw()


class RCheck(tk.Canvas):
    """Terminal-style checkbox: rounded green box on black, black tick when on.
    Bound to a tk.BooleanVar so it works like a ttk.Checkbutton."""

    def __init__(self, master, text, variable, *, parent_bg=None, font=None,
                 disabled=False, command=None, box=14, gap=10):
        parent_bg = CARD if parent_bg is None else parent_bg
        self._font = font or tkfont.nametofont('TkDefaultFont')
        self._box = box
        self._gap = gap
        self._text = text
        self._var = variable
        self._disabled = disabled
        self._cmd = command
        self._hot = False
        w = box + gap + self._font.measure(text) + 8
        h = max(box + 4, self._font.metrics('linespace') + 4)
        super().__init__(master, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._h = h
        if not disabled:
            self.configure(cursor='hand2')
            self.bind('<Button-1>', self._toggle)
            self.bind('<Enter>', lambda e: self._set_hot(True))
            self.bind('<Leave>', lambda e: self._set_hot(False))
        self._trace = variable.trace_add('write', self._draw)
        self.bind('<Destroy>', self._on_destroy)
        self._draw()

    def _on_destroy(self, _e):
        try:
            self._var.trace_remove('write', self._trace)
        except Exception:
            pass

    def _set_hot(self, hot):
        self._hot = hot
        self._draw()

    def _toggle(self, _e):
        self._var.set(not self._var.get())
        if self._cmd:
            self._cmd()

    def _draw(self, *_):
        if not self.winfo_exists():
            return
        self.delete('all')
        box = self._box
        y0 = (self._h - box) // 2
        x0 = 1
        on = bool(self._var.get())
        if self._disabled:
            border, text_col, fill = MUTED, MUTED, BG
        else:
            border = ACCENT_HI if self._hot else ACCENT
            text_col = FG
            fill = ACCENT if on else BG
        _panel(self, x0, y0, x0 + box, y0 + box, 3,
               fill=fill, outline=border, width=1)
        if on:
            tick = [x0 + 0.26 * box, y0 + 0.54 * box,
                    x0 + 0.43 * box, y0 + 0.72 * box,
                    x0 + 0.76 * box, y0 + 0.30 * box]
            self.create_line(*tick, fill=(MUTED if self._disabled else BG),
                             width=2, capstyle='round', joinstyle='round')
        self.create_text(x0 + box + self._gap, self._h // 2, text=self._text,
                         anchor='w', fill=text_col, font=self._font)


class MultiDropdown(tk.Canvas):
    """Rounded, terminal-styled MULTI-select dropdown: a field showing the current
    selection that opens a scrollable checklist popup on click."""

    def __init__(self, master, options, *, parent_bg=None, font=None, mono=None,
                 placeholder='Tutti', width=240, height=32, radius=8,
                 on_change=None, popup_rows=10):
        parent_bg = CARD if parent_bg is None else parent_bg
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self._opts = [str(o) for o in options]
        self._vars = {o: tk.BooleanVar(value=False) for o in self._opts}
        self._placeholder = placeholder
        self._font = font or tkfont.nametofont('TkDefaultFont')
        self._mono = mono or self._font
        self._r = radius
        self._on_change = on_change
        self._popup = None
        self._rows = popup_rows
        self._click_funcid = None
        self.configure(cursor='hand2')
        self.bind('<Configure>', lambda e: self._draw())
        self.bind('<Button-1>', self._toggle_popup)
        self.bind('<Destroy>', lambda e: self._close())
        self._draw()

    # selection API
    def selected(self) -> list:
        return [o for o in self._opts if self._vars[o].get()]

    def clear(self) -> None:
        for v in self._vars.values():
            v.set(False)
        self._draw()
        if self._on_change:
            self._on_change()

    def _summary(self) -> str:
        sel = self.selected()
        if not sel:
            return self._placeholder
        if len(sel) <= 3:
            return ', '.join(sel)
        return f'{len(sel)} selezionati'

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete('all')
        w = self.winfo_width() or int(self['width'])
        h = int(self['height'])
        outline = ACCENT if self._popup else BORDER
        _panel(self, 1, 1, w - 1, h - 1, self._r,
               fill=FIELD, outline=outline, width=1)
        sel = self.selected()
        self.create_text(12, h / 2, text=self._summary(), anchor='w',
                         fill=(FG if sel else MUTED), font=self._mono)
        self.create_text(w - 16, h / 2, text=('▲' if self._popup else '▾'),
                         anchor='w', fill=ACCENT, font=self._font)

    def _on_pick(self) -> None:
        self._draw()
        if self._on_change:
            self._on_change()

    def _toggle_popup(self, _e=None) -> None:
        if self._popup:
            self._close()
        else:
            self._open()

    def _open(self) -> None:
        # IN-WINDOW overlay (a placed Frame), NOT a separate Toplevel: on macOS a
        # topmost child window steals the first click for activation, so clicking
        # another dropdown wouldn't register. An overlay in the same window works.
        self.update_idletasks()
        root = self.winfo_toplevel()
        rh = self._mono.metrics('linespace') + 12  # ~ one RCheck row
        w = max(self.winfo_width(), 160)
        body_h = 200  # fixed popup height; the list scrolls inside
        scrolls = rh * len(self._opts) > body_h
        sb_w = 8 if scrolls else 0
        x = self.winfo_rootx() - root.winfo_rootx()
        y = self.winfo_rooty() - root.winfo_rooty() + self.winfo_height()

        pop = tk.Frame(root, bg=BORDER)  # 1px border via padding
        self._popup = pop
        inner = tk.Frame(pop, bg=CARD)
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        canvas = tk.Canvas(inner, bg=CARD, highlightthickness=0, bd=0,
                           width=w - 2 - sb_w, height=body_h, yscrollincrement=1)
        self._list_canvas = canvas  # exposed so the wheel can scroll the list
        frame = tk.Frame(canvas, bg=CARD)
        frame.bind('<Configure>',
                   lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        win = canvas.create_window((0, 0), window=frame, anchor='nw')
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))
        if scrolls:
            sb = RScroll(inner, canvas.yview, parent_bg=CARD, width=sb_w)
            canvas.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        for o in self._opts:
            RCheck(frame, o, self._vars[o], parent_bg=CARD, font=self._mono,
                   command=self._on_pick).pack(anchor='w', padx=8, pady=1)

        pop.place(x=x, y=y, width=w, height=body_h + 2)
        pop.tkraise()
        self._draw()
        # close when clicking outside the popup / field (same-window event)
        self._click_funcid = root.bind('<Button-1>', self._maybe_close, add='+')

    def _maybe_close(self, e) -> None:
        if not self._popup:
            return
        wp = str(e.widget)
        if wp == str(self) or wp.startswith(str(self._popup)):
            return  # click on the field or inside the popup -> keep open
        self._close()

    def _close(self) -> None:
        if self._click_funcid is not None:
            try:
                self.winfo_toplevel().unbind('<Button-1>', self._click_funcid)
            except Exception:
                pass
            self._click_funcid = None
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None
            self._draw()


def _import_cli():
    """Lazily import the CLI module (so the GUI still opens if it fails)."""
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import mailstore_webapi_export as cli  # noqa: WPS433 (runtime import by design)
    return cli


# ============================================================
# Main application
# ============================================================

class MailStoreGUI:

    def __init__(self, root: tk.Tk):
        self.root = root
        _cfg = _load_config()
        self.lang = _cfg.get('lang') if _cfg.get('lang') in T else 'it'
        self.theme = apply_theme(_cfg.get('theme', 'green'))
        root.title('MailStore — Export Web API')
        root.minsize(560, 460)  # narrow is fine: the whole page scrolls
        root.geometry('850x710')
        root.configure(bg=BG)

        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self._transient_line = False
        self._stop_requested = False
        self._tmp_env_path: str | None = None
        self._pbar_determinate = False

        self.var_host = tk.StringVar(value=DEFAULT_HOST)
        self.var_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_workers = tk.StringVar(value=str(DEFAULT_WORKERS))
        self.var_dedup = tk.BooleanVar(value=False)
        self.var_split_year = tk.BooleanVar(value=False)
        self.var_count_first = tk.BooleanVar(value=True)
        self.var_arc_search = tk.StringVar()
        self._archive_rows: list[tuple[str, bool]] = []
        # Year/month multi-select via HTML-style <select multiple> (Listbox).
        # Empty selection = all years / all months.
        self._years = list(range(YEAR_MAX, YEAR_MIN - 1, -1))  # descending
        self.var_include = tk.StringVar()
        self.var_exclude = tk.StringVar()
        self.var_date_from = tk.StringVar()
        self.var_date_to = tk.StringVar()
        self.var_status = tk.StringVar(value=self.t('ready'))
        self.var_prog_text = tk.StringVar(value=self.t('waiting'))

        self.archive_vars: dict[str, tuple[tk.BooleanVar, bool]] = {}

        self._fonts()
        self._apply_style()
        self._build_ui()
        self._load_env_into_form(announce=False)
        self.root.after(80, self._drain_events)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    # ----- i18n ------------------------------------------------------------

    def t(self, key: str) -> str:
        return T.get(self.lang, T['it']).get(key, T['it'].get(key, key))

    def _months(self) -> list:
        return T.get(self.lang, T['it'])['months_abbr']

    def _set_lang(self, lang: str) -> None:
        if lang == self.lang or lang not in T:
            return
        self.lang = lang
        self.var_status.set(self.t('ready'))
        self.var_prog_text.set(self.t('waiting'))
        _save_config(lang=lang)
        self._rebuild_ui()

    def _set_theme(self, name: str) -> None:
        if name == self.theme or name not in THEMES:
            return
        self.theme = apply_theme(name)      # reassign the palette globals
        _save_config(theme=name)
        self.root.configure(bg=BG)
        self._apply_style()                 # refresh ttk label/scrollbar colors
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """Tear down and rebuild the whole page preserving form state. Shared by
        language and theme switches (both recreate every widget so the new
        strings / palette take effect)."""
        # Capture state living in widgets we are about to destroy. Months are
        # captured by POSITION (not localized name) so it survives a lang change.
        years = self._selected_years()
        mnames = list(self.month_dd._vars)
        months = [i for i, n in enumerate(mnames)
                  if self.month_dd._vars[n].get()]
        log_text = self.log.get('1.0', 'end-1c')
        for w in self.root.winfo_children():
            w.destroy()
        self._build_ui()
        # restore: archives (BooleanVars survive the widget destroy), year/month
        # selection, log content
        if self._archive_rows:
            self._render_archives()
        for y in years:
            if str(y) in self.year_dd._vars:
                self.year_dd._vars[str(y)].set(True)
        self.year_dd._draw()
        self._update_months()
        new_names = list(self.month_dd._vars)
        for i in months:
            if 0 <= i < len(new_names):
                self.month_dd._vars[new_names[i]].set(True)
        self.month_dd._draw()
        if log_text:
            self._log_enable()
            self.log.insert('end', log_text + '\n')
            self._log_disable()

    # ----- fonts / ttk styling (labels, checkbuttons, scrollbars) ----------

    def _fonts(self) -> None:
        base = tkfont.nametofont('TkDefaultFont')
        fam = 'Menlo'  # monospace EVERYWHERE for the terminal look
        try:
            base.configure(family=fam, size=8)
        except tk.TclError:
            fam = base.cget('family')
        self.font_base = base
        self.font_h1 = tkfont.Font(family=fam, size=12, weight='bold')
        self.font_h2 = tkfont.Font(family=fam, size=9, weight='bold')
        self.font_btn = tkfont.Font(family=fam, size=8)
        self.font_btn_b = tkfont.Font(family=fam, size=8, weight='bold')
        self.font_mono = tkfont.Font(family=fam, size=8)       # terminal feel
        self.font_mono_s = tkfont.Font(family=fam, size=8)
        self.font_xs = tkfont.Font(family=fam, size=7)

    def _apply_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('.', background=CARD, foreground=FG, focuscolor=CARD)
        style.configure('Win.TLabel', background=BG, foreground=FG)
        style.configure('WinMuted.TLabel', background=BG, foreground=MUTED)
        style.configure('H1.TLabel', background=BG, foreground=FG, font=self.font_h1)
        style.configure('Card.TLabel', background=CARD, foreground=FG)
        style.configure('CardMuted.TLabel', background=CARD, foreground=MUTED)
        style.configure('Prog.TLabel', background=BG, foreground=MUTED)
        style.configure('TCheckbutton', background=CARD, foreground=FG,
                        focuscolor=CARD)
        style.map('TCheckbutton', background=[('active', CARD)],
                  indicatorcolor=[('selected', ACCENT), ('!selected', FIELD)],
                  foreground=[('disabled', MUTED)])
        for orient in ('Vertical', 'Horizontal'):
            style.configure(f'{orient}.TScrollbar', background=FIELD,
                            troughcolor=CARD, bordercolor=CARD, arrowcolor=MUTED,
                            relief='flat')
            style.map(f'{orient}.TScrollbar', background=[('active', FIELD_HI)])

    # ----- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        pad = dict(padx=8, pady=6)

        # Whole UI is vertically scrollable: a canvas hosting one inner frame.
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill='both', expand=True)
        self._scroll_canvas = tk.Canvas(container, bg=BG, highlightthickness=0,
                                        bd=0, yscrollincrement=1)  # 1 unit = 1px
        vscroll = RScroll(container, self._scroll_canvas.yview, parent_bg=BG, width=12)
        self._scroll_canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side='right', fill='y')
        self._scroll_canvas.pack(side='left', fill='both', expand=True)
        page = tk.Frame(self._scroll_canvas, bg=BG)
        self._page_win = self._scroll_canvas.create_window((0, 0), window=page,
                                                           anchor='nw')
        page.bind('<Configure>', lambda e: self._scroll_canvas.configure(
            scrollregion=self._scroll_canvas.bbox('all')))
        self._scroll_canvas.bind('<Configure>', lambda e: self._scroll_canvas.
                                 itemconfigure(self._page_win, width=e.width))
        self.root.bind_all('<MouseWheel>', self._on_wheel)
        self.root.bind_all('<Button-4>', self._on_wheel)  # X11 wheel up
        self.root.bind_all('<Button-5>', self._on_wheel)  # X11 wheel down
        # macOS + Tk 9: the trackpad emits <TouchpadScroll>, NOT <MouseWheel>.
        try:
            self.root.bind_all('<TouchpadScroll>', self._on_touchpad)
        except tk.TclError:
            pass

        outer = tk.Frame(page, bg=BG)
        outer.pack(fill='both', expand=True, padx=16, pady=16)

        # Header — terminal prompt with a blinking block cursor + language picker
        header = tk.Frame(outer, bg=BG)
        header.pack(fill='x', pady=(0, 12))
        ttk.Label(header, text='mailstore@export:~$ ./mailstore_export --webapi',
                  style='H1.TLabel').pack(side='left')
        self._cursor = ttk.Label(header, text='█', style='H1.TLabel')
        self._cursor.pack(side='left')
        self._blink()
        # language picker on the right: one small button per language
        langbar = tk.Frame(header, bg=BG)
        langbar.pack(side='right')
        for code in reversed(LANG_ORDER):
            active = code == self.lang
            RBtn(langbar, f'[ {LANG_LABEL[code]} ]',
                 (lambda c=code: self._set_lang(c)), parent_bg=BG,
                 bg=(ACCENT if active else FIELD),
                 fg=(BG if active else FG),
                 hover=(ACCENT_HI if active else FIELD_HI),
                 font=self.font_btn).pack(side='right', padx=2)
        # theme picker, just left of the language picker
        themebar = tk.Frame(header, bg=BG)
        themebar.pack(side='right', padx=(0, 14))
        for code in reversed(THEME_ORDER):
            active = code == self.theme
            RBtn(themebar, f'[ {THEME_LABEL[code]} ]',
                 (lambda c=code: self._set_theme(c)), parent_bg=BG,
                 bg=(ACCENT if active else FIELD),
                 fg=(BG if active else FG),
                 hover=(ACCENT_HI if active else FIELD_HI),
                 font=self.font_btn).pack(side='right', padx=2)

        def entry(parent, var, r, c, show=None, width=160, span=1, sticky='ew'):
            e = REntry(parent, var, show=show, width=width, font=self.font_base)
            e.grid(row=r, column=c, columnspan=span, sticky=sticky, **pad)
            return e

        def clabel(parent, text, r, c):
            ttk.Label(parent, text=text, style='Card.TLabel').grid(
                row=r, column=c, sticky='w', **pad)

        # Connection card
        conn = Card(outer, self.t('card_conn'), title_font=self.font_h2)
        conn.pack(fill='x', pady=(0, 12))
        cb = conn.body
        for c in (1, 3):
            cb.columnconfigure(c, weight=1)
        clabel(cb, self.t('host'), 1, 0);   entry(cb, self.var_host, 1, 1)
        clabel(cb, self.t('port'), 1, 2);  entry(cb, self.var_port, 1, 3, width=90)
        clabel(cb, self.t('user'), 2, 0); entry(cb, self.var_user, 2, 1)
        clabel(cb, self.t('pass'), 2, 2); entry(cb, self.var_pass, 2, 3, show='•')
        clabel(cb, self.t('token'), 3, 0); entry(cb, self.var_token, 3, 1, show='•')
        cbtns = tk.Frame(cb, bg=CARD)
        cbtns.grid(row=3, column=2, columnspan=2, sticky='e', **pad)
        RBtn(cbtns, self.t('load_env'), lambda: self._load_env_into_form(True),
             parent_bg=CARD, font=self.font_btn).pack(side='left', padx=4)
        self.btn_test = RBtn(cbtns, self.t('test'), self.on_test_connection,
                             parent_bg=CARD, bg=FIELD, font=self.font_btn)
        self.btn_test.pack(side='left', padx=4)

        # Output / options card
        opt = Card(outer, self.t('card_opt'), title_font=self.font_h2)
        opt.pack(fill='x', pady=(0, 12))
        ob = opt.body
        ob.columnconfigure(1, weight=1)
        clabel(ob, self.t('outdir'), 1, 0)
        entry(ob, self.var_output, 1, 1)
        RBtn(ob, self.t('browse'), self.on_browse_output, parent_bg=CARD,
             font=self.font_btn).grid(row=1, column=2, **pad)
        clabel(ob, self.t('workers'), 2, 0)
        entry(ob, self.var_workers, 2, 1, width=90, sticky='w')
        checks = tk.Frame(ob, bg=CARD)
        checks.grid(row=3, column=0, columnspan=3, sticky='w', **pad)
        RCheck(checks, self.t('dedup'), self.var_dedup, parent_bg=CARD,
               font=self.font_mono).pack(side='left', padx=(0, 20))
        RCheck(checks, self.t('split'), self.var_split_year,
               parent_bg=CARD, font=self.font_mono).pack(side='left', padx=(0, 20))
        RCheck(checks, self.t('countfirst'), self.var_count_first,
               parent_bg=CARD, font=self.font_mono).pack(side='left')
        clabel(ob, self.t('inc'), 4, 0)
        entry(ob, self.var_include, 4, 1, span=2)
        clabel(ob, self.t('exc'), 5, 0)
        entry(ob, self.var_exclude, 5, 1, span=2)

        # Period card: dropdown multi-select for years (+ months when exactly one
        # year is selected).
        per = Card(outer, self.t('card_period'), title_font=self.font_h2)
        per.pack(fill='x', pady=(0, 12))
        pb = per.body
        # Years and months side by side: the year popup drops DOWN, so keeping
        # months to the RIGHT means the open year list never covers them.
        ttk.Label(pb, text=self.t('years'), style='CardMuted.TLabel').grid(
            row=1, column=0, sticky='w', **pad)
        self.year_dd = MultiDropdown(
            pb, self._years, parent_bg=CARD, font=self.font_base,
            mono=self.font_mono_s, placeholder=self.t('all_years'), width=210,
            on_change=self._update_months, popup_rows=10)
        self.year_dd.grid(row=1, column=1, sticky='w', **pad)

        self.month_label = ttk.Label(pb, text=self.t('months'),
                                     style='CardMuted.TLabel')
        self.month_label.grid(row=1, column=2, sticky='w', padx=(24, 8), pady=6)
        self.month_dd = MultiDropdown(
            pb, self._months(), parent_bg=CARD, font=self.font_base,
            mono=self.font_mono_s, placeholder=self.t('all_months'), width=210,
            popup_rows=12)
        self.month_dd.grid(row=1, column=3, sticky='w', **pad)
        self.month_label.grid_remove()
        self.month_dd.grid_remove()  # shown only when exactly one year is picked

        # Filtro per RANGE di data esatto (YYYY-MM-DD). Più stringente di
        # anno/mese: scarica DIRETTAMENTE la finestra. Ha precedenza su anno/mese.
        ttk.Label(pb, text=self.t('date_from'), style='CardMuted.TLabel').grid(
            row=2, column=0, sticky='w', **pad)
        REntry(pb, self.var_date_from, width=130, font=self.font_base).grid(
            row=2, column=1, sticky='w', **pad)
        ttk.Label(pb, text=self.t('date_to'), style='CardMuted.TLabel').grid(
            row=2, column=2, sticky='w', padx=(24, 8), pady=6)
        REntry(pb, self.var_date_to, width=130, font=self.font_base).grid(
            row=2, column=3, sticky='w', **pad)
        ttk.Label(pb, text=self.t('date_hint'),
                  style='CardMuted.TLabel').grid(
            row=3, column=0, columnspan=4, sticky='w', padx=8, pady=(0, 6))

        # Archives card
        arc = Card(outer, self.t('card_arc'), title_font=self.font_h2)
        arc.pack(fill='x', pady=(0, 12))
        ab = arc.body
        ab.columnconfigure(0, weight=1)
        arctop = tk.Frame(ab, bg=CARD)
        arctop.grid(row=1, column=0, sticky='ew')
        RBtn(arctop, self.t('sel_all'), lambda: self._set_all_archives(True),
             parent_bg=CARD, font=self.font_btn).pack(side='left')
        RBtn(arctop, self.t('desel_all'), lambda: self._set_all_archives(False),
             parent_bg=CARD, font=self.font_btn).pack(side='left', padx=6)
        # Order: buttons, hint, then a search field with a magnifier icon inside.
        ttk.Label(arctop, text=self.t('arc_hint'),
                  style='CardMuted.TLabel', font=self.font_xs).pack(
            side='left', padx=(12, 8))
        REntry(arctop, self.var_arc_search, font=self.font_base, width=200,
               icon='search').pack(side='left')
        self.var_arc_search.trace_add('write', lambda *a: self._render_archives())
        # Archives in a fixed, tall scroll box: shows as many rows as fit and
        # the WHEEL scrolls the list itself (see _do_scroll).
        wrap = tk.Frame(ab, bg=CARD)
        wrap.grid(row=2, column=0, sticky='ew', pady=(8, 0))
        wrap.columnconfigure(0, weight=1)
        self._arc_canvas = tk.Canvas(wrap, height=106, highlightthickness=0,
                                     bg=CARD, bd=0, yscrollincrement=1)
        sb = RScroll(wrap, self._arc_canvas.yview, parent_bg=CARD)
        self.arc_frame = tk.Frame(self._arc_canvas, bg=CARD)
        self.arc_frame.bind('<Configure>', lambda e: self._arc_canvas.configure(
            scrollregion=self._arc_canvas.bbox('all')))
        self._arc_win = self._arc_canvas.create_window((0, 0), window=self.arc_frame,
                                                       anchor='nw')
        self._arc_canvas.bind('<Configure>', lambda e: self._arc_canvas.itemconfigure(
            self._arc_win, width=e.width))
        self._arc_canvas.configure(yscrollcommand=sb.set)
        self._arc_canvas.grid(row=0, column=0, sticky='ew')
        sb.grid(row=0, column=1, sticky='ns', padx=(6, 0))

        # Actions row
        act = tk.Frame(outer, bg=BG)
        act.pack(fill='x', pady=(0, 12))
        self.action_buttons: list[RBtn] = []
        self.btn_export = RBtn(act, self.t('export'), self.on_export, parent_bg=BG,
                               bg=ACCENT, fg='#001a08', hover=ACCENT_HI,
                               pressed=ACCENT_LO, font=self.font_btn_b, padx=20)
        self.btn_export.pack(side='left', padx=(0, 8))
        self.action_buttons.append(self.btn_export)
        for text, cmd in ((self.t('analyze'), self.on_analyze),
                          (self.t('benchmark'), self.on_benchmark),
                          (self.t('scantmp'), self.on_scan_tmp),
                          (self.t('failed'), self.on_list_failed),
                          (self.t('doctor'), self.on_doctor),
                          (self.t('reconcile'), self.on_reconcile),
                          (self.t('retry'), self.on_retry_failed)):
            b = RBtn(act, text, cmd, parent_bg=BG, font=self.font_btn)
            b.pack(side='left', padx=4)
            self.action_buttons.append(b)
        self.btn_stop = RBtn(act, self.t('stop'), self.on_stop, parent_bg=BG,
                             bg=DANGER, fg='#1a0000', hover=DANGER_HI,
                             font=self.font_btn_b)
        self.btn_stop.pack(side='right')
        self.btn_stop.set_state(False)
        # Reset state.db: azione a sé (a destra, separata dai diagnostici).
        # Protetta da conferma forte + backup automatico nell'handler.
        self.btn_reset = RBtn(act, self.t('reset_state'), self.on_reset_state,
                              parent_bg=BG, font=self.font_btn)
        self.btn_reset.pack(side='right', padx=(0, 12))
        self.action_buttons.append(self.btn_reset)

        # Progress
        prog = tk.Frame(outer, bg=BG)
        prog.pack(fill='x', pady=(0, 12))
        self.pbar = RProgress(prog, parent_bg=BG)
        self.pbar.pack(fill='x')
        ttk.Label(prog, textvariable=self.var_prog_text, style='Prog.TLabel').pack(
            anchor='w', pady=(6, 0))

        # Log card (fixed height, does NOT expand to fill leftover space)
        logc = Card(outer, self.t('card_out'), title_font=self.font_h2)
        self._logc = logc  # for wheel routing (disabled Text doesn't get events)
        logc.pack(fill='x')
        lb = logc.body
        lb.columnconfigure(0, weight=1)
        log_lines = max(3, round(145 / self.font_mono.metrics('linespace')))  # ~145px
        # Monochrome themed log: phosphor text on black for the terminal look.
        self.log = tk.Text(lb, height=log_lines, wrap='none', background=LOG_BG,
                           foreground=FG, insertbackground=FG,
                           selectbackground=ACCENT, selectforeground=BG,
                           relief='flat', highlightthickness=0, bd=0,
                           font=self.font_mono)
        logsb = RScroll(lb, self.log.yview, parent_bg=CARD)
        self.log.configure(yscrollcommand=logsb.set, state='disabled')
        self.log.grid(row=1, column=0, sticky='nsew')
        logsb.grid(row=1, column=1, sticky='ns', padx=(6, 0))

        ttk.Label(outer, textvariable=self.var_status, style='WinMuted.TLabel',
                  anchor='w').pack(fill='x', pady=(8, 0))

    # ----- form helpers ----------------------------------------------------

    def _load_env_into_form(self, announce: bool) -> None:
        try:
            cli = _import_cli()
            env = cli.load_env_file()
        except Exception as exc:
            if announce:
                messagebox.showwarning('Carica .env',
                                       f'Impossibile leggere .env:\n{exc}')
            return
        if not env:
            if announce:
                messagebox.showinfo('Carica .env', 'Nessun file .env trovato.')
            return
        mapping = {
            'MAILSTORE_HOST': self.var_host, 'MAILSTORE_PORT': self.var_port,
            'MAILSTORE_USER': self.var_user, 'MAILSTORE_PASSWORD': self.var_pass,
            'MAILSTORE_TOKEN': self.var_token, 'MAILSTORE_OUTPUT': self.var_output,
            'MAILSTORE_WORKERS': self.var_workers,
        }
        for key, var in mapping.items():
            if env.get(key):
                var.set(env[key])
        loaded = ', '.join(k for k in env if not k.endswith('PASSWORD'))
        self.var_status.set(f'.env caricato: {loaded}')
        if announce:
            messagebox.showinfo('Carica .env', f'Caricate {len(env)} variabili.')

    def on_browse_output(self) -> None:
        initial = self.var_output.get() or str(Path.home())
        chosen = filedialog.askdirectory(initialdir=initial,
                                         title='Scegli cartella di output')
        if chosen:
            self.var_output.set(chosen)

    def _do_scroll(self, widget, px: int) -> None:
        """Scroll by `px` pixels (positive = toward the end). Over a listbox/log
        scrolls that widget while it still can, then bubbles to the whole page."""
        if px == 0:
            return
        # open dropdown popup: scroll its list if hovering it, else close it
        # (a placed popup wouldn't follow a page scroll, so it'd detach).
        for dd in (getattr(self, 'year_dd', None), getattr(self, 'month_dd', None)):
            if dd is None or dd._popup is None:
                continue
            if str(widget).startswith(str(dd._popup)):
                lc = getattr(dd, '_list_canvas', None)
                if lc is not None:
                    lc.yview_scroll(px, 'units')
                return
            dd._close()
        # over the archive box (the canvas or any of its RCheck rows): scroll the
        # box itself while it can, then bubble to the page at the edges.
        # Scrolling INSIDE a box scrolls only that box, never the whole page
        # (yview_scroll clamps at the edges).
        arc = getattr(self, '_arc_canvas', None)
        if arc is not None and (widget is arc
                                or str(widget).startswith(str(self.arc_frame))):
            arc.yview_scroll(px, 'units')  # 1 unit = 1px
            return
        # over the Output card: the log Text is disabled (doesn't receive wheel
        # events on macOS), so route by card ancestry and scroll the log.
        logc = getattr(self, '_logc', None)
        if logc is not None and str(widget).startswith(str(logc)):
            self.log.yview_scroll(1 if px > 0 else -1, 'units')
            return
        cls = ''
        try:
            cls = widget.winfo_class()
        except Exception:
            pass
        if cls in ('Listbox', 'Text'):
            try:
                first, last = widget.yview()
            except Exception:
                first, last = 0.0, 1.0
            if (px > 0 and last < 1.0) or (px < 0 and first > 0.0):
                widget.yview_scroll(1 if px > 0 else -1, 'units')
                return  # inner widget consumed it
        self._scroll_canvas.yview_scroll(px, 'units')  # 1 unit = 1px

    def _on_wheel(self, e) -> None:
        """Classic mouse wheel (and X11 Button-4/5)."""
        num = getattr(e, 'num', None)
        if num == 4:
            px = -45
        elif num == 5:
            px = 45
        else:
            px = -45 if (e.delta or 0) > 0 else 45
        self._do_scroll(e.widget, px)

    def _on_touchpad(self, e) -> None:
        """macOS/Tk9 trackpad: %D packs (deltaX<<16 | deltaY); low 16 bits = Y."""
        dy = (e.delta or 0) & 0xFFFF
        if dy >= 0x8000:
            dy -= 0x10000
        if dy:
            self._do_scroll(e.widget, -dy * 2)

    def _blink(self) -> None:
        try:
            cur = self._cursor.cget('text')
            self._cursor.configure(text=' ' if cur == '█' else '█')
            self.root.after(530, self._blink)
        except tk.TclError:
            pass  # window closed

    def _selected_years(self) -> list[int]:
        return sorted(int(y) for y in self.year_dd.selected())

    def _update_months(self) -> None:
        """Months are only meaningful for a single year: show them only then."""
        if len(self.year_dd.selected()) == 1:
            self.month_label.grid()
            self.month_dd.grid()
        else:
            self.month_dd.clear()
            self.month_label.grid_remove()
            self.month_dd.grid_remove()

    def _set_all_archives(self, value: bool) -> None:
        # applies to the currently filtered (visible) archives only
        q = self.var_arc_search.get().strip().lower()
        for name, (var, contains) in self.archive_vars.items():
            if not contains:
                continue
            if q and q not in name.lower():
                continue
            var.set(value)

    def _selected_archives(self) -> list[str]:
        return [name for name, (var, _) in self.archive_vars.items() if var.get()]

    # ----- connection test / archive loading -------------------------------

    def on_test_connection(self) -> None:
        if self.proc is not None:
            messagebox.showwarning('Occupato',
                                   'Attendi il termine dell\'operazione in corso.')
            return
        host = self.var_host.get().strip() or DEFAULT_HOST
        try:
            port = int(self.var_port.get().strip() or DEFAULT_PORT)
        except ValueError:
            messagebox.showerror('Porta', 'La porta deve essere un numero.')
            return
        user = self.var_user.get().strip() or None
        password = self.var_pass.get() or None
        token = self.var_token.get().strip() or None
        if not token and not user:
            messagebox.showerror('Credenziali',
                                 'Inserisci utente+password oppure un token.')
            return
        self.btn_test.set_state(False)
        self.var_status.set(f'Connessione a {host}:{port}…')
        self._log_line(f'[test] login su https://{host}:{port} …')
        threading.Thread(target=self._test_worker,
                         args=(host, port, user, password, token),
                         daemon=True).start()

    def _test_worker(self, host, port, user, password, token) -> None:
        try:
            cli = _import_cli()
            client = cli.WebClient(f'https://{host}:{port}', token=token,
                                   username=user, password=password,
                                   verify_tls=False)
            archives = client.get_archives()
            rows = [(a.get('name', '?'), bool(a.get('containsFolders')))
                    for a in archives]
            rows.sort(key=lambda r: r[0].lower())
            self.events.put(('archives', rows))
        except Exception as exc:
            self.events.put(('test_error', f'{type(exc).__name__}: {exc}'))

    def _populate_archives(self, rows: list[tuple[str, bool]]) -> None:
        self._archive_rows = rows
        # build/refresh vars, preserving any existing selection by name
        new_vars: dict[str, tuple[tk.BooleanVar, bool]] = {}
        for name, contains in rows:
            old = self.archive_vars.get(name)
            var = old[0] if old else tk.BooleanVar(value=False)
            new_vars[name] = (var, contains)
        self.archive_vars = new_vars
        self._render_archives()
        non_empty = sum(1 for _, c in rows if c)
        self.var_status.set(
            f'Connessione OK — {len(rows)} archivi ({non_empty} non vuoti).')
        self._log_line(f'[test] OK: {len(rows)} archivi trovati.')

    def _render_archives(self) -> None:
        """(Re)draw the archive checkboxes filtered by the search bar."""
        for child in self.arc_frame.winfo_children():
            child.destroy()
        q = self.var_arc_search.get().strip().lower()
        shown = 0
        for name, contains in self._archive_rows:
            if q and q not in name.lower():
                continue
            var = self.archive_vars[name][0]
            label = name if contains else f'{name}  (vuoto)'
            RCheck(self.arc_frame, label, var, parent_bg=CARD,
                   font=self.font_mono_s, disabled=not contains).pack(
                anchor='w', pady=2)
            shown += 1
        if q and shown == 0 and self._archive_rows:
            tk.Label(self.arc_frame, text=self.t('no_match'),
                     bg=CARD, fg=MUTED, font=self.font_mono_s).pack(
                anchor='w', pady=4)
        if hasattr(self, '_arc_canvas'):
            self._arc_canvas.yview_moveto(0.0)

    # ----- building subprocess invocation ----------------------------------

    def _write_tmp_env(self, password: str | None, token: str | None) -> str | None:
        if not password and not token:
            return None
        fd, path = tempfile.mkstemp(prefix='mailstore_env_', suffix='.env')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                if password:
                    f.write(f'MAILSTORE_PASSWORD={password}\n')
                if token:
                    f.write(f'MAILSTORE_TOKEN={token}\n')
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _common_argv(self, require_archives: bool, require_output: bool):
        host = self.var_host.get().strip() or DEFAULT_HOST
        try:
            port = int(self.var_port.get().strip() or DEFAULT_PORT)
        except ValueError:
            return None, 'La porta deve essere un numero.'
        user = self.var_user.get().strip()
        token = self.var_token.get().strip()
        if not user and not token:
            return None, 'Inserisci utente+password oppure un token.'
        try:
            workers = int(self.var_workers.get().strip() or DEFAULT_WORKERS)
        except ValueError:
            return None, 'I worker devono essere un numero.'
        output = self.var_output.get().strip()
        if require_output and not output:
            return None, 'Scegli una cartella di output.'
        argv = [sys.executable, str(CLI_SCRIPT), '--no-interactive',
                '--host', host, '--port', str(port), '--workers', str(workers)]
        if user:
            argv += ['--user', user]
        if output:
            argv += ['--output', output]
        if self.var_dedup.get():
            argv += ['--dedup-message-id']
        if self.var_count_first.get():
            argv += ['--count-first']
        if self.var_split_year.get():
            argv += ['--split-by-year']
        sel_years = self._selected_years()
        for y in sel_years:
            argv += ['--year', str(y)]
        if len(sel_years) == 1:  # months only apply to a single selected year
            months = self._months()
            for name in self.month_dd.selected():
                argv += ['--month', str(months.index(name) + 1)]
        # Range di data esatto (YYYY-MM-DD): ha precedenza su anno/mese lato CLI.
        df = self.var_date_from.get().strip()
        dt_ = self.var_date_to.get().strip()
        for lbl, val in ((self.t('date_from'), df), (self.t('date_to'), dt_)):
            if val and not re.match(r'^\d{4}-\d{2}-\d{2}$', val):
                return None, f'{lbl}: usa il formato YYYY-MM-DD (es. 2025-01-15).'
        if df:
            argv += ['--date-from', df]
        if dt_:
            argv += ['--date-to', dt_]
        for patt in self.var_include.get().strip().splitlines():
            if patt.strip():
                argv += ['--include-folder', patt.strip()]
        for patt in self.var_exclude.get().strip().splitlines():
            if patt.strip():
                argv += ['--exclude-folder', patt.strip()]
        if require_archives:
            archives = self._selected_archives()
            if not archives:
                return None, 'Seleziona almeno un archivio.'
            for a in archives:
                argv += ['--archive', a]
        return argv, None

    # ----- action handlers --------------------------------------------------

    def on_export(self) -> None:
        argv, err = self._common_argv(require_archives=True, require_output=True)
        if err:
            messagebox.showerror('Export', err)
            return
        n = len(self._selected_archives())
        if not messagebox.askyesno(
                'Conferma export',
                f'Avviare l\'export di {n} archivi in:\n{self.var_output.get()}'
                '\n\nIl resume è automatico (state.db).'):
            return
        self._launch(argv, label='export')

    def on_analyze(self) -> None:
        argv, err = self._common_argv(require_archives=True, require_output=True)
        if err:
            messagebox.showerror('Analizza', err)
            return
        argv += ['--analyze']
        self._launch(argv, label='analyze')

    def on_benchmark(self) -> None:
        argv, err = self._common_argv(require_archives=False, require_output=False)
        if err:
            messagebox.showerror('Benchmark', err)
            return
        argv += ['--benchmark']
        for a in self._selected_archives():
            argv += ['--archive', a]
        self._launch(argv, label='benchmark')

    def on_scan_tmp(self) -> None:
        argv, err = self._common_argv(require_archives=False, require_output=True)
        if err:
            messagebox.showerror('Scan .tmp', err)
            return
        argv += ['--scan-tmp']
        self._launch(argv, label='scan-tmp')

    def on_list_failed(self) -> None:
        argv, err = self._common_argv(require_archives=False, require_output=True)
        if err:
            messagebox.showerror('Lista falliti', err)
            return
        argv += ['--list-failed']
        self._launch(argv, label='list-failed')

    def on_doctor(self) -> None:
        # Health-check struttura: serve il server (credenziali). Se sono
        # selezionati archivi, il doctor si limita a quelli; altrimenti tutti.
        argv, err = self._common_argv(require_archives=False, require_output=True)
        if err:
            messagebox.showerror('Doctor', err)
            return
        argv += ['--doctor']
        for a in self._selected_archives():
            argv += ['--archive', a]
        self._launch(argv, label='doctor')

    def on_reconcile(self) -> None:
        # Riconciliazione SOLO da state.db: niente server, basta l'output.
        out = self.var_output.get().strip()
        if not out:
            messagebox.showerror('Riconcilia', 'Scegli una cartella di output.')
            return
        argv = [sys.executable, str(CLI_SCRIPT), '--no-interactive',
                '--reconcile', '--output', out]
        self._launch(argv, label='reconcile')

    def on_retry_failed(self) -> None:
        argv, err = self._common_argv(require_archives=False, require_output=True)
        if err:
            messagebox.showerror('Ritenta falliti', err)
            return
        choice = messagebox.askyesnocancel(
            'Ritenta falliti',
            'Ri-scarica i messaggi in failed_messages (rete, IncompleteRead, '
            '500 temporanei sono recuperabili).\n\n'
            'Saltare i fallimenti PERMANENTI (messaggi corrotti lato MailStore: '
            'ritentarli è inutile)?\n\n'
            '• Sì = salta i permanenti (consigliato)\n'
            '• No = ritenta tutti\n'
            '• Annulla = non fare nulla')
        if choice is None:
            return
        argv += ['--retry-failed']
        if choice:
            argv += ['--retry-skip-permanent']
        # Se sono selezionati archivi, ritenta solo quelli; altrimenti tutti.
        for a in self._selected_archives():
            argv += ['--archive', a]
        self._launch(argv, label='retry-failed')

    def on_reset_state(self) -> None:
        # Azzera lo state.db (resume/dedup/conteggi/falliti) con backup. È una
        # operazione locale e istantanea: la facciamo in-process, non via
        # subprocess. NON tocca i .eml.
        out = self.var_output.get().strip()
        if not out:
            messagebox.showerror('Reset state.db',
                                 'Scegli una cartella di output.')
            return
        if self.proc is not None:
            messagebox.showwarning('Occupato',
                                   'Ferma l\'operazione in corso prima del reset.')
            return
        db = Path(out) / '.mailstore_webapi_export' / 'state.db'
        if not db.exists():
            messagebox.showinfo('Reset state.db',
                                f'Nessuno state.db trovato in:\n{db.parent}')
            return
        if not messagebox.askyesno(
                'Reset state.db',
                'Azzerare lo state.db?\n\n'
                'Verrà fatto un BACKUP automatico (state.db.bak-…), poi '
                'cancellato lo state.db: resume, dedup, conteggi e lista '
                'falliti. I file .eml NON vengono toccati.\n\n'
                '⚠ Un nuovo export ripartirà da ZERO. Se i .eml sono già '
                'presenti, verranno creati DUPLICATI (_1, _2). Per un restart '
                'davvero pulito usa una cartella di output nuova/vuota.\n\n'
                'Procedere?'):
            return
        try:
            from mailstore_webapi_export import reset_state_db
            backup = reset_state_db(Path(out))
            self._log_line(f'$ state.db azzerato. Backup: {backup}')
            messagebox.showinfo('Reset state.db',
                                f'Fatto.\nBackup salvato in:\n{backup}')
        except Exception as exc:
            messagebox.showerror('Reset state.db', f'Errore: {exc}')

    # ----- subprocess lifecycle --------------------------------------------

    def _launch(self, argv: list[str], label: str) -> None:
        if self.proc is not None:
            messagebox.showwarning('Occupato', 'C\'è già un\'operazione in corso.')
            return
        if not CLI_SCRIPT.exists():
            messagebox.showerror('Errore', f'Script non trovato:\n{CLI_SCRIPT}')
            return
        try:
            self._tmp_env_path = self._write_tmp_env(
                self.var_pass.get() or None, self.var_token.get().strip() or None)
        except Exception as exc:
            messagebox.showerror('Errore', f'Impossibile creare env temp:\n{exc}')
            return
        if self._tmp_env_path:
            argv += ['--env-file', self._tmp_env_path]
        self._stop_requested = False
        self._set_running(True)
        self._start_indeterminate(f'{label}: avvio…')
        self._log_line('')
        self._log_line(f'$ {label}  (avvio…)')
        env = dict(os.environ)
        env['PYTHONUNBUFFERED'] = '1'
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(APP_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except Exception as exc:
            self._log_line(f'[errore] impossibile avviare: {exc}')
            self.pbar.stop()
            self._cleanup_after_proc()
            self._set_running(False)
            return
        threading.Thread(target=self._reader, args=(self.proc,),
                         daemon=True).start()
        self.var_status.set(f'In esecuzione: {label} (PID {self.proc.pid})')

    def _reader(self, proc: subprocess.Popen) -> None:
        dec = codecs.getincrementaldecoder('utf-8')('replace')
        cur = ''
        fd = proc.stdout
        try:
            while True:
                chunk = fd.read(4096)
                if not chunk:
                    break
                for ch in dec.decode(chunk):
                    if ch == '\n':
                        self.events.put(('line', cur)); cur = ''
                    elif ch == '\r':
                        self.events.put(('replace', cur)); cur = ''
                    else:
                        cur += ch
        except Exception:
            pass
        finally:
            if cur:
                self.events.put(('line', cur))
            rc = proc.wait()
            self.events.put(('done', rc))

    # ----- event pump ------------------------------------------------------

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'line':
                    self._maybe_update_progress(payload)
                    self._commit_line(payload)
                elif kind == 'replace':
                    self._maybe_update_progress(payload)
                    self._replace_line(payload)
                elif kind == 'archives':
                    self.btn_test.set_state(True)
                    self._populate_archives(payload)
                elif kind == 'test_error':
                    self.btn_test.set_state(True)
                    self.var_status.set('Connessione fallita.')
                    self._log_line(f'[test] ERRORE: {payload}')
                    messagebox.showerror('Test connessione', payload)
                elif kind == 'done':
                    self._on_proc_done(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    # ----- progress bar ----------------------------------------------------

    def _start_indeterminate(self, text: str) -> None:
        self._pbar_determinate = False
        self.pbar.start()
        self.var_prog_text.set(text)

    def _maybe_update_progress(self, text: str) -> None:
        m = PROG_RE.search(text)
        if not m:
            return
        done = _to_int(m.group('dm'))
        total = _to_int(m.group('tm'))
        pct = float(m.group('pct'))
        rate = _to_int(m.group('rate'))
        df, tf = m.group('df'), m.group('tf')
        skip = _to_int(m.group('skip'))
        fail = _to_int(m.group('fail'))
        if not self._pbar_determinate:
            self.pbar.stop()
            self._pbar_determinate = True
        self.pbar.set(pct)
        eta = _fmt_eta((total - done) / rate) if rate else '—'
        self.var_prog_text.set(
            f'{pct:.1f}%   ·   {done:,}/{total:,} msg   ·   {rate:,} msg/s'
            f'   ·   ETA {eta}   ·   cartelle {df}/{tf}'
            f'   ·   skip {skip:,} · fail {fail:,}')

    # ----- log helpers -----------------------------------------------------

    def _log_enable(self):
        self.log.configure(state='normal')

    def _log_disable(self):
        self.log.configure(state='disabled')
        self.log.see('end')

    def _log_line(self, text: str) -> None:
        self._commit_line(text)

    def _commit_line(self, text: str) -> None:
        self._log_enable()
        if self._transient_line:
            self.log.delete('end-1l linestart', 'end-1c')
            self.log.insert('end', text)
        else:
            self.log.insert('end', text)
        self.log.insert('end', '\n')
        self._transient_line = False
        self._log_disable()

    def _replace_line(self, text: str) -> None:
        if not text:
            return
        self._log_enable()
        if self._transient_line:
            self.log.delete('end-1l linestart', 'end-1c')
            self.log.insert('end', text)
        else:
            self.log.insert('end', text)
        self._transient_line = True
        self._log_disable()

    # ----- running-state toggles -------------------------------------------

    def _set_running(self, running: bool) -> None:
        for b in self.action_buttons:
            b.set_state(not running)
        self.btn_test.set_state(not running)
        self.btn_stop.set_state(running)

    def on_stop(self) -> None:
        if self.proc is None:
            return
        if not self._stop_requested:
            self._stop_requested = True
            self._log_line('[stop] interruzione richiesta (SIGINT) — '
                           'completo l\'operazione in corso…')
            self.var_status.set('Interruzione in corso…')
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception as exc:
                self._log_line(f'[stop] SIGINT fallito: {exc}')
        else:
            self._log_line('[stop] forzo la chiusura (SIGTERM).')
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _on_proc_done(self, rc: int) -> None:
        self.pbar.stop()
        if rc == 0 and self._pbar_determinate:
            self.pbar.set(100.0)
        self._log_line(f'$ terminato (exit code {rc}).')
        ok = 'completata' if rc == 0 else f'uscita con codice {rc}'
        self.var_prog_text.set(f'Operazione {ok}.')
        self.var_status.set(f'Terminato (exit {rc}).')
        self._cleanup_after_proc()
        self._set_running(False)

    def _cleanup_after_proc(self) -> None:
        self.proc = None
        self._stop_requested = False
        if self._tmp_env_path:
            try:
                os.unlink(self._tmp_env_path)
            except OSError:
                pass
            self._tmp_env_path = None

    def _on_close(self) -> None:
        if self.proc is not None:
            if not messagebox.askyesno(
                    'Uscita',
                    'Un\'operazione è ancora in corso. Interromperla e uscire?'):
                return
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self._cleanup_after_proc()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MailStoreGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
