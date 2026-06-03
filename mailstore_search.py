#!/usr/bin/env python3
"""
MailStore Search Tool
=====================
Full-text search across all MailStore archives via the Web Access REST API.
Prints a results table and dumps every match into a JSON file (no .eml
download). Reuses WebClient and the .env loader from mailstore_webapi_export.py.

Usage:
    # Interactive wizard (recommended): pick archives + scope at runtime
    python3 mailstore_search.py

    # One-shot with a query on the command line
    python3 mailstore_search.py "fattura 2024"

    # Fully non-interactive (cron / scripts)
    python3 mailstore_search.py --no-interactive \
        --archive admin --archive b.montanari \
        --json-output /tmp/hits.json "ordine 1234"

Only stdlib. Python 3.9+. macOS / Linux / Windows.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    from mailstore_webapi_export import (
        WebClient, AuthExpired, ApiError, load_env_file,
        _is_cached_search_expired,
    )
    from mailstore_export import (
        banner, section, ask, ask_int, ask_yes_no, ask_password,
        select_folders_interactive, _is_interactive_tty, sym, decode_subject,
    )
except ImportError as e:
    print(f"ERROR: mailstore_webapi_export.py and mailstore_export.py must be "
          f"in the same directory ({e}).", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Search payload
# ---------------------------------------------------------------------------
# Cap on parallel searches. MailStore's search cache saturates fast: >8
# concurrent searches start returning HTTP 500 "cached search expired".
MAX_PARALLEL_SEARCHES = 8

# Page size for /api/searches/{id}/results.
RESULTS_PAGE_SIZE = 500


def build_search_payload(query: str, scope: dict, folder: str | None,
                         recurse: bool = True) -> dict:
    """Build the JSON body for POST /api/searches.

    Field order and full key set match the payload that MailStore Web Access
    sends from the browser (captured via DevTools). All fields appear to be
    required for the API's input validation, even when null.
    """
    return {
        "querySubject":            bool(scope.get("subject", True)),
        "queryFromToCcBcc":        bool(scope.get("addresses", True)),
        "queryMessageBody":        bool(scope.get("body", True)),
        "queryAttachments":        bool(scope.get("attachments", True)),
        "queryAttachmentContents": bool(scope.get("attachment_contents", False)),
        "hasAttachments":          None,
        "folderRecurse":           bool(recurse),
        "query":                   query,
        "from":                    None,
        "to":                      None,
        "folder":                  folder or None,
        "priority":                "NotSpecified",
        "sizeMin":                 None,
        "sizeMax":                 None,
    }


# ---------------------------------------------------------------------------
# Search execution
# ---------------------------------------------------------------------------

def _create_text_search(client: WebClient, query: str, scope: dict,
                        folder: str, sort: str = "d-desc") -> str:
    """POST /api/searches with a free-text query. Returns the searchId."""
    body = build_search_payload(query, scope, folder, recurse=True)
    data = client._json(
        "POST", "/api/searches",
        params={"sortCriteria": sort},
        json_body=body,
    )
    sid = data.get("searchId")
    if not sid:
        raise ApiError(0, f"create_search returned no searchId: {data}", b"")
    return sid


def _download_url(base_url: str, gid, mid) -> str:
    """Authenticated URL to fetch a single message as raw .eml bytes.

    Requires the Bearer token in the Authorization header — pasting the URL
    into a browser session that's already logged into Web Access also works.
    """
    return f"{base_url}/api/messages/{gid}/{mid}/download/"


def _collect_all_results(client: WebClient, sid: str, total: int,
                         logger: logging.Logger,
                         page_size: int = RESULTS_PAGE_SIZE) -> list[dict]:
    """Page through /results until every match has been collected.

    Each returned item is annotated with `download_url`.
    """
    out: list[dict] = []
    start = 0
    base = client.base_url
    while start < total:
        try:
            res = client.get_search_results(sid, start, page_size)
        except AuthExpired:
            raise
        except ApiError as e:
            if _is_cached_search_expired(e):
                logger.warning(f"Search cache expired mid-pagination at "
                               f"offset {start}; partial results returned "
                               f"({len(out)}/{total}).")
                break
            raise
        items = res.get("searchResultItems") or []
        if not items:
            break
        for it in items:
            gid, mid = it.get("gid"), it.get("mid")
            if gid is not None and mid is not None:
                it["download_url"] = _download_url(base, gid, mid)
        out.extend(items)
        start += page_size
    return out


def search_archive(client: WebClient, archive: str, query: str, scope: dict,
                   logger: logging.Logger) -> dict:
    """Run a single recursive search over one archive."""
    t0 = time.time()
    try:
        sid = _create_text_search(client, query, scope, folder=archive)
        client.wait_search_done(sid, timeout=600)
        total = client.get_search_count(sid)
    except AuthExpired:
        raise
    except Exception as e:
        logger.error(f"Search failed for archive {archive!r}: {e}")
        return {
            "archive": archive,
            "matches": 0,
            "items": [],
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e),
        }

    items: list[dict] = []
    if total > 0:
        try:
            items = _collect_all_results(client, sid, total, logger)
        except AuthExpired:
            raise
        except Exception as e:
            logger.error(f"collect_results failed for {archive!r}: {e}")

    return {
        "archive": archive,
        "matches": total,
        "items": items,
        "elapsed_s": round(time.time() - t0, 2),
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _trim(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_subject(item: dict, n: int = 70) -> str:
    raw = item.get("subject") or ""
    if raw:
        try:
            raw = decode_subject(raw)
        except Exception:
            pass
    return _trim(raw or "(no subject)", n)


def _fmt_from(item: dict, n: int = 38) -> str:
    # MailStore returns a single "fromOrTo" field that holds the sender for
    # incoming mail and the recipient for outgoing mail.
    f = (item.get("fromOrTo")
         or item.get("from")
         or item.get("fromAddress")
         or item.get("sender")
         or "")
    return _trim(f, n)


def _fmt_date(item: dict) -> str:
    d = item.get("date") or item.get("messageDate") or ""
    return d[:19].replace("T", " ") if d else ""


def _fmt_folder(item: dict, archive: str, n: int = 30) -> str:
    fp = item.get("folder") or ""
    # Folder usually starts with the archive name; strip it for compactness.
    prefix = archive + "/"
    if fp.startswith(prefix):
        fp = fp[len(prefix):]
    return _trim(fp, n)


def print_results_table(results: list[dict], max_rows: int) -> None:
    total = sum(r["matches"] for r in results)
    print()
    print("=" * 120)
    print(f"RESULTS: {total:,} matches across {len(results)} archive(s)")
    print("=" * 120)
    if total == 0:
        print("No matches.")
        return

    print(f"{'Date':<19}  {'Archive':<20}  {'Folder':<30}  "
          f"{'From':<38}  Subject")
    print("-" * 120)
    shown = 0
    for r in results:
        for it in r["items"]:
            if shown >= max_rows:
                print(f"... and {total - shown:,} more (see JSON for the full list)")
                return
            print(
                f"{_fmt_date(it):<19}  "
                f"{_trim(r['archive'], 20):<20}  "
                f"{_fmt_folder(it, r['archive']):<30}  "
                f"{_fmt_from(it):<38}  "
                f"{_fmt_subject(it)}"
            )
            shown += 1


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

def run_wizard(env: dict, query_arg: str | None) -> dict:
    banner("MailStore — Search across all archives")

    section("1/4 — Connection")
    host = env.get("MAILSTORE_HOST") or ask("MailStore host", default="127.0.0.1")
    env_port = env.get("MAILSTORE_PORT")
    port = int(env_port) if env_port else ask_int("Web Access port", default=8462,
                                                   min_val=1, max_val=65535)
    username = env.get("MAILSTORE_USER") or ask("Username", default="admin")
    password = env.get("MAILSTORE_PASSWORD") or ask_password("Password")

    base_url = f"https://{host}:{port}"
    client = WebClient(base_url, username=username, password=password,
                       verify_tls=False)

    print()
    print(f"  Logging in to {base_url} ...")
    try:
        client.authenticate()
        archives = client.get_archives()
    except AuthExpired as e:
        print(f'  {sym("✗", "X")} Login failed: {e.message}')
        sys.exit(2)
    except ApiError as e:
        print(f'  {sym("✗", "X")} API error: {e}')
        sys.exit(2)
    print(f'  {sym("✓", "OK")} Connected. {len(archives)} archives found.')

    section("2/4 — Archive selection")
    arc_names = [a["name"] for a in archives if a.get("containsFolders")]
    if not arc_names:
        print("No non-empty archives. Nothing to search.")
        sys.exit(0)
    chosen = select_folders_interactive(arc_names)
    if not chosen:
        print("No archives selected.")
        sys.exit(0)

    section("3/4 — Search query")
    if query_arg:
        query = query_arg
        print(f"  Query (from CLI): {query!r}")
    else:
        query = ask("Text to search for")
    print("  Search scope (which fields to match):")
    scope = {
        "subject":             ask_yes_no("  - subject",                       default=True),
        "body":                ask_yes_no("  - message body",                  default=True),
        "addresses":           ask_yes_no("  - From / To / Cc / Bcc",          default=True),
        "attachments":         ask_yes_no("  - attachment filenames",          default=True),
        "attachment_contents": ask_yes_no("  - attachment contents (slower)",  default=False),
    }

    section("4/4 — Output")
    default_json = (Path.cwd()
                    / f'mailstore_search_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')
    json_path = Path(ask("Output JSON path",
                         default=str(default_json))).expanduser().resolve()

    print()
    print(f"  Will search {len(chosen)} archive(s) for {query!r} "
          f"(scope: {[k for k, v in scope.items() if v]})")
    if not ask_yes_no("Start search?", default=True):
        sys.exit(0)

    return {
        "client": client,
        "archives": chosen,
        "query": query,
        "scope": scope,
        "json_path": json_path,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mailstore_search")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return logger


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search across all MailStore email via the Web Access REST API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("query", nargs="?", default=None,
                        help="Search text (asked at runtime if omitted)")
    parser.add_argument("--host",     default=None)
    parser.add_argument("--port",     type=int, default=None)
    parser.add_argument("--user", "--username", dest="username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--archive",  action="append", default=[],
                        help="Archive name to search (repeatable)")
    parser.add_argument("--workers",  type=int, default=4,
                        help=f"Parallel searches (max {MAX_PARALLEL_SEARCHES})")
    parser.add_argument("--no-subject",          action="store_true",
                        help="Do NOT search the Subject field")
    parser.add_argument("--no-body",             action="store_true",
                        help="Do NOT search the message body")
    parser.add_argument("--no-addresses",        action="store_true",
                        help="Do NOT search From/To/Cc/Bcc")
    parser.add_argument("--no-attachments",      action="store_true",
                        help="Do NOT search attachment filenames")
    parser.add_argument("--attachment-contents", action="store_true",
                        help="Also search inside extracted attachment text (slow)")
    parser.add_argument("--json-output", type=Path, default=None,
                        help="Output JSON path "
                             "(default: ./mailstore_search_YYYYMMDD_HHMMSS.json)")
    parser.add_argument("--max-rows",    type=int, default=200,
                        help="Maximum rows printed to stdout "
                             "(all matches are still saved to JSON)")
    parser.add_argument("--env-file", type=Path, default=None,
                        help=".env file to load (default: ./.env)")
    parser.add_argument("-i", "--interactive",   action="store_true")
    parser.add_argument("--no-interactive",      action="store_true")
    args = parser.parse_args()

    env = load_env_file(args.env_file)
    if env:
        print(f".env loaded: {len(env)} variables")

    eff_host = args.host or env.get("MAILSTORE_HOST")
    eff_port = args.port or (int(env.get("MAILSTORE_PORT"))
                             if env.get("MAILSTORE_PORT") else None)
    eff_user = args.username or env.get("MAILSTORE_USER")
    eff_pass = args.password or env.get("MAILSTORE_PASSWORD")
    eff_arc = list(args.archive) if args.archive else (
        [s.strip() for s in (env.get("MAILSTORE_ARCHIVES") or "").split(",")
         if s.strip()])

    has_creds = bool(eff_user)
    missing = (not has_creds) or (not eff_arc) or (not args.query)
    use_wizard = args.interactive or (
        missing and not args.no_interactive and _is_interactive_tty()
    )

    if use_wizard:
        cfg = run_wizard(env, args.query)
        client    = cfg["client"]
        archives  = cfg["archives"]
        query     = cfg["query"]
        scope     = cfg["scope"]
        json_path = cfg["json_path"]
    else:
        if missing:
            print("ERROR: non-interactive mode requires --user, at least one "
                  "--archive, and a query.", file=sys.stderr)
            sys.exit(2)
        host = eff_host or "127.0.0.1"
        port = eff_port or 8462
        client = WebClient(f"https://{host}:{port}",
                           username=eff_user, password=eff_pass,
                           verify_tls=False)
        try:
            client.authenticate()
        except AuthExpired as e:
            print(f"Login failed: {e.message}", file=sys.stderr)
            sys.exit(2)
        archives = eff_arc
        query    = args.query
        scope = {
            "subject":             not args.no_subject,
            "body":                not args.no_body,
            "addresses":           not args.no_addresses,
            "attachments":         not args.no_attachments,
            "attachment_contents": bool(args.attachment_contents),
        }
        json_path = (args.json_output or
                     Path.cwd() / f'mailstore_search_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')

    workers = max(1, min(args.workers, MAX_PARALLEL_SEARCHES))
    log_path = json_path.with_suffix(".log")
    logger = _setup_logger(log_path)
    logger.info(f"query={query!r} archives={archives} scope={scope} "
                f"workers={workers}")

    stop_event = threading.Event()
    def handle_sig(signum, frame):
        if not stop_event.is_set():
            print(f'\n{sym("⚠", "!")}  Interrupt requested, finishing current '
                  f'searches...\n', file=sys.stderr)
            stop_event.set()
        else:
            print(f'\n{sym("⚠", "!")}  Second interrupt, forcing exit.\n',
                  file=sys.stderr)
            sys.exit(130)
    signal.signal(signal.SIGINT,  handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print()
    print(f"Searching {len(archives)} archive(s) for {query!r} "
          f"(scope: {[k for k, v in scope.items() if v]}, workers: {workers})")
    print()

    t0 = time.time()
    results: list[dict] = []
    auth_aborted = False

    with ThreadPoolExecutor(max_workers=workers,
                             thread_name_prefix="search") as pool:
        futures = {pool.submit(search_archive, client, a, query, scope, logger): a
                   for a in archives}
        done_n = 0
        for fut in as_completed(futures):
            arc = futures[fut]
            if stop_event.is_set():
                # Let in-flight futures finish; just stop counting print noise.
                continue
            try:
                r = fut.result()
            except AuthExpired:
                logger.error("Auth expired during search, aborting.")
                auth_aborted = True
                stop_event.set()
                continue
            except Exception as e:
                logger.exception(f"Search worker exception on {arc!r}: {e}")
                r = {"archive": arc, "matches": 0, "items": [],
                     "elapsed_s": 0.0, "error": str(e)}
            results.append(r)
            done_n += 1
            err = f"  ERROR: {r['error']}" if r.get("error") else ""
            print(f'  [{done_n}/{len(archives)}] {arc[:30]:<30}  '
                  f'{r["matches"]:>7,} match  ({r["elapsed_s"]}s){err}')

    elapsed = time.time() - t0
    results.sort(key=lambda r: r["matches"], reverse=True)

    print_results_table(results, max_rows=args.max_rows)

    summary = {
        "searched_at":       datetime.now(timezone.utc).isoformat(),
        "duration_seconds":  round(elapsed, 2),
        "host":              client.base_url,
        "query":             query,
        "scope":             scope,
        "archives_searched": archives,
        "workers":           workers,
        "aborted":           bool(auth_aborted or stop_event.is_set()),
        "total_matches":     sum(r["matches"] for r in results),
        "archives":          results,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print()
    print(f'Elapsed: {elapsed:.1f}s   Total matches: {summary["total_matches"]:,}')
    print(f"JSON:    {json_path}")
    print(f"Log:     {log_path}")
    if auth_aborted:
        print(f'{sym("⚠","!")}  Aborted due to expired auth. '
              f'Re-run the same command.')
    elif stop_event.is_set():
        print(f'{sym("⚠","!")}  Interrupted. Partial results were saved.')


if __name__ == "__main__":
    main()
