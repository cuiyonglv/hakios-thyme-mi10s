#!/usr/bin/env python3
"""Generate original XNU RPC code on macOS, without building or flashing a kernel.

Xcode 16 changed the IORPC structure ABI. Fail rather than silently mixing that
output with XNU 8796. Generated code still requires a separate compile/link audit.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

XNU_COMMIT = 'c54f35ca767986246321eb901baf8f5ff7923f6a'


def output(command, env=None):
    return subprocess.check_output(command, env=env, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: generate-apple-iig.py XNU_SOURCE NEW_OUTPUT_DIR')
    source = Path(sys.argv[1]).resolve()
    destination = Path(sys.argv[2]).resolve()
    if output(['git', '-C', str(source), 'rev-parse', 'HEAD']) != XNU_COMMIT:
        raise SystemExit('unexpected XNU source revision')
    if output(['git', '-C', str(source), 'status', '--porcelain']):
        raise SystemExit('XNU source must be clean')
    destination.mkdir(parents=True, exist_ok=False)
    record = {'schema': 1, 'xnu_commit': XNU_COMMIT, 'complete': False,
              'purpose': 'generated source only; no runtime or flash acceptance',
              'generations': []}
    manifest = destination / 'manifest.json'

    def save():
        manifest.write_text(json.dumps(record, indent=2) + '\n')

    save()
    candidates = []
    for app in Path('/Applications').glob('Xcode*.app'):
        env = dict(os.environ, DEVELOPER_DIR=str(app / 'Contents/Developer'))
        try:
            version = output(['xcodebuild', '-version'], env)
        except subprocess.CalledProcessError:
            continue
        match = re.search(r'^Xcode (15(?:\.\d+)*)$', version, re.M)
        if match:
            candidates.append((tuple(map(int, match[1].split('.'))), app, version))
    record['available_xcode_apps'] = sorted(str(p) for p in Path('/Applications').glob('Xcode*.app'))
    save()
    if not candidates:
        raise SystemExit('Xcode 15 required for legacy IORPC; inspect manifest for installed versions')
    _, app, version = min(candidates, key=lambda row: row[0])
    env = dict(os.environ, DEVELOPER_DIR=str(app / 'Contents/Developer'))
    iig = output(['xcrun', '--find', 'iig'], env)
    sdk = output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], env)
    record.update(xcode_version=version, iig_sha256=digest(Path(iig)),
                  sdk_version=output(['xcrun', '--sdk', 'macosx', '--show-sdk-version'], env))
    definitions = sorted((source / 'iokit/DriverKit').glob('*.iig'))
    if len(definitions) != 17:
        raise SystemExit('expected exactly 17 XNU 8796 definitions')
    flags = ['-I' + str(source / directory)
             for directory in ('iokit', 'osfmk', 'bsd', 'EXTERNAL_HEADERS')]
    flags += ['-isysroot', sdk, '-target', 'arm64-apple-macos13.0', '-x', 'c++',
              '-std=gnu++1z', '-D__IIG=1', '-DDRIVERKIT_PRIVATE=1', '-DPRIVATE_WIFI_ONLY=1']
    record['compiler_flags'] = flags
    for definition in definitions:
        header = destination / (definition.stem + '.h')
        implementation = destination / (definition.stem + '.iig.cpp')
        command = [iig, '--def', str(definition), '--header', str(header),
                   '--impl', str(implementation), '--framework-name', 'DriverKit', '--', *flags]
        result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        log = destination / (definition.stem + '.log')
        log.write_text(result.stdout)
        row = {'definition': str(definition.relative_to(source)),
               'input_sha256': digest(definition), 'exit_code': result.returncode,
               'outputs': {p.name: digest(p) for p in (header, implementation) if p.is_file()}}
        record['generations'].append(row)
        save()
        print(definition.name, result.returncode, flush=True)
        if result.returncode or len(row['outputs']) != 2:
            raise SystemExit('IIG generation failed; see ' + str(log))
        if 'kernelContent' in implementation.read_text():
            raise SystemExit('unexpected newer IORPC ABI in generated implementation')
    record['complete'] = True
    save()


if __name__ == '__main__':
    main()
