#!/usr/bin/env python3
"""Negative tests for the offline artifact gate; fixtures cannot boot a phone."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('verify_loader', Path(__file__).with_name('verify-loader-build.py'))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class BuildRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='thyme-build-record-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.artifact = self.directory / 'loader.efi'
        self.manifest = self.directory / 'loader.build.json'
        self.identity = self.directory / 'test-identity.h'
        self.identity.write_text('test fixture, no Apple/device data')
        self.sources = self.directory / 'src/loader'
        for name in gate.SOURCES:
            path = self.sources / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source fixture ' + name)
        data = bytearray(512)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 0x3c, 64)
        data[64:68] = b'PE\0\0'
        struct.pack_into('<H', data, 68, 0xaa64)
        struct.pack_into('<H', data, 88, 0x20b)
        struct.pack_into('<Q', data, 112, 0xa1000000)
        struct.pack_into('<H', data, 156, 10)
        self.artifact.write_bytes(data)
        self.revision = 'a' * 40
        self.record = {'schema': 1, 'revision': self.revision, 'tracked_source_dirty': False,
                       'direct_storage_writes': False, 'target': 'aarch64-unknown-windows',
                       'image_base': '0xA1000000',
                       'artifact': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
                       'sources': {name: hashlib.sha256((self.sources / name).read_bytes()).hexdigest()
                                   for name in gate.SOURCES},
                       'identity_sha256': hashlib.sha256(self.identity.read_bytes()).hexdigest()}

    def check(self):
        self.manifest.write_text(json.dumps(self.record))
        return gate.verify(self.artifact, self.manifest, self.sources, self.identity, self.revision)

    def test_matching_record(self):
        self.assertEqual(self.check(), [])

    def test_writable_dirty_unknown_or_wrong_revision_are_blocked(self):
        for key, value in (('direct_storage_writes', True), ('direct_storage_writes', None),
                           ('tracked_source_dirty', True), ('tracked_source_dirty', None),
                           ('revision', 'b' * 40), ('schema', 2)):
            old = self.record[key]
            self.record[key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(self.check())
            self.record[key] = old

    def test_replaced_artifact_source_or_identity_are_blocked(self):
        for path in (self.artifact, self.sources / 'loader.c', self.identity,
                     self.sources / '../kernel/bsd/sys/thyme_storage_layout.h'):
            old = path.read_bytes()
            path.write_bytes(old + b'changed')
            with self.subTest(path=path.name):
                self.assertTrue(self.check())
            path.write_bytes(old)

    def test_bad_machine_is_blocked_even_if_hash_is_updated(self):
        data = bytearray(self.artifact.read_bytes())
        struct.pack_into('<H', data, 68, 0x8664)
        self.artifact.write_bytes(data)
        self.record['artifact']['sha256'] = hashlib.sha256(data).hexdigest()
        self.assertTrue(any('EFI application' in item for item in self.check()))

    def test_unexpected_source_list_is_blocked_without_following_paths(self):
        self.record['sources']['../outside'] = 'not read'
        self.assertIn('unexpected source-input list', self.check())


if __name__ == '__main__':
    unittest.main(verbosity=2)
