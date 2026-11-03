#!/usr/bin/env python3
"""Check a read-only loader artifact against its recorded inputs.

This is an offline consistency gate, not authorization to boot/flash a phone.
It does not authenticate a publisher or verify firmware/kernel behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

SOURCES = {'loader.c', 'efi.h', 'sha256.c', 'sha256.h', 'build.sh',
           '../kernel/bsd/sys/thyme_storage_layout.h'}


def verify(artifact, manifest, source_dir, identity, revision):
    problems = []
    data = Path(artifact).read_bytes()
    record = json.loads(Path(manifest).read_text())
    if record.get('schema') != 1:
        problems.append('unsupported build record')
    if record.get('revision') != revision or not re.fullmatch('[0-9a-f]{40}', revision):
        problems.append('unexpected source revision')
    if record.get('tracked_source_dirty') is not False:
        problems.append('build did not record a clean source checkout')
    if record.get('direct_storage_writes') is not False:
        problems.append('loader direct storage writes are not disabled')
    if record.get('target') != 'aarch64-unknown-windows' or record.get('image_base') != '0xA1000000':
        problems.append('unexpected build target or image base')
    summary = record.get('artifact', {})
    if (summary.get('bytes') != len(data) or
            summary.get('sha256') != hashlib.sha256(data).hexdigest()):
        problems.append('artifact bytes differ from the build record')
    sources = record.get('sources', {})
    if set(sources) != SOURCES:
        problems.append('unexpected source-input list')
    else:
        for name in sorted(SOURCES):
            if hashlib.sha256((Path(source_dir) / name).read_bytes()).hexdigest() != sources[name]:
                problems.append(f'source changed after build: {name}')
    if hashlib.sha256(Path(identity).read_bytes()).hexdigest() != record.get('identity_sha256'):
        problems.append('device-identity input differs from the recorded input')
    try:
        if data[:2] != b'MZ':
            raise ValueError
        offset = struct.unpack_from('<I', data, 0x3c)[0]
        if data[offset:offset + 4] != b'PE\0\0':
            raise ValueError
        optional = offset + 24
        if (struct.unpack_from('<H', data, offset + 4)[0] != 0xaa64 or
                struct.unpack_from('<H', data, optional)[0] != 0x20b or
                struct.unpack_from('<Q', data, optional + 24)[0] != 0xa1000000 or
                struct.unpack_from('<H', data, optional + 68)[0] != 10):
            raise ValueError
    except (ValueError, struct.error):
        problems.append('artifact is not an arm64 PE32+ EFI application at the expected base')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--source-dir', type=Path, required=True)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    try:
        problems = verify(args.artifact, args.manifest, args.source_dir, args.identity, args.revision)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.exit(2, f'verify-loader-build: invalid or unavailable inputs: {error}\n')
    for problem in problems:
        print('BLOCKED: ' + problem)
    if problems:
        return 1
    print('PASS: read-only loader record and inputs match; phone validation is still required')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
