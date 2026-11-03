#!/usr/bin/env python3
"""Verify captured file hashes, GPT CRCs/copies and partition-to-device mapping."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import zlib

REQUIRED = set('boot_a boot_b vendor_boot_a vendor_boot_b dtbo_a dtbo_b vbmeta_a vbmeta_b '
               'vbmeta_system_a vbmeta_system_b persist persistbak metadata modemst1 modemst2 fsg fsc'.split())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def gpt_header(data, offset, origin, total_bytes, sector, current, alternate):
    if offset < 0 or offset + 92 > len(data) or data[offset:offset+8] != b'EFI PART':
        raise ValueError('GPT header absent or outside captured range')
    revision, size, checksum, reserved = struct.unpack_from('<4I', data, offset+8)
    if revision != 0x10000 or not 92 <= size <= sector or offset + size > len(data) or reserved:
        raise ValueError('unsupported GPT header')
    header = bytearray(data[offset:offset+size])
    header[16:20] = b'\0' * 4
    if zlib.crc32(header) != checksum:
        raise ValueError('GPT header CRC mismatch')
    here, other, first, last = struct.unpack_from('<4Q', data, offset+24)
    entry_lba, count, entry_bytes, entry_crc = struct.unpack_from('<Q3I', data, offset+72)
    blocks = total_bytes // sector
    if here != current or other != alternate or not 1 < first <= last < blocks-1:
        raise ValueError('GPT geometry/header location mismatch')
    if not count or entry_bytes < 128 or entry_bytes % 8:
        raise ValueError('invalid GPT entry dimensions')
    at, length = entry_lba * sector - origin, count * entry_bytes
    if at < 0 or at + length > len(data):
        raise ValueError('GPT entry array is not fully captured')
    entries = data[at:at+length]
    if zlib.crc32(entries) != entry_crc:
        raise ValueError('GPT entry-array CRC mismatch')
    result = []
    for index in range(count):
        entry = entries[index*entry_bytes:(index+1)*entry_bytes]
        if entry[:16] == b'\0' * 16:
            continue
        begin, end, attributes = struct.unpack_from('<3Q', entry, 32)
        name = entry[56:128].decode('utf-16-le', errors='strict').split('\0', 1)[0]
        if not first <= begin <= end <= last or not name:
            raise ValueError('partition outside usable range or unnamed')
        result.append({'index': index+1, 'name': name, 'first_lba': begin, 'last_lba': end,
                       'bytes': (end-begin+1)*sector, 'attributes': attributes,
                       'type_guid_bytes': entry[:16].hex(), 'unique_guid_bytes': entry[16:32].hex()})
    ordered = sorted(result, key=lambda entry: entry['first_lba'])
    if any(a['last_lba'] >= b['first_lba'] for a, b in zip(ordered, ordered[1:])):
        raise ValueError('overlapping GPT partitions')
    return {'disk_guid_bytes': data[offset+56:offset+72].hex(), 'first_usable': first,
            'last_usable': last, 'entries': entries, 'partitions': result}


def verify_gpt(first, last, total_bytes, sector):
    if sector not in (512, 4096) or total_bytes < 2097152 or total_bytes % sector:
        raise ValueError('invalid LUN geometry')
    if len(first) != 1048576 or len(last) != 1048576:
        raise ValueError('both GPT capture buffers must be 1 MiB')
    blocks = total_bytes // sector
    primary = gpt_header(first, sector, 0, total_bytes, sector, 1, blocks-1)
    secondary = gpt_header(last, 1048576-sector, total_bytes-1048576,
                           total_bytes, sector, blocks-1, 1)
    if primary != secondary:
        raise ValueError('primary and secondary GPT disagree')
    primary.pop('entries')
    return primary


def verify(directory):
    directory = Path(directory).resolve()
    manifests = list(directory.rglob('files.tsv'))
    if len(manifests) != 1:
        raise ValueError('expected one completed backup manifest')
    base = manifests[0].parent
    if (base / 'status').read_text().strip() != 'complete' or (base / 'failures').read_text().strip() != '0':
        raise ValueError('phone did not report complete stable capture')
    files = {}
    for line in manifests[0].read_text().splitlines():
        name, size, digest, source = line.split('\t')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in files:
            raise ValueError('invalid or duplicate backup filename')
        path = base / name
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size != int(size) or sha(path) != digest:
            raise ValueError('backup file changed: ' + name)
        if not re.fullmatch('/dev/block/sd[a-z][0-9]*', source):
            raise ValueError('unexpected source device')
        files[name] = {'bytes': int(size), 'sha256': digest, 'source': source}
    if not {name + '.img' for name in REQUIRED} <= files.keys():
        raise ValueError('a required recovery partition is missing')
    disks = {}
    for line in (base / 'geometry.tsv').read_text().splitlines():
        name, size, sector = line.split('\t')
        if not re.fullmatch('sd[a-z]', name) or name in disks:
            raise ValueError('invalid or duplicate LUN')
        for suffix in ('first', 'last'):
            if files[name + '-gpt-' + suffix + '.bin']['source'] != '/dev/block/' + name:
                raise ValueError('GPT source device mismatch')
        disks[name] = verify_gpt((base / (name+'-gpt-first.bin')).read_bytes(),
                                 (base / (name+'-gpt-last.bin')).read_bytes(), int(size), int(sector))
        disks[name].update(bytes=int(size), sector_bytes=int(sector))
    if not disks:
        raise ValueError('no verified GPT geometry')
    for name, metadata in files.items():
        if not name.endswith('.img'):
            continue
        match = re.fullmatch('/dev/block/(sd[a-z])([0-9]+)', metadata['source'])
        if not match or match[1] not in disks:
            raise ValueError('partition has no verified GPT source')
        entries = [entry for entry in disks[match[1]]['partitions'] if entry['index'] == int(match[2])]
        if len(entries) != 1 or entries[0]['name'] != name[:-4] or entries[0]['bytes'] != metadata['bytes']:
            raise ValueError('partition name/size does not match GPT: ' + name)
    return {'schema': 1, 'verified': True, 'files': files, 'disks': disks,
            'captured_bytes': sum(file['bytes'] for file in files.values()),
            'limitation': 'Snapshot/hash/GPT checks; not a restore test or permission to flash.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        record = verify(args.directory)
        if args.out.exists():
            raise ValueError('verification output already exists')
        args.out.write_text(json.dumps(record, indent=2) + '\n')
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        parser.exit(1, 'verify-phone-backup: ' + str(error) + '\n')
    print(f"Verified {len(record['files'])} files, {len(record['disks'])} GPT pairs, {record['captured_bytes']} captured bytes")


if __name__ == '__main__':
    main()
