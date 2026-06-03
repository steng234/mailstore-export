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


if __name__ == '__main__':
    unittest.main(verbosity=2)
