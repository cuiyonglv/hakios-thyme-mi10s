#!/usr/bin/env python3
"""Check that C consumers and Python interpret the same legacy storage contract."""
import json
import os
import random
from pathlib import Path
import subprocess
import tempfile
import unittest
from storage_layout import HEADER, LEGACY_LAYOUT, read_legacy_layout, plan_v2_layout, valid_v2_layout


class LayoutTests(unittest.TestCase):
    def test_v2_c_python_boundaries(self):
        source = r'''
#include <stdio.h>
#include "thyme_storage_layout.h"
int main(void) {
    unsigned long long at,slot,flags,image,partition;
    unsigned count,block;
    while(scanf("%llu %llu %u %u %llu %llu %llu",&at,&slot,&count,&block,&flags,&image,&partition)==7){
        struct thyme_store_layout layout={at,slot,count,block,flags};
        printf("%d\n",thyme_store_layout_valid(&layout,image,partition));
    }
}
'''
        baseline = [12288, 65536, 2, 4096, 0, 9000, 4 << 20]
        cases = [baseline]
        for index in range(7):
            for value in (0, 1, 4095, 4096, 8192, 1 << 31, (1 << 32)-1):
                case = baseline.copy()
                case[index] = value
                cases.append(case)
        for index in (0, 1, 4, 5, 6):
            for value in ((1 << 63)-1, 1 << 63, (1 << 64)-1):
                case = baseline.copy()
                case[index] = value
                cases.append(case)
        rng = random.Random(10)
        for _ in range(200):
            image = rng.randrange(4096, 1 << 32)
            slot = rng.randrange(2, 10000) * 4096
            at = (image + 4095) // 4096 * 4096
            capacity = at + 2 * slot + rng.choice((-1, 0, 1, 4096))
            cases.append([at, slot, 2, 4096, 0, image, capacity])
        expected = [int(valid_v2_layout(dict(zip(('store_at','slot_bytes','slots','block_bytes','flags'),
                                                  case[:5])), case[5], case[6])) for case in cases]
        self.assertIn(0, expected)
        self.assertIn(1, expected)
        with tempfile.TemporaryDirectory(prefix='thyme-v2-layout-') as temp:
            directory = Path(temp)
            cfile, binary = directory / 'layout.c', directory / 'layout'
            cfile.write_text(source)
            subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Werror',
                            '-fsanitize=address,undefined', '-I', str(HEADER.parent),
                            str(cfile), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], input=''.join(' '.join(map(str,c))+'\n' for c in cases),
                                    capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(list(map(int, result.stdout.split())), expected)

    def test_v2_planning_and_explicit_capacity(self):
        image = LEGACY_LAYOUT['store_at'] + 8177904
        layout = plan_v2_layout(image, 4 << 30)
        self.assertGreaterEqual(layout['store_at'], image)
        self.assertLess(layout['store_at']-image, 4096)
        self.assertTrue(valid_v2_layout(layout, image, 4 << 30))
        for capacity in (None, 0, image, (1 << 63), True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                plan_v2_layout(image, capacity)

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
