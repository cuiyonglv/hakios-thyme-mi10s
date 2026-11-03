#!/usr/bin/env python3
"""Report unresolved XNU export declarations without modifying build inputs.

Use the actual config/allsymbols produced by the failed build, plus the exact
*.exports files listed in that build's symbol-set command. A nonzero report is
not a repair; do not silence missing exports or generate fake success stubs.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def audit(symbol_path, export_paths):
    symbol_data = Path(symbol_path).read_bytes()
    defined = {line.strip() for line in symbol_data.decode().splitlines() if line.strip()}
    if not defined or any(not symbol.startswith('_') or any(c.isspace() for c in symbol)
                          for symbol in defined):
        raise ValueError('symbols must be nonempty output from nm -gj (one symbol per line)')
    missing = []
    export_files = []
    checked = 0
    for path in map(Path, export_paths):
        content = path.read_bytes()
        export_files.append({'name': path.name, 'sha256': hashlib.sha256(content).hexdigest()})
        for number, raw in enumerate(content.decode().splitlines(), 1):
            line = raw.partition('#')[0].strip()
            if not line:
                continue
            exported, separator, target = line.partition(':')
            exported = exported.strip()
            target = target.strip() if separator else exported
            if any(not symbol.startswith('_') or any(c.isspace() for c in symbol)
                   for symbol in (exported, target)):
                raise ValueError(f'{path.name}:{number}: unsupported export syntax')
            checked += 1
            if separator and any(c in exported + target for c in '*?['):
                raise ValueError(f'{path.name}:{number}: unsupported wildcard alias')
            if any(c in target for c in '?[') or ('*' in target and
                    (not target.endswith('*') or target.count('*') != 1)):
                raise ValueError(f'{path.name}:{number}: unsupported wildcard syntax')
            matched = (any(symbol.startswith(target[:-1]) for symbol in defined)
                       if target.endswith('*') else target in defined)
            if not matched:
                missing.append({'file': path.name, 'line': number,
                                'exported': exported, 'target': target})
    if not checked:
        raise ValueError('no export declarations were supplied')
    demangler = shutil.which('c++filt')
    if missing and demangler:
        result = subprocess.run([demangler], input='\n'.join(item['target'][1:] for item in missing),
                                text=True, capture_output=True, check=True, timeout=30)
        for item, name in zip(missing, result.stdout.splitlines()):
            item['demangled'] = name
            item['likely_driverkit_rpc'] = any(token in name for token in ('IORPC', '_Invoke', '_Dispatch'))
    return {'defined_symbols': len(defined), 'checked_declarations': checked,
            'symbol_file_sha256': hashlib.sha256(symbol_data).hexdigest(),
            'export_files': export_files,
            'missing_declarations': len(missing),
            'missing_by_file': dict(sorted(Counter(item['file'] for item in missing).items())),
            'missing': missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', type=Path, required=True)
    parser.add_argument('--exports', type=Path, required=True, nargs='+')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.symbols, args.exports)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + '\n')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f'audit-kernel-exports: {error}\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'missing'}))
    return 1 if report['missing'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
