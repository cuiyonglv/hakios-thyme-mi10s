#!/usr/bin/env python3
"""Test packing, bounds and atomic failure behavior using tiny local fixtures."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('image_builder', Path(__file__).with_name('build-image.py'))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='thyme-pack-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.kernel = self.directory / 'kernel'
        self.small = self.directory / 'root.apfs'
        self.system = self.directory / 'system.apfs'
        self.output = self.directory / 'image'
        self.kernel.write_bytes(struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 2, 0, 0, 0, 0))
        container = bytearray(4096)
        container[32:36] = b'NXSB'
        struct.pack_into('<IQ', container, 36, 4096, 1)
        self.small.write_bytes(container)
        self.system.write_bytes(container)

    def build(self, command='ufs=2 ufs_write=0 var_store=0'):
        return builder.build_image(self.kernel, self.small, self.system, self.output, command)

    def test_format_and_digest(self):
        result = self.build()
        data = self.output.read_bytes()
        self.assertEqual(data[:8], b'IOSSTOR1')
        self.assertEqual(struct.unpack_from('<II', data, 8), (1, 0))
        payload_size, system_size, part1 = struct.unpack_from('<QQQ', data, 16)
        self.assertEqual(system_size, 4096)
        self.assertEqual(part1, system_size)
        self.assertEqual(data[96:128], hashlib.sha256(data[4096:]).digest())
        self.assertEqual(data[4096:4100], b'XNUP')
        self.assertEqual(data[4096 + payload_size:], self.system.read_bytes())
        self.assertEqual(result['image_bytes'], len(data))

    def test_persistence_boundary_and_previous_output(self):
        size = self.build()['image_bytes']
        with patch.object(builder, 'STORE_AT', size):
            self.build()
        self.output.write_bytes(b'previous known image')
        with patch.object(builder, 'STORE_AT', size - 1):
            with self.assertRaisesRegex(ValueError, 'overlaps persistence'):
                self.build()
        self.assertEqual(self.output.read_bytes(), b'previous known image')
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_invalid_kernel(self):
        for data in (b'short', struct.pack('<8I', 0xfeedfacf, 7, 0, 2, 0, 0, 0, 0), b'x' * 32):
            with self.subTest(data=data[:4]):
                self.kernel.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.build()
        self.assertFalse(self.output.exists())

    def test_invalid_apfs(self):
        original = self.system.read_bytes()
        for offset, value in ((32, b'BAD!'), (36, struct.pack('<I', 512)),
                              (40, struct.pack('<Q', 2))):
            data = bytearray(original)
            data[offset:offset + len(value)] = value
            self.system.write_bytes(data)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.build()
        self.assertFalse(self.output.exists())

    def test_bad_command_lines(self):
        for command in ('', 'x\0y', 'x\ny', 'x\ry', 'x' * builder.BOOT_LINE_LENGTH):
            with self.subTest(command=repr(command[:12])), self.assertRaises(ValueError):
                self.build(command)

    def test_v2_descriptor_and_bound_digest(self):
        result = builder.build_image(self.kernel, self.small, self.system, self.output,
                                     layout_version=2, partition_bytes=4 << 20, slot_bytes=65536)
        data = self.output.read_bytes()
        self.assertEqual(data[:8], b'IOSSTOR2')
        self.assertEqual(struct.unpack_from('<II', data, 8), (2, 0))
        store, slot, count, block, flags = struct.unpack_from('<QQIIQ', data, 64)
        self.assertEqual((slot, count, block, flags), (65536, 2, 4096, 0))
        self.assertEqual(store, (len(data) + 4095) // 4096 * 4096)
        self.assertEqual(result['storage_layout']['store_at'], store)
        expected = hashlib.sha256(data[:96] + data[4096:]).digest()
        self.assertEqual(data[96:128], expected)
        changed = bytearray(data)
        changed[64] ^= 1
        self.assertNotEqual(hashlib.sha256(changed[:96] + changed[4096:]).digest(), expected)
        with patch.object(builder, 'STORE_AT', 1):
            builder.build_image(self.kernel, self.small, self.system, self.output,
                                layout_version=2, partition_bytes=4 << 20, slot_bytes=65536)

    def test_v2_invalid_capacity_preserves_output(self):
        self.output.write_bytes(b'previous')
        for options in ({'partition_bytes': None}, {'partition_bytes': 8192},
                        {'partition_bytes': 4 << 20, 'slot_bytes': 1},
                        {'partition_bytes': 1 << 63}, {'layout_version': 3}):
            arguments = {'layout_version': 2, **options}
            with self.subTest(options=options), self.assertRaises(ValueError):
                builder.build_image(self.kernel, self.small, self.system, self.output, **arguments)
            self.assertEqual(self.output.read_bytes(), b'previous')
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_output_cannot_replace_input(self):
        self.output = self.kernel
        previous = self.kernel.read_bytes()
        with self.assertRaisesRegex(ValueError, 'replace an input'):
            self.build()
        self.assertEqual(self.kernel.read_bytes(), previous)

    def test_failed_publish_is_atomic(self):
        self.output.write_bytes(b'previous')
        with patch.object(os, 'replace', side_effect=OSError('simulated publish failure')):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(self.output.read_bytes(), b'previous')
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_truncation_during_copy(self):
        original = self.system.read_bytes()
        self.output.write_bytes(b'previous')
        open_file = Path.open
        count = 0

        def changing_open(path, *args, **kwargs):
            nonlocal count
            if path == self.system:
                count += 1
                if count == 2:
                    return io.BytesIO(original[:-1])
            return open_file(path, *args, **kwargs)

        with patch.object(Path, 'open', changing_open):
            with self.assertRaisesRegex(ValueError, 'truncated'):
                self.build()
        self.assertEqual(self.output.read_bytes(), b'previous')
        self.assertFalse(list(self.directory.glob('*.tmp')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
