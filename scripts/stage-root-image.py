#!/usr/bin/env python3
"""Build a private root APFS with matching guarded init/saver, then verify readback.

Never modifies the template or donor binaries. No device, partition or flash access.
Requires already prepared external APFS validators; output is still experimental.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_userland(directory):
    record = json.loads((directory / 'build.json').read_text())
    if (record.get('schema') != 1 or record.get('tracked_source_dirty') is not False or
            record.get('target') != 'arm64-apple-ios16.5' or
            not re.fullmatch('[0-9a-f]{40}', record.get('revision', ''))):
        raise ValueError('userland build is not a clean recorded arm64 build')
    port = {
        'port/var_store.c': ROOT / 'src/userland/var_store.c',
        'port/restore_guard.inc': ROOT / 'src/userland/restore_guard.inc',
        'port/thyme_storage_layout.h': ROOT / 'src/kernel/bsd/sys/thyme_storage_layout.h',
        'port/prepare-init.py': ROOT / 'scripts/prepare-init.py',
        'port/build-guarded-userland.py': ROOT / 'scripts/build-guarded-userland.py',
    }
    prepare = load_module('pinned_init', ROOT / 'scripts/prepare-init.py')
    builder = load_module('pinned_builder', ROOT / 'scripts/build-guarded-userland.py')
    expected = {name: digest(path) for name, path in port.items()}
    expected.update({'external/init.c': prepare.SOURCE_SHA256,
                     **{f'external/{name}': value for name, value in builder.MINI_SHA256.items()}})
    actual = record.get('input_sha256', {})
    if actual != expected:
        changed = sorted(name for name in set(actual) | set(expected) if actual.get(name) != expected.get(name))
        raise ValueError('userland input mismatch: ' + ', '.join(changed))
    artifacts = record.get('artifact_sha256', {})
    if set(artifacts) != {'init', 'var_store', 'init.c'}:
        raise ValueError('unexpected userland artifact list')
    for name, expected_hash in artifacts.items():
        if digest(directory / name) != expected_hash:
            raise ValueError(f'userland artifact changed: {name}')
    for name in ('init', 'var_store'):
        data = (directory / name).read_bytes()
        if len(data) < 32:
            raise ValueError('truncated userland executable')
        header = struct.unpack_from('<8I', data)
        if header[0] != 0xfeedfacf or header[1] != 0x100000c or header[3] != 2 or not header[6] & 1:
            raise ValueError('userland artifact is not an arm64 executable with defined symbols')
    return record


def inventory(directory):
    rows = []
    for path in sorted(directory.rglob('*')):
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        relative = path.relative_to(directory).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            rows.append((relative, mode, 'link', os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            rows.append((relative, mode, 'file', digest(path)))
        elif stat.S_ISDIR(metadata.st_mode):
            rows.append((relative, mode, 'dir', ''))
        else:
            raise ValueError('template contains unsupported special files')
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--userland-build', type=Path, required=True)
    parser.add_argument('--apfs-tools', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        template = args.template.resolve()
        if args.out_dir.exists() or args.out_dir.is_symlink():
            raise ValueError('output directory exists; choose a new directory')
        if template == args.out_dir.resolve() or template in args.out_dir.resolve().parents:
            raise ValueError('output must not be inside the source template')
        for name in ('sbin', 'bin', 'LaunchDaemons'):
            if not stat.S_ISDIR((template / name).lstat().st_mode):
                raise ValueError(f'template {name} must be a real directory')
        plist_path = template / 'LaunchDaemons/com.hakios.varstore.plist'
        plist = plistlib.loads(plist_path.read_bytes())
        if (plist.get('ProgramArguments') != ['/redwood/bin/var_store', 'watch'] or
                plist.get('RunAtLoad') is not True or plist.get('KeepAlive')):
            raise ValueError('unexpected autosaver launchd job')
        userland = checked_userland(args.userland_build)
        before = inventory(template)
        tool_root = args.apfs_tools.resolve().parents[1]
        reader = tool_root / 'apfs-work/apfs-tool'
        apfsck = tool_root / 'xnu-deps/build/apfsprogs/apfsck/apfsck'
        if not reader.is_file() or not apfsck.is_file():
            raise ValueError('prepare APFS reader and apfsck before staging')
        args.out_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='root-image-', dir=args.out_dir.parent) as temp:
            staging = Path(temp)
            tree = staging / 'root'
            # These are real copies: no subsequent write can truncate a donor hard link.
            shutil.copytree(template, tree, symlinks=True)
            for name, relative in (('init', 'sbin/init'), ('var_store', 'bin/var_store')):
                destination = tree / relative
                with tempfile.NamedTemporaryFile(dir=staging, delete=False) as stream:
                    replacement = Path(stream.name)
                    stream.write((args.userland_build / name).read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                replacement.chmod(0o755)
                os.replace(replacement, destination)
            image = staging / 'root.apfs'
            subprocess.run(['python3', str(args.apfs_tools / 'mkapfs.py'), '-z', '--plain', 'fw',
                            '-o', str(image), f'redwood={tree}'], check=True)
            checked = subprocess.run(['bash', str(args.apfs_tools / 'check-image.sh'),
                                      str(image), f'redwood={tree}'], capture_output=True, text=True)
            (staging / 'verification.log').write_text(checked.stdout + checked.stderr)
            if checked.returncode:
                raise ValueError('independent APFS validation failed: ' + checked.stderr[-600:])
            for name, relative in (('init', '/sbin/init'), ('var_store', '/bin/var_store')):
                restored = subprocess.check_output([str(reader), 'cat', str(image), relative])
                if hashlib.sha256(restored).hexdigest() != userland['artifact_sha256'][name]:
                    raise ValueError('APFS executable readback differs from the checked build')
            if before != inventory(template):
                raise ValueError('template changed during image construction')
            checked_userland(args.userland_build)
            record = {
                'schema': 1, 'status': 'experimental root image; no phone or kernel acceptance',
                'userland_revision': userland['revision'], 'userland_record_sha256': digest(args.userland_build / 'build.json'),
                'template_tree_sha256': before, 'image_sha256': digest(image),
                'image_bytes': image.stat().st_size,
                'program_sha256': {name: userland['artifact_sha256'][name] for name in ('init', 'var_store')},
                'reader_sha256': digest(reader), 'apfsck_sha256': digest(apfsck),
                'mkapfs_sha256': digest(args.apfs_tools / 'mkapfs.py'),
                'stage_script_sha256': digest(Path(__file__).resolve()),
                'validation': 'apfsck and independent reader census; executable SHA-256 readback',
            }
            (staging / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
            os.rename(staging, args.out_dir)
        print('Built and verified private root APFS; device and full-kernel gates remain incomplete')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'stage-root-image: {error}\n')


if __name__ == '__main__':
    main()
