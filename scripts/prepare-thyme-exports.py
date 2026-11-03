#!/usr/bin/env python3
"""Prepare the explicit Qualcomm thyme export contract; never edit the input.

This is not a general missing-symbol filter. It requires the pinned original
contract, the fixed platform exclusion list, and an actual linked symbol table
whose missing entries are exactly that list. All original checks still run.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_audit', ROOT / 'scripts/audit-kernel-exports.py')
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(source, symbols, out, target):
    if target != 'thyme':
        raise ValueError('this contract is only for the Qualcomm thyme target')
    source, symbols, out = map(lambda p: Path(p).resolve(), (source, symbols, out))
    profile_path = ROOT / 'tools/thyme-exports/profile.json'
    profile_data = profile_path.read_bytes()
    profile = json.loads(profile_data)
    if (profile['schema'] != 1 or profile['target'] != target or
            profile['xnu_commit'] != 'c54f35ca767986246321eb901baf8f5ff7923f6a'):
        raise ValueError('unexpected target profile')
    original = source.read_bytes()
    if sha(original) != profile['original_sha256']:
        raise ValueError('original export contract changed; review a new platform profile')
    excluded = [s for group in profile['unavailable_interfaces'].values() for s in group]
    if len(excluded) != 35 or len(set(excluded)) != 35:
        raise ValueError('expected the reviewed 35 distinct Apple-only interfaces')
    excluded = set(excluded)
    before = audit_module.audit(symbols, [source])
    if {item['target'] for item in before['missing']} != excluded:
        raise ValueError('actual missing interfaces differ from the reviewed platform contract')
    lines = original.decode().splitlines(keepends=True)
    selected = ('# Qualcomm thyme target: Apple SoC-only interfaces are unavailable.\n'
                '# Original contract and explicit exclusions are retained in the profile record.\n' +
                ''.join(line for line in lines if line.strip() not in excluded)).encode()
    if sum(line.strip() in excluded for line in lines) != 35:
        raise ValueError('unexpected original declarations')
    if sha(selected) != profile['selected_sha256']:
        raise ValueError('selected contract differs from the reviewed target hash')
    if out.exists() or any(p.is_relative_to(out) for p in (source, symbols, profile_path)):
        raise ValueError('output must be a new directory outside all inputs')
    out.mkdir(parents=True)
    try:
        selected_path = out / 'Private.arm64.exports'
        selected_path.write_bytes(selected)
        after = audit_module.audit(symbols, [selected_path])
        if after['missing_declarations'] or before['checked_declarations'] - after['checked_declarations'] != 35:
            raise ValueError('selected contract still has missing declarations')
        (out / 'Private.arm64.apple.exports').write_bytes(original)
        record = {'schema': 1, 'target': target, 'xnu_commit': profile['xnu_commit'],
                  'profile_sha256': sha(profile_data), 'original_sha256': sha(original),
                  'selected_sha256': sha(selected), 'symbols_sha256': sha(symbols.read_bytes()),
                  'unavailable_interfaces': profile['unavailable_interfaces'],
                  'before': before, 'after': after,
                  'limitation': 'No Apple IOMMU/DART/SART/UAT/NVMe-PPL ABI; not a phone acceptance result.'}
        (out / 'profile.json').write_text(json.dumps(record, indent=2) + '\n')
    except Exception:
        # This is the fresh directory created above, never an existing build tree.
        shutil.rmtree(out)
        raise
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--symbols', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--target', required=True, choices=['thyme'])
    args = parser.parse_args()
    try:
        record = prepare(args.source, args.symbols, args.out_dir, args.target)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, 'prepare-thyme-exports: ' + str(error) + '\n')
    print('Prepared explicit thyme contract; 35 Apple-only interfaces unavailable; '
          f"{record['after']['checked_declarations']} arm64 Private declarations retained")


if __name__ == '__main__':
    main()
