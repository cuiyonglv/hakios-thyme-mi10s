#!/usr/bin/env python3
"""Build guarded init and var_store using pinned external libmini inputs.

Produces a new local directory atomically; never edits the donor checkout or phone.
The record is for reproducibility and does not certify a bootable image.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MINI_SHA256 = {
    'mini.h': '7f633455ab782794b11e2daa04480fe27bd8d8eb4f470e1e7bcbfc77246f09cb',
    'mini.c': '4aafe4461e79a503336fc149c263d96fef7d5e9d28eaf7375ce968f838c4aa68',
    'start.S': '75024af4f60a89ff82269a7076be303bcb50779f9f1d4ef504231bcf4ea1012c',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', type=Path, required=True)
    parser.add_argument('--mini-dir', type=Path, required=True)
    parser.add_argument('--clang', default='clang-18')
    parser.add_argument('--ld64', required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.out_dir.exists():
            raise ValueError('output directory already exists; choose a new build directory')
        spec = importlib.util.spec_from_file_location('prepare_init', ROOT / 'scripts/prepare-init.py')
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        inputs = {'external/init.c': args.init,
                  **{f'external/{name}': args.mini_dir / name for name in ('mini.h', 'mini.c', 'start.S')},
                  'port/var_store.c': ROOT / 'src/userland/var_store.c',
                  'port/restore_guard.inc': ROOT / 'src/userland/restore_guard.inc',
                  'port/thyme_storage_layout.h': ROOT / 'src/kernel/bsd/sys/thyme_storage_layout.h',
                  'port/prepare-init.py': ROOT / 'scripts/prepare-init.py',
                  'port/build-guarded-userland.py': Path(__file__).resolve()}
        before = {name: digest(path) for name, path in inputs.items()}
        for name, expected in MINI_SHA256.items():
            if before[f'external/{name}'] != expected:
                raise ValueError(f'unrecognized external libmini input: {name}')
        if before['external/init.c'] != prepare.SOURCE_SHA256:
            raise ValueError('unrecognized external init input')
        generated = prepare.prepare(args.init.read_text(), (ROOT / 'src/userland/restore_guard.inc').read_text())
        args.out_dir.parent.mkdir(parents=True, exist_ok=True)
        flags = ['--target=arm64-apple-ios16.5', '-Os', '-ffreestanding',
                 '-fno-stack-protector', '-fno-builtin', '-Wall', '-Wextra']
        with tempfile.TemporaryDirectory(prefix='guarded-userland-', dir=args.out_dir.parent) as temp:
            staging = Path(temp)
            (staging / 'init.c').write_text(generated)
            sources = {'mini': args.mini_dir / 'mini.c', 'start': args.mini_dir / 'start.S',
                       'init': staging / 'init.c', 'var_store': ROOT / 'src/userland/var_store.c'}
            for name, path in sources.items():
                subprocess.run([args.clang, *flags, '-I', str(args.mini_dir.resolve()),
                                '-c', str(path.resolve()), '-o', str(staging / (name + '.o'))], check=True)
            for name in ('init', 'var_store'):
                subprocess.run([args.ld64, '-arch', 'arm64', '-platform_version', 'ios', '16.5', '16.5',
                                '-static', '-e', '_start', '-pagezero_size', '0x100000000',
                                '-o', str(staging / name), str(staging / (name + '.o')),
                                str(staging / 'mini.o'), str(staging / 'start.o')], check=True)
            if before != {name: digest(path) for name, path in inputs.items()}:
                raise ValueError('build inputs changed during compilation')
            revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                      text=True, capture_output=True, check=True).stdout.strip()
            dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'],
                                   text=True, capture_output=True, check=True).stdout.strip()
            record = {'schema': 1, 'revision': revision, 'tracked_source_dirty': bool(dirty),
                      'target': 'arm64-apple-ios16.5', 'input_sha256': before,
                      'compiler_flags': flags,
                      'clang': subprocess.check_output([args.clang, '--version'], text=True).splitlines()[0],
                      'artifact_sha256': {name: digest(staging / name) for name in ('init', 'var_store', 'init.c')},
                      'limitation': 'Local build only; not a packaged or device-validated installation.'}
            (staging / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
            os.rename(staging, args.out_dir)
        print('Built guarded init and var_store; inspect build.json before image integration')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'build-guarded-userland: {error}\n')


if __name__ == '__main__':
    main()
