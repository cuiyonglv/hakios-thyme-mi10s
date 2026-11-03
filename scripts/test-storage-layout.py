#!/usr/bin/env python3
"""Check that C consumers and Python interpret the same legacy storage contract."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from storage_layout import HEADER, LEGACY_LAYOUT, read_legacy_layout


class LayoutTests(unittest.TestCase):
    def test_c_and_python_agree(self):
        source = r'''
#include <stdio.h>
#include "thyme_storage_layout.h"
int main(void) {
    printf("{\"version\":%u,\"block_bytes\":%llu,\"store_at\":%llu,"
           "\"slot_bytes\":%llu,\"slots\":%u,\"store_end\":%llu}",
           THYME_LAYOUT_VERSION, THYME_STORAGE_BLOCK_SIZE, THYME_STORE_AT,
           THYME_STORE_SLOT_BYTES, THYME_STORE_SLOTS, THYME_STORE_END);
}
'''
        with tempfile.TemporaryDirectory(prefix='thyme-layout-') as temp:
            directory = Path(temp)
            cfile, binary = directory / 'layout.c', directory / 'layout'
            cfile.write_text(source)
            subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Werror',
                            '-I', str(HEADER.parent), str(cfile), '-o', str(binary)], check=True)
            result = json.loads(subprocess.check_output([str(binary)], text=True))
            self.assertEqual(result, LEGACY_LAYOUT)
            self.assertEqual(result['store_at'], 2503475200)
            self.assertEqual(result['store_end'], 3577217024)

    def test_invalid_or_ambiguous_definitions(self):
        original = HEADER.read_text()
        changes = [
            original.replace('THYME_LAYOUT_VERSION 1u', 'THYME_LAYOUT_VERSION 2u'),
            original.replace('THYME_STORAGE_BLOCK_SIZE 4096ULL', 'THYME_STORAGE_BLOCK_SIZE 512ULL'),
            original.replace('THYME_STORE_FIRST_BLOCK 611200ULL', 'THYME_STORE_FIRST_BLOCK 0ULL'),
            original.replace('THYME_STORE_SLOT_BYTES 536870912ULL', 'THYME_STORE_SLOT_BYTES 4097ULL'),
            original.replace('THYME_STORE_FIRST_BLOCK 611200ULL', 'THYME_STORE_FIRST_BLOCK 9223372036854775807ULL'),
            original + '\n#define THYME_STORE_FIRST_BLOCK (1+1)\n',
            original.replace('#define THYME_STORE_SLOTS 2u', ''),
        ]
        with tempfile.TemporaryDirectory(prefix='thyme-layout-invalid-') as temp:
            path = Path(temp) / 'layout.h'
            for number, source in enumerate(changes):
                with self.subTest(case=number):
                    path.write_text(source)
                    with self.assertRaises(ValueError):
                        read_legacy_layout(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
