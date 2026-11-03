#!/usr/bin/env python3
"""Reject known incomplete IIG build inputs; absence of markers is not ABI proof."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def audit(generated, replacements):
    outputs = sorted(Path(generated).glob('*.iig.cpp'))
    substitutes = sorted(Path(replacements).glob('*.iig.cpp'))
    if not outputs or not substitutes:
        raise ValueError('expected both generated and replacement IIG source files')
    records = []
    for kind, paths in (('generated', outputs), ('replacement', substitutes)):
        for path in paths:
            data = path.read_bytes()
            source = data.decode()
            empty = 'no DriverKit implementation generated (iig shim)' in source
            stubs = re.findall(r'kern_return_t __iig_stub_\d+\(void\) __asm__\("([^"\n]+)"\);', source)
            branches = sum(line.startswith('__asm__(') and '.globl' in line
                           for line in source.splitlines())
            records.append({'kind': kind, 'name': path.name,
                            'sha256': hashlib.sha256(data).hexdigest(),
                            'empty_shim_output': empty,
                            'unsupported_stub_symbols': stubs,
                            'direct_branch_wrappers': branches,
                            'known_stand_in': 'stand-in for iig output' in source})
    blocked = any(r['empty_shim_output'] or r['unsupported_stub_symbols'] or
                  r['known_stand_in'] or r['direct_branch_wrappers'] for r in records)
    return {'schema': 1, 'known_incomplete_inputs': blocked, 'files': records,
            'limitation': 'Known-marker audit only; no completeness, ABI or runtime certification.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generated-dir', type=Path, required=True)
    parser.add_argument('--replacement-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.generated_dir, args.replacement_dir)
        args.out.write_text(json.dumps(report, indent=2) + '\n')
    except (OSError, ValueError) as error:
        parser.exit(2, f'audit-iig-inputs: {error}\n')
    print('BLOCKED: known incomplete IIG inputs' if report['known_incomplete_inputs']
          else 'No known markers detected; manual ABI and completeness review still required')
    return 1 if report['known_incomplete_inputs'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
