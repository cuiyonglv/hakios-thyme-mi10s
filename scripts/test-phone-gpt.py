#!/usr/bin/env python3
"""Synthetic independent GPT pairs: validate CRCs and bind named partition copies."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import zlib

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify', root / 'scripts/verify-phone-backup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def pair(sector, names):
    total = 16 * 1024 * 1024
    blocks = total // sector
    first_usable = 1048576 // sector
    last_usable = blocks - first_usable - 1
    entries = bytearray(128 * 128)
    for index, name in enumerate(names):
        at = index * 128
        entries[at:at+16] = b'T' * 16
        entries[at+16:at+32] = bytes([index+1]) * 16
        start = first_usable + index * (4096 // sector)
        struct.pack_into('<3Q', entries, at+32, start, start+4096//sector-1, 0)
        encoded = name.encode('utf-16-le')
        entries[at+56:at+56+len(encoded)] = encoded

    def header(here, other, table_lba):
        data = bytearray(92)
        data[:8] = b'EFI PART'
        struct.pack_into('<4I4Q', data, 8, 0x10000, 92, 0, 0, here, other, first_usable, last_usable)
        data[56:72] = b'0123456789abcdef'
        struct.pack_into('<Q3I', data, 72, table_lba, 128, 128, zlib.crc32(entries))
        struct.pack_into('<I', data, 16, zlib.crc32(data))
        return data

    primary, secondary = bytearray(1048576), bytearray(1048576)
    primary[510:512] = b'\x55\xaa'
    primary[sector:sector+92] = header(1, blocks-1, 2)
    primary[2*sector:2*sector+len(entries)] = entries
    table_lba = blocks-1-len(entries)//sector
    at = table_lba * sector - (total-1048576)
    secondary[at:at+len(entries)] = entries
    secondary[-sector:-sector+92] = header(blocks-1, 1, table_lba)
    return bytes(primary), bytes(secondary), total


def reject(operation):
    try:
        operation()
    except (ValueError, OSError):
        return
    raise AssertionError('damaged backup accepted')


for sector in (512, 4096):
    names = sorted(module.REQUIRED)
    primary, secondary, total = pair(sector, names)
    parsed = module.verify_gpt(primary, secondary, total, sector)
    assert [entry['name'] for entry in parsed['partitions']] == names
    for offset in (sector+40, 2*sector+60):
        damaged = bytearray(primary)
        damaged[offset] ^= 1
        reject(lambda: module.verify_gpt(damaged, secondary, total, sector))
    damaged = bytearray(secondary)
    damaged[-sector+16] ^= 1
    reject(lambda: module.verify_gpt(primary, damaged, total, sector))
    reject(lambda: module.verify_gpt(primary, secondary, total+sector, sector))
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        rows = []
        for index, name in enumerate(names):
            data = name.encode().ljust(4096, b'\0')
            path = work / (name+'.img')
            path.write_bytes(data)
            rows.append(f'{path.name}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\t/dev/block/sda{index+1}')
        for suffix, data in [('first', primary), ('last', secondary)]:
            name = 'sda-gpt-' + suffix + '.bin'
            (work / name).write_bytes(data)
            rows.append(f'{name}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\t/dev/block/sda')
        (work / 'files.tsv').write_text('\n'.join(rows)+'\n')
        (work / 'geometry.tsv').write_text(f'sda\t{total}\t{sector}\n')
        (work / 'status').write_text('complete\n')
        (work / 'failures').write_text('0\n')
        record = module.verify(work)
        assert record['verified'] and len(record['files']) == len(names)+2
        first = work / (names[0]+'.img')
        first.write_bytes(b'changed')
        reject(lambda: module.verify(work))
        first.write_bytes(names[0].encode().ljust(4096,b'\0'))
        lines = (work / 'files.tsv').read_text().splitlines()
        lines[0] = lines[0].rsplit('\t',1)[0] + '\t/dev/block/sda2'
        (work / 'files.tsv').write_text('\n'.join(lines)+'\n')
        reject(lambda: module.verify(work))
print('Phone GPT: 512/4096-byte sectors, both CRCs/copies, file hashes and source mapping checked')
