#!/usr/bin/env python3
"""
Unit tests for the pure (side-effect-free) functions across the MailStore
Export scripts: path sanitization, filename building, MIME decoding, IMAP
response parsing, .env loading and date helpers.

Stdlib `unittest` only — no external test runner, in keeping with the project's
zero-dependency philosophy. Run with:

    python3 -m unittest discover -s tests -v
    # or
    python3 tests/test_pure_functions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# Make the repository root importable when tests are run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mailstore_export as ex            # noqa: E402
import mailstore_webapi_export as wex    # noqa: E402
import mailstore_search as search        # noqa: E402


# ============================================================
# sanitize_component
# ============================================================

class TestSanitizeComponent(unittest.TestCase):
    def test_forbidden_chars_become_underscore(self):
        self.assertEqual(ex.sanitize_component('foo:bar/baz'), 'foo_bar_baz')

    def test_all_invalid_chars(self):
        self.assertEqual(ex.sanitize_component('a<b>c:d"e\\f|g?h*i'),
                         'a_b_c_d_e_f_g_h_i')

    def test_empty_becomes_underscore(self):
        self.assertEqual(ex.sanitize_component(''), '_')

    def test_whitespace_only_becomes_underscore(self):
        self.assertEqual(ex.sanitize_component('   '), '_')

    def test_runs_of_spaces_collapsed(self):
        self.assertEqual(ex.sanitize_component('foo   bar    baz'), 'foo bar baz')

    def test_tabs_and_newlines_become_underscore(self):
        # Tab/newline are control chars (\x00-\x1f) and forbidden, so they are
        # replaced with '_' before the space-collapse step — they are not turned
        # into spaces.
        self.assertEqual(ex.sanitize_component('foo\tbar\nbaz'), 'foo_bar_baz')

    def test_trailing_dots_and_spaces_stripped(self):
        self.assertEqual(ex.sanitize_component('report...  '), 'report')

    def test_control_chars_removed(self):
        self.assertEqual(ex.sanitize_component('a\x00b\x1fc'), 'a_b_c')

    def test_unicode_is_preserved_and_nfc(self):
        # é as decomposed (e + combining accent) must normalize to composed NFC.
        decomposed = 'Café'
        out = ex.sanitize_component(decomposed)
        self.assertEqual(out, 'Café')
        self.assertEqual(out, 'Café')

    def test_byte_truncation_respects_utf8_boundary(self):
        # 100 multibyte chars (2 bytes each) truncated to 10 bytes must not
        # split a character and must stay within the byte budget.
        out = ex.sanitize_component('é' * 100, max_len=10)
        self.assertLessEqual(len(out.encode('utf-8')), 10)
        # No replacement character / mojibake from a split codepoint.
        self.assertNotIn('�', out)


# ============================================================
# build_filename
# ============================================================

class TestBuildFilename(unittest.TestCase):
    def test_documented_example(self):
        fn = ex.build_filename(datetime(2023, 5, 15, 14, 30, 45),
                               'Re: Fattura n.123', 'abc123def456')
        self.assertEqual(fn, 'Re_ Fattura n.123_20230515_143045_abc123de.eml')

    def test_missing_date_uses_placeholder(self):
        fn = ex.build_filename(None, 'hello', 'deadbeefcafe')
        self.assertEqual(fn, 'hello_00000000_000000_deadbeef.eml')

    def test_empty_subject_falls_back(self):
        fn = ex.build_filename(datetime(2023, 1, 1, 0, 0, 0), '', '0011' * 4)
        self.assertTrue(fn.startswith('no_subject_'))

    def test_hash_truncated_to_8(self):
        fn = ex.build_filename(None, 'x', 'abcdef0123456789')
        self.assertIn('abcdef01', fn)
        self.assertNotIn('abcdef0123', fn)

    def test_long_subject_stays_within_byte_budget(self):
        fn = ex.build_filename(datetime(2023, 5, 15, 14, 30, 45),
                               'A' * 1000, 'abc123def456')
        self.assertLessEqual(len(fn.encode('utf-8')), ex.MAX_FILENAME_LEN)
        # date + hash must survive truncation
        self.assertIn('20230515_143045', fn)
        self.assertIn('abc123de', fn)
        self.assertTrue(fn.endswith('.eml'))


# ============================================================
# decode_subject
# ============================================================

class TestDecodeSubject(unittest.TestCase):
    def test_mime_quoted_printable_utf8(self):
        self.assertEqual(ex.decode_subject('=?utf-8?Q?Caf=C3=A9?='), 'Café')

    def test_mime_base64(self):
        self.assertEqual(ex.decode_subject('=?utf-8?B?Q2Fmw6k=?='), 'Café')

    def test_plain_passthrough(self):
        self.assertEqual(ex.decode_subject('plain subject'), 'plain subject')

    def test_empty(self):
        self.assertEqual(ex.decode_subject(''), '')

    def test_multipart_mixed_charsets(self):
        out = ex.decode_subject('=?utf-8?Q?Caf=C3=A9?= and =?iso-8859-1?Q?caf=E9?=')
        self.assertIn('Café', out)
        self.assertIn('café', out)


# ============================================================
# parse_imap_date
# ============================================================

class TestParseImapDate(unittest.TestCase):
    def test_rfc2822(self):
        dt = ex.parse_imap_date('Mon, 15 May 2023 14:30:45 +0200')
        self.assertIsNotNone(dt)
        self.assertEqual((dt.year, dt.month, dt.day), (2023, 5, 15))

    def test_empty_is_none(self):
        self.assertIsNone(ex.parse_imap_date(''))

    def test_garbage_is_none(self):
        self.assertIsNone(ex.parse_imap_date('not a date at all'))


# ============================================================
# parse_list_response
# ============================================================

class TestParseListResponse(unittest.TestCase):
    def test_quoted_name(self):
        self.assertEqual(
            ex.parse_list_response(b'(\\HasNoChildren) "/" "INBOX"'), 'INBOX')

    def test_unquoted_name(self):
        self.assertEqual(
            ex.parse_list_response(b'(\\HasNoChildren) "/" INBOX'), 'INBOX')

    def test_dot_delimiter_and_spaces_in_name(self):
        self.assertEqual(
            ex.parse_list_response(b'(\\HasChildren) "." "Sent Items"'),
            'Sent Items')

    def test_escaped_quote_in_name(self):
        self.assertEqual(
            ex.parse_list_response(b'(\\HasNoChildren) "/" "a\\"b"'), 'a"b')

    def test_empty_line_is_none(self):
        self.assertIsNone(ex.parse_list_response(b''))

    def test_garbage_is_none(self):
        self.assertIsNone(ex.parse_list_response(b'totally bogus'))


# ============================================================
# parse_namespace_prefixes
# ============================================================

class TestParseNamespacePrefixes(unittest.TestCase):
    def test_other_and_shared(self):
        raw = (b'NAMESPACE (("" "/")) (("Other Users/" "/")) '
               b'(("Public Folders/" "/"))')
        self.assertEqual(ex.parse_namespace_prefixes(raw),
                         ['Other Users/', 'Public Folders/'])

    def test_personal_only_returns_empty(self):
        self.assertEqual(ex.parse_namespace_prefixes(b'NAMESPACE (("" "/")) NIL NIL'),
                         [])

    def test_empty_is_empty(self):
        self.assertEqual(ex.parse_namespace_prefixes(b''), [])


# ============================================================
# parse_selection
# ============================================================

class TestParseSelection(unittest.TestCase):
    def test_all_keywords(self):
        for kw in ('a', 'all', '*'):
            self.assertEqual(ex.parse_selection(kw, 5), {1, 2, 3, 4, 5})

    def test_none_keywords(self):
        for kw in ('', 'n', 'none'):
            self.assertEqual(ex.parse_selection(kw, 5), set())

    def test_singles(self):
        self.assertEqual(ex.parse_selection('1,3,5', 10), {1, 3, 5})

    def test_range(self):
        self.assertEqual(ex.parse_selection('2-5', 10), {2, 3, 4, 5})

    def test_mixed(self):
        self.assertEqual(ex.parse_selection('1,3,5-9,12', 20),
                         {1, 3, 5, 6, 7, 8, 9, 12})

    def test_out_of_range_filtered(self):
        self.assertEqual(ex.parse_selection('1,99', 5), {1})

    def test_reversed_range_normalized(self):
        self.assertEqual(ex.parse_selection('5-2', 10), {2, 3, 4, 5})

    def test_invalid_single_raises(self):
        with self.assertRaises(ValueError):
            ex.parse_selection('1,abc', 5)

    def test_invalid_range_raises(self):
        with self.assertRaises(ValueError):
            ex.parse_selection('1-x', 5)


# ============================================================
# folder_to_path / safe_local_path_for
# ============================================================

class TestFolderPaths(unittest.TestCase):
    def test_folder_to_path(self):
        root = Path('/tmp/out')
        self.assertEqual(ex.folder_to_path('INBOX/Lavoro/2023', root),
                         root / 'INBOX' / 'Lavoro' / '2023')

    def test_folder_to_path_sanitizes(self):
        root = Path('/tmp/out')
        self.assertEqual(ex.folder_to_path('INBOX/a:b', root),
                         root / 'INBOX' / 'a_b')

    def test_folder_to_path_empty_returns_root(self):
        root = Path('/tmp/out')
        self.assertEqual(ex.folder_to_path('', root), root)

    def test_safe_local_path_for(self):
        root = Path('/tmp/out')
        self.assertEqual(
            wex.safe_local_path_for('admin', 'admin/Exchange/Archive', root),
            root / 'admin' / 'Exchange' / 'Archive')

    def test_safe_local_path_for_sanitizes(self):
        root = Path('/tmp/out')
        self.assertEqual(
            wex.safe_local_path_for('admin', 'admin/a:b/c?d', root),
            root / 'admin' / 'a_b' / 'c_d')


# ============================================================
# load_env_file
# ============================================================

class TestLoadEnvFile(unittest.TestCase):
    def _write_env(self, text: str) -> Path:
        fd, name = tempfile.mkstemp(suffix='.env')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        path = Path(name)
        self.addCleanup(path.unlink)
        return path

    def test_basic_key_value(self):
        env = wex.load_env_file(self._write_env('MAILSTORE_HOST=mail.local\n'))
        self.assertEqual(env['MAILSTORE_HOST'], 'mail.local')

    def test_comments_and_blank_lines_skipped(self):
        env = wex.load_env_file(self._write_env(
            '# comment\n\nMAILSTORE_PORT=8462\n   # indented comment\n'))
        self.assertEqual(env, {'MAILSTORE_PORT': '8462'})

    def test_quotes_stripped(self):
        env = wex.load_env_file(self._write_env(
            'A="double"\nB=\'single\'\n'))
        self.assertEqual(env['A'], 'double')
        self.assertEqual(env['B'], 'single')

    def test_export_prefix(self):
        env = wex.load_env_file(self._write_env('export MAILSTORE_USER=admin\n'))
        self.assertEqual(env['MAILSTORE_USER'], 'admin')

    def test_value_with_equals_sign(self):
        env = wex.load_env_file(self._write_env('MAILSTORE_TOKEN=abc=def=ghi\n'))
        self.assertEqual(env['MAILSTORE_TOKEN'], 'abc=def=ghi')

    def test_missing_file_returns_empty(self):
        self.assertEqual(wex.load_env_file(Path('/nonexistent/.env')), {})


# ============================================================
# _item_year_month
# ============================================================

class TestItemYearMonth(unittest.TestCase):
    def test_iso_with_z(self):
        self.assertEqual(wex._item_year_month({'date': '2023-05-15T14:30:45Z'}),
                         (2023, 5))

    def test_iso_with_offset(self):
        self.assertEqual(
            wex._item_year_month({'date': '2024-12-01T00:00:00+01:00'}),
            (2024, 12))

    def test_missing_date(self):
        self.assertEqual(wex._item_year_month({}), (None, None))

    def test_unparsable_date(self):
        self.assertEqual(wex._item_year_month({'date': 'nope'}), (None, None))


# ============================================================
# _parse_email_date_from_tmp_name
# ============================================================

class TestParseTmpName(unittest.TestCase):
    def test_valid(self):
        dt = wex._parse_email_date_from_tmp_name(
            'Subject_20230515_143045_abc123de.eml.tmp')
        self.assertEqual(dt, datetime(2023, 5, 15, 14, 30, 45))

    def test_valid_with_unique_token(self):
        # Nuovo schema a nome univoco: ..._{hash8}.{pid}-{rand}.eml.tmp
        dt = wex._parse_email_date_from_tmp_name(
            'Subject_20230515_143045_abc123de.40521-1a2b3c4d.eml.tmp')
        self.assertEqual(dt, datetime(2023, 5, 15, 14, 30, 45))

    def test_dotted_subject_with_token(self):
        dt = wex._parse_email_date_from_tmp_name(
            'Re_ Fattura n.123_20230515_143045_abc123de.99-deadbeef.eml.tmp')
        self.assertEqual(dt, datetime(2023, 5, 15, 14, 30, 45))

    def test_placeholder_date_is_none(self):
        self.assertIsNone(wex._parse_email_date_from_tmp_name(
            'Subject_00000000_000000_abc123de.eml.tmp'))

    def test_no_match_is_none(self):
        self.assertIsNone(wex._parse_email_date_from_tmp_name('not-a-tmp.txt'))

    def test_final_eml_not_matched(self):
        # Only .eml.tmp (in-flight) names carry the date marker we parse.
        self.assertIsNone(wex._parse_email_date_from_tmp_name(
            'Subject_20230515_143045_abc123de.eml'))


# ============================================================
# search.build_search_payload / _trim
# ============================================================

class TestSearchPayload(unittest.TestCase):
    def test_defaults(self):
        p = search.build_search_payload('hello', {}, None)
        self.assertEqual(p['query'], 'hello')
        self.assertTrue(p['querySubject'])
        self.assertTrue(p['queryMessageBody'])
        self.assertFalse(p['queryAttachmentContents'])
        self.assertTrue(p['folderRecurse'])
        self.assertIsNone(p['folder'])
        self.assertEqual(p['priority'], 'NotSpecified')

    def test_scope_overrides(self):
        p = search.build_search_payload(
            'q', {'subject': False, 'body': False}, 'admin/INBOX', recurse=False)
        self.assertFalse(p['querySubject'])
        self.assertFalse(p['queryMessageBody'])
        self.assertFalse(p['folderRecurse'])
        self.assertEqual(p['folder'], 'admin/INBOX')

    def test_full_key_set_present(self):
        # The API rejects payloads missing keys, so guard the full shape.
        expected = {
            'querySubject', 'queryFromToCcBcc', 'queryMessageBody',
            'queryAttachments', 'queryAttachmentContents', 'hasAttachments',
            'folderRecurse', 'query', 'from', 'to', 'folder', 'priority',
            'sizeMin', 'sizeMax',
        }
        self.assertEqual(set(search.build_search_payload('q', {}, None)), expected)


class TestTrim(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(search._trim('hello', 70), 'hello')

    def test_long_truncated_with_ellipsis(self):
        out = search._trim('x' * 100, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith('…'))

    def test_newlines_flattened(self):
        self.assertEqual(search._trim('a\nb\r\nc', 70), 'a b c')

    def test_empty(self):
        self.assertEqual(search._trim('', 70), '')


# ============================================================
# _is_permanent_failure (diagnostica retry)
# ============================================================

class TestIsPermanentFailure(unittest.TestCase):
    def test_corrupt_message_500_is_permanent(self):
        # Il segnale principale è il TESTO, riconosciuto anche con status=500.
        self.assertTrue(wex._is_permanent_failure(
            500, 'ApiError',
            'HTTP 500: The message could not be entirely retrieved from the archive.'))

    def test_corrupt_message_detected_even_with_legacy_status_zero(self):
        # Righe salvate prima della colonna status hanno status=0: vanno
        # comunque classificate come permanenti via testo del messaggio.
        self.assertTrue(wex._is_permanent_failure(
            0, 'ApiError',
            'HTTP 500: The message could not be entirely retrieved from the archive.'))

    def test_network_error_is_transient(self):
        self.assertFalse(wex._is_permanent_failure(
            0, 'ApiError', 'HTTP 0: Errore di rete: <urlopen error>'))

    def test_incomplete_read_is_transient(self):
        self.assertFalse(wex._is_permanent_failure(
            0, 'ApiError', 'Download troncato (IncompleteRead): ...'))

    def test_generic_500_without_corrupt_text_is_transient(self):
        self.assertFalse(wex._is_permanent_failure(
            500, 'ApiError', 'HTTP 500: Internal Server Error'))

    def test_empty_message_is_transient(self):
        self.assertFalse(wex._is_permanent_failure(0, 'RuntimeError', ''))


# ============================================================
# _count_years_in_sorted (conteggio per anno via ricerca binaria)
# ============================================================

class TestCountYearsInSorted(unittest.TestCase):
    @staticmethod
    def _make_year_at(years_desc):
        # Lista di anni ordinata in modo DECRESCENTE; restituisce year_at(i).
        return lambda i: years_desc[i] if 0 <= i < len(years_desc) else None

    def test_single_year_middle_block(self):
        # 3x2026, 4x2025, 2x2024  -> il 2025 è in mezzo (non all'inizio)
        ys = [2026, 2026, 2026, 2025, 2025, 2025, 2025, 2024, 2024]
        cnt, spans = wex._count_years_in_sorted(len(ys), {2025},
                                                self._make_year_at(ys))
        self.assertEqual(cnt, 4)
        self.assertEqual(spans, [(2025, 3, 7)])

    def test_multiple_non_contiguous_years(self):
        # seleziona 2025 e 2023, salta 2024
        ys = [2025, 2025, 2024, 2024, 2024, 2023, 2023, 2022]
        cnt, _ = wex._count_years_in_sorted(len(ys), {2025, 2023},
                                            self._make_year_at(ys))
        self.assertEqual(cnt, 4)  # 2 del 2025 + 2 del 2023

    def test_year_not_present(self):
        ys = [2026, 2026, 2024]
        cnt, spans = wex._count_years_in_sorted(len(ys), {2025},
                                                self._make_year_at(ys))
        self.assertEqual(cnt, 0)
        self.assertEqual(spans, [])

    def test_undated_tail_excluded(self):
        # mail senza data (None) scivolano in coda e non contano
        ys = [2025, 2025, 2024, None, None]
        cnt, _ = wex._count_years_in_sorted(len(ys), {2025},
                                            self._make_year_at(ys))
        self.assertEqual(cnt, 2)

    def test_all_match(self):
        ys = [2025, 2025, 2025]
        cnt, _ = wex._count_years_in_sorted(len(ys), {2025},
                                            self._make_year_at(ys))
        self.assertEqual(cnt, 3)

    def test_empty_inputs(self):
        self.assertEqual(wex._count_years_in_sorted(0, {2025}, lambda i: None),
                         (0, []))
        self.assertEqual(wex._count_years_in_sorted(5, set(), lambda i: 2025),
                         (0, []))


# ============================================================
# _format_oserror (log diagnostici Windows/POSIX)
# ============================================================

class TestFormatOSError(unittest.TestCase):
    def test_posix_errno_and_filename(self):
        e = OSError(13, 'Permission denied', '/Volumes/X/a.eml.tmp')
        s = wex._format_oserror(e)
        self.assertIn('errno 13', s)
        self.assertIn('EACCES', s)
        self.assertIn('/Volumes/X/a.eml.tmp', s)

    def test_winerror_file_in_use_hint(self):
        # Simula un OSError stile Windows (WinError 32 = file in uso / lock)
        e = OSError()
        e.winerror = 32
        e.errno = 13
        e.filename = r'C:\export\msg.eml.tmp'
        s = wex._format_oserror(e)
        self.assertIn('WinError 32', s)
        self.assertIn('file in uso', s)
        self.assertIn(r'C:\export\msg.eml.tmp', s)

    def test_winerror_access_denied_hint(self):
        e = OSError()
        e.winerror = 5
        s = wex._format_oserror(e)
        self.assertIn('WinError 5', s)
        self.assertIn('accesso negato', s)


# ============================================================
# _build_date_intervals (filtro -> intervalli di data)
# ============================================================

class TestBuildDateIntervals(unittest.TestCase):
    def test_no_filter_is_none(self):
        self.assertIsNone(wex._build_date_intervals(set(), set()))

    def test_months_only_is_none(self):
        # soli mesi (senza anno) non sono esprimibili a intervalli
        self.assertIsNone(wex._build_date_intervals(set(), {3, 4}))

    def test_single_year(self):
        iv = wex._build_date_intervals({2025}, set())
        self.assertEqual(iv, [(datetime(2025, 1, 1),
                               datetime(2025, 12, 31, 23, 59, 59))])

    def test_adjacent_years_merged(self):
        iv = wex._build_date_intervals({2024, 2025}, set())
        self.assertEqual(iv, [(datetime(2024, 1, 1),
                               datetime(2025, 12, 31, 23, 59, 59))])

    def test_non_contiguous_years_two_intervals_desc(self):
        iv = wex._build_date_intervals({2023, 2025}, set())
        # ordinati per hi DESC: prima 2025, poi 2023
        self.assertEqual(iv[0][0], datetime(2025, 1, 1))
        self.assertEqual(iv[1][0], datetime(2023, 1, 1))
        self.assertEqual(len(iv), 2)

    def test_year_month(self):
        iv = wex._build_date_intervals({2025}, {2})
        self.assertEqual(iv, [(datetime(2025, 2, 1),
                               datetime(2025, 2, 28, 23, 59, 59))])

    def test_year_december_month_boundary(self):
        iv = wex._build_date_intervals({2025}, {12})
        self.assertEqual(iv, [(datetime(2025, 12, 1),
                               datetime(2025, 12, 31, 23, 59, 59))])

    def test_explicit_range_takes_precedence(self):
        df = datetime(2025, 3, 10)
        dt = datetime(2025, 3, 10, 23, 59, 59)
        iv = wex._build_date_intervals({2025}, {1}, df, dt)
        self.assertEqual(iv, [(df, dt)])


# ============================================================
# _span_in_sorted (finestra indici per intervallo, su lista desc)
# ============================================================

class TestSpanInSorted(unittest.TestCase):
    @staticmethod
    def _dt_at(dts_desc):
        return lambda i: dts_desc[i] if 0 <= i < len(dts_desc) else None

    def test_window_in_middle(self):
        # desc; cerchiamo [2025-01-01, 2025-12-31]
        dts = [datetime(2026, 1, 5), datetime(2025, 12, 1),
               datetime(2025, 6, 1), datetime(2025, 1, 2),
               datetime(2024, 11, 1)]
        s, e = wex._span_in_sorted(len(dts), datetime(2025, 1, 1),
                                   datetime(2025, 12, 31, 23, 59, 59),
                                   self._dt_at(dts))
        self.assertEqual((s, e), (1, 4))  # indici 1,2,3 sono 2025

    def test_no_match(self):
        dts = [datetime(2026, 1, 1), datetime(2024, 1, 1)]
        s, e = wex._span_in_sorted(len(dts), datetime(2025, 1, 1),
                                   datetime(2025, 12, 31), self._dt_at(dts))
        self.assertEqual(s, e)

    def test_undated_tail_excluded(self):
        dts = [datetime(2025, 5, 1), datetime(2025, 1, 1), None, None]
        s, e = wex._span_in_sorted(len(dts), datetime(2025, 1, 1),
                                   datetime(2025, 12, 31, 23, 59, 59),
                                   self._dt_at(dts))
        self.assertEqual((s, e), (0, 2))


# ============================================================
# reset_state_db (backup + rimozione state.db, .eml intatti)
# ============================================================

class TestResetStateDb(unittest.TestCase):
    def test_backup_then_remove_keeps_eml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sdir = root / '.mailstore_webapi_export'
            sdir.mkdir()
            (sdir / 'state.db').write_bytes(b'SQLITEDB')
            (sdir / 'state.db-wal').write_bytes(b'wal')
            (sdir / 'state.db-shm').write_bytes(b'shm')
            eml = root / 'INBOX' / 'msg.eml'
            eml.parent.mkdir()
            eml.write_bytes(b'From: x')

            backup = wex.reset_state_db(root)

            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertEqual(backup.read_bytes(), b'SQLITEDB')  # backup fedele
            self.assertFalse((sdir / 'state.db').exists())       # db rimosso
            self.assertFalse((sdir / 'state.db-wal').exists())
            self.assertFalse((sdir / 'state.db-shm').exists())
            self.assertTrue(eml.exists())                        # .eml intatto

    def test_no_db_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.mailstore_webapi_export').mkdir()
            self.assertIsNone(wex.reset_state_db(Path(d)))


# ============================================================
# WriteFailureGuard (circuit breaker scritture)
# ============================================================

class _NullLogger:
    def error(self, *a, **k):
        pass


class TestWriteFailureGuard(unittest.TestCase):
    def _guard(self, threshold):
        import threading
        ev = threading.Event()
        return wex.WriteFailureGuard(threshold, ev, _NullLogger()), ev

    def test_trips_after_consecutive_failures(self):
        g, ev = self._guard(3)
        g.record_write_failure(OSError(13, 'denied'))
        g.record_write_failure(OSError(13, 'denied'))
        self.assertFalse(g.tripped)
        self.assertFalse(ev.is_set())
        g.record_write_failure(OSError(13, 'denied'))
        self.assertTrue(g.tripped)
        self.assertTrue(ev.is_set())          # stop_event signalled
        self.assertEqual(g.total, 3)

    def test_success_resets_consecutive_counter(self):
        g, ev = self._guard(3)
        g.record_write_failure(OSError(13, 'x'))
        g.record_write_failure(OSError(13, 'x'))
        g.record_success()                     # azzera la serie
        g.record_write_failure(OSError(13, 'x'))
        g.record_write_failure(OSError(13, 'x'))
        self.assertFalse(g.tripped)            # mai 3 di fila
        self.assertEqual(g.total, 4)           # ma il totale conta tutti

    def test_threshold_zero_disables(self):
        g, ev = self._guard(0)
        for _ in range(100):
            g.record_write_failure(OSError(13, 'x'))
        self.assertFalse(g.tripped)
        self.assertFalse(ev.is_set())


if __name__ == '__main__':
    unittest.main(verbosity=2)
