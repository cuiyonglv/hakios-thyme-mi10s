#!/usr/bin/env python3
"""Supply checked Apple IIG output to an otherwise Linux XNU build.

Accept the original IIG command line. Every definition and both cached outputs
must match the generation record. No empty translation units or stub fallback.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def sha(data):
    return hashlib.sha256(data).hexdigest()


def generate(definition, header, implementation, cache):
    cache = Path(cache).resolve()
    record = json.loads((cache / 'manifest.json').read_text())
    if (record.get('schema') != 1 or record.get('complete') is not True or
            record.get('xnu_commit') != 'c54f35ca767986246321eb901baf8f5ff7923f6a' or
            not re.match(r'^Xcode 15(?:\.|\n)', record.get('xcode_version', ''))):
        raise ValueError('unrecognized or incomplete Apple generation record')
    rows = record.get('generations', [])
    if len(rows) != 17 or len({r['definition'] for r in rows}) != 17:
        raise ValueError('expected 17 distinct generation records')
    definition = Path(definition).resolve()
    # Definitions include each other. A changed OSObject.iig also invalidates
    # IOService output, even when IOService.iig itself has not changed.
    for dependency in rows:
        relative = Path(dependency['definition'])
        if relative.parent.as_posix() != 'iokit/DriverKit' or relative.suffix != '.iig':
            raise ValueError('unexpected recorded definition path')
        if sha((definition.parent / relative.name).read_bytes()) != dependency['input_sha256']:
            raise ValueError('definition set changed; native regeneration required')
    key = 'iokit/DriverKit/' + definition.name
    matches = [row for row in rows if row['definition'] == key]
    if len(matches) != 1:
        raise ValueError('definition is not in the recorded XNU source set')
    row = matches[0]
    if row.get('exit_code') != 0 or sha(definition.read_bytes()) != row['input_sha256']:
        raise ValueError('definition changed; regenerate with native Apple IIG')
    names = [definition.stem + '.h', definition.stem + '.iig.cpp']
    if set(row['outputs']) != set(names):
        raise ValueError('unexpected cached output names')
    data = [(cache / name).read_bytes() for name in names]
    for name, content in zip(names, data):
        if not content or sha(content) != row['outputs'][name]:
            raise ValueError('cached source hash mismatch: ' + name)
    if b'kernelContent' in data[1]:
        raise ValueError('newer IORPC ABI is incompatible with XNU 8796')
    targets = [Path(header).resolve(), Path(implementation).resolve()]
    if len(set(targets)) != 2 or any(p == definition or p.is_relative_to(cache) for p in targets):
        raise ValueError('outputs must be distinct and outside source/cache inputs')
    # Validate everything before replacing either output. Each replacement is
    # atomic; make stops on any failure and cannot compile a truncated file.
    for target, content in zip(targets, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = stream.name
                stream.write(content)
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--def', dest='definition', required=True)
    parser.add_argument('--header', required=True)
    parser.add_argument('--impl', required=True)
    parser.add_argument('--framework-name', default='DriverKit')
    parser.add_argument('--cache', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'tools/apple-iig-8796')
    args, _ = parser.parse_known_args()
    if args.framework_name != 'DriverKit':
        parser.error('only the recorded DriverKit framework is supported')
    try:
        generate(args.definition, args.header, args.impl, args.cache)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, 'cached-apple-iig: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
