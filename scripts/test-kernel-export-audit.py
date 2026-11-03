#!/usr/bin/env python3
"""Exercise aliases and failure reporting without a kernel or phone."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('export_audit', Path(__file__).with_name('audit-kernel-exports.py'))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='thyme-exports-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.symbols = self.directory / 'allsymbols'
        self.exports = self.directory / 'Example.exports'

    def test_alias_uses_target_and_preserves_comments(self):
        self.symbols.write_text('_actual\n_direct\n')
        self.exports.write_text('# comment\n_alias: _actual # mapping\n_direct\n')
        report = audit.audit(self.symbols, [self.exports])
        self.assertEqual(report['checked_declarations'], 2)
        self.assertEqual(report['missing'], [])

    def test_missing_declarations_keep_source_locations(self):
        self.symbols.write_text('_present\n')
        self.exports.write_text('_present\n_alias:_missing\n_absent\n')
        report = audit.audit(self.symbols, [self.exports])
        self.assertEqual(report['missing_by_file'], {'Example.exports': 2})
        self.assertEqual(report['missing'][0]['exported'], '_alias')
        self.assertEqual(report['missing'][0]['target'], '_missing')
        self.assertEqual(report['missing'][0]['line'], 2)

    def test_wildcard_exports_are_not_literal_symbols(self):
        self.symbols.write_text('__ZN16IOPlatformExpert5startEv\n_present\n')
        self.exports.write_text('__ZN16IOPlatformExpert*\n_missing*\n')
        report = audit.audit(self.symbols, [self.exports])
        self.assertEqual(report['missing_declarations'], 1)
        self.assertEqual(report['missing'][0]['target'], '_missing*')

    def test_empty_or_wrong_nm_format_is_not_success(self):
        self.exports.write_text('_present\n')
        for contents in ('', '00000000 T _present\n'):
            self.symbols.write_text(contents)
            with self.assertRaises(ValueError):
                audit.audit(self.symbols, [self.exports])

    def test_empty_or_unknown_export_syntax_is_not_success(self):
        self.symbols.write_text('_present\n')
        for contents in ('# nothing\n', '_first _second\n', '_alias: not_a_symbol\n',
                         '_alias:_target*\n', '_bad*pattern\n', '_not_native?\n'):
            self.exports.write_text(contents)
            with self.assertRaises(ValueError):
                audit.audit(self.symbols, [self.exports])


if __name__ == '__main__':
    unittest.main(verbosity=2)
