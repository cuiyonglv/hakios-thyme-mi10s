#!/usr/bin/env python3
"""Build a local image; v1 is default, v2 is an experimental on-disk format.

KERNEL, SMALL, SYSTEM, OUT and CMDLINE retain the original environment API.
Neither format is an installation plan. V2 requires explicit partition capacity
and is currently rejected by the loader pending runtime integration.
"""
import hashlib
import os
from pathlib import Path
import struct
import tempfile
from storage_layout import LEGACY_LAYOUT, plan_v2_layout

HEADER = 4096
MAGIC = b'IOSSTOR1'
PAYLOAD_MAX = 0x04000000
STORE_AT = LEGACY_LAYOUT['store_at']
BOOT_LINE_LENGTH = 608
CHUNK = 1024 * 1024
DEFAULT_CMDLINE = (
    'debug=0x8 cpus=7 bootcpu=7 adreno=21 adreno_keep=1 touchscreen=1 gl=1 cpufreq=1 '
    'fb_scan=1 fb_wcomb=1 pon=8 pon_power_bit=1 pon_vol_bit=2 buttons=1 wifi=1 '
    'wifi_mac=02:00:00:00:00:01 loader_return_after=120 ufs=2 ufs_write=0 var_store=0'
)


def entry(name, body):
    encoded = name.encode('ascii')
    if not encoded or len(encoded) >= 16 or len(body) > 0xffffffff:
        raise ValueError('invalid XNUP entry')
    return struct.pack('<16sII', encoded, len(body), 0) + body + b'\0' * (-len(body) % 16)


def check_apfs(path):
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(48)
    if len(header) != 48 or header[32:36] != b'NXSB':
        raise ValueError(f'{path.name}: not an APFS container')
    block_size, blocks = struct.unpack_from('<IQ', header, 36)
    if block_size != 4096 or blocks == 0 or blocks * block_size != size:
        raise ValueError(f'{path.name}: truncated or inconsistent APFS container')
    return size


def build_image(kernel_path, small_path, system_path, output_path, cmdline=DEFAULT_CMDLINE,
                *, layout_version=1, partition_bytes=None, slot_bytes=LEGACY_LAYOUT['slot_bytes']):
    kernel_path, small_path, system_path, output_path = map(
        Path, (kernel_path, small_path, system_path, output_path))
    if output_path.resolve() in {p.resolve() for p in (kernel_path, small_path, system_path)}:
        raise ValueError('output must not replace an input')
    if not cmdline or any(c in cmdline for c in ('\0', '\n', '\r')):
        raise ValueError('CMDLINE must be one nonempty line without NUL')
    command = cmdline.encode('ascii') + b'\0'
    if len(command) > BOOT_LINE_LENGTH:
        raise ValueError('CMDLINE exceeds the kernel boot-argument limit')
    kernel_size = kernel_path.stat().st_size
    small_size = check_apfs(small_path)
    system_size = check_apfs(system_path)
    if kernel_size < 32 or kernel_size + small_size > PAYLOAD_MAX:
        raise ValueError('kernel and small image exceed the payload window')
    kernel = kernel_path.read_bytes()
    magic, cpu, _, kind = struct.unpack_from('<IIII', kernel)
    if magic != 0xfeedfacf or cpu != 0x0100000c or kind != 2:
        raise ValueError('kernel must be an arm64 Mach-O executable')
    body = (entry('kernel', kernel) + entry('ramdisk', small_path.read_bytes()) +
            entry('cmdline', command))
    payload = struct.pack('<II', 0x50554e58, len(body)) + body
    if len(payload) > PAYLOAD_MAX:
        raise ValueError('XNUP payload exceeds the reserved RAM window')
    image_size = HEADER + len(payload) + system_size
    if type(layout_version) is not int or layout_version not in (1, 2):
        raise ValueError('unsupported layout version')
    if layout_version == 1 and image_size > STORE_AT:
        raise ValueError(f'image overlaps persistence slots by {image_size - STORE_AT} bytes; '
                         'rebuild smaller images or explicitly redesign the shared layout')
    header = bytearray(HEADER)
    layout = None
    if layout_version == 2:
        layout = plan_v2_layout(image_size, partition_bytes, slot_bytes)
    header[:8] = MAGIC if layout_version == 1 else b'IOSSTOR2'
    struct.pack_into('<IIQQQQQQ', header, 8, layout_version, 0, len(payload), system_size,
                     system_size, 0, 0, 0)
    if layout is not None:
        struct.pack_into('<QQIIQ', header, 64, layout['store_at'], layout['slot_bytes'],
                         layout['slots'], layout['block_bytes'], layout['flags'])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=output_path.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(header)
            stream.write(payload)
            digest = hashlib.sha256()
            if layout_version == 2:
                digest.update(header[:96])
            digest.update(payload)
            copied = 0
            with system_path.open('rb') as system:
                while chunk := system.read(CHUNK):
                    copied += len(chunk)
                    if copied > system_size:
                        raise ValueError('system image grew while copying')
                    stream.write(chunk)
                    digest.update(chunk)
            if copied != system_size:
                raise ValueError('system image changed or was truncated while copying')
            header[96:128] = digest.digest()
            stream.seek(0)
            stream.write(header)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    result = {'payload_bytes': len(payload), 'system_bytes': system_size,
              'image_bytes': image_size, 'layout_version': layout_version,
              ('sha256_payload_system' if layout_version == 1 else
               'sha256_header_payload_system'): digest.hexdigest()}
    if layout is not None:
        result['storage_layout'] = layout
        result['digest_scope'] = 'header[0:96] + payload + system'
        result['integration_status'] = 'experimental format; runtime integration and migration not complete'
    return result


def main():
    base = Path(os.environ.get('IOS', str(Path.home() / 'hackios'))) / 'thyme'
    result = build_image(os.environ.get('KERNEL', base / 'kernel-stage.bin'),
                         os.environ.get('SMALL', base / 'root.apfs'),
                         os.environ.get('SYSTEM', base / 'system.apfs'),
                         os.environ.get('OUT', base / 'ios-storage/ios.img'),
                         os.environ.get('CMDLINE', DEFAULT_CMDLINE),
                         layout_version=int(os.environ.get('LAYOUT_VERSION', '1')),
                         partition_bytes=(int(os.environ['PARTITION_BYTES'], 0)
                                          if 'PARTITION_BYTES' in os.environ else None))
    for key, value in result.items():
        print(f'{key}: {value}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(f'build-image: {error}')
