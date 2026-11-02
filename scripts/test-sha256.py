#!/usr/bin/env python3
"""Compare the loader's streaming SHA-256 implementation with hashlib."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
harness = r'''
#include <stdio.h>
#include "sha256.h"
int main(void) {
    SHA256_CTX hash;
    unsigned char buffer[137], digest[32];
    size_t length;
    sha256_init(&hash);
    while ((length = fread(buffer, 1, sizeof(buffer), stdin)) != 0)
        sha256_update(&hash, buffer, length);
    if (ferror(stdin)) return 1;
    sha256_final(&hash, digest);
    for (unsigned i = 0; i < sizeof(digest); ++i) printf("%02x", digest[i]);
    return 0;
}
'''
vectors = [b'', b'abc', b'a' * 1000000]
vectors += [bytes(range(256))[:n] for n in (1, 55, 56, 63, 64, 65)]
vectors += [bytes(range(256)) * 5000]
with tempfile.TemporaryDirectory(prefix='thyme-sha256-') as temporary:
    directory = Path(temporary)
    cfile, binary = directory / 'digest.c', directory / 'digest'
    cfile.write_text(harness)
    subprocess.run([os.environ.get('CC', 'cc'), '-O2', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', '-I', str(root / 'src/loader'),
                    str(cfile), str(root / 'src/loader/sha256.c'), '-o', str(binary)], check=True)
    for data in vectors:
        result = subprocess.run([str(binary)], input=data, capture_output=True, check=True, timeout=20)
        if result.stdout.decode() != hashlib.sha256(data).hexdigest():
            raise SystemExit(f'SHA-256 mismatch for {len(data)} bytes')
print(f'PASS: {len(vectors)} streaming SHA-256 vectors match hashlib with sanitizers')
