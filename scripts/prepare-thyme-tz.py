#!/usr/bin/env python3
"""Prepare the exact thyme TzDxe revision for a temporary UEFI RAM boot."""
import argparse
import hashlib
from pathlib import Path
import struct

SOURCE_SHA256 = 'dfc36c4c0ee54d251b9bb9cae1a5782526305d815af7ce23b3ca9b3406e57601'
OUTPUT_SHA256 = '3494282f851483f85bff6b8a173bfcb789cb976aaeb490531ba6e4423d24d0e9'
FLAG_OFFSET = 0xd469


def prepare(data):
    if len(data) != 61440 or hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        raise ValueError('unrecognized thyme TzDxe revision; no patch applied')
    # This byte is the app-loaded state checked by both callers of the load path.
    # Check the actual instruction sites as well as the complete binary identity.
    if data[0x2e28:0x2e34] != bytes.fromhex('e9a65139ff0700b989020034'):
        raise ValueError('first app-loaded state check changed')
    if data[0x32d4:0x32dc] != bytes.fromhex('560000d0c9a65139'):
        raise ValueError('second app-loaded state check changed')
    if data[FLAG_OFFSET] != 0:
        raise ValueError('app-loaded state is not the expected original value')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0' or struct.unpack_from('<H', data, pe+4)[0] != 0xaa64:
        raise ValueError('expected ARM64 PE driver')
    result = bytearray(data)
    result[FLAG_OFFSET] = 1
    if hashlib.sha256(result).hexdigest() != OUTPUT_SHA256:
        raise ValueError('patched result differs from the reviewed firmware profile')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.input.read_bytes())
        # Never overwrite either an existing artifact or the original module.
        with args.output.open('xb') as stream:
            stream.write(result)
    except (OSError, ValueError, struct.error) as error:
        parser.exit(1, 'prepare-thyme-tz: ' + str(error) + '\n')
    print('Prepared reviewed thyme TzDxe RAM-boot module:', OUTPUT_SHA256)


if __name__ == '__main__':
    main()
