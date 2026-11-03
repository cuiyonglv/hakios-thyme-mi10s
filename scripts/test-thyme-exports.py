#!/usr/bin/env python3
"""Check platform selection without silently hiding any other missing export."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare', root / 'scripts/prepare-thyme-exports.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
source = root / 'tools/thyme-exports/Private.arm64.apple.exports'
profile = json.loads((source.parent / 'profile.json').read_text())
original = source.read_bytes()
assert hashlib.sha256(original).hexdigest() == profile['original_sha256']
excluded = {s for group in profile['unavailable_interfaces'].values() for s in group}
symbols = set()
for line in original.decode().splitlines():
    target = line.partition(':')[2] or line
    if target not in excluded:
        symbols.add(target.replace('*', 'ProfileTest'))

with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    table = work / 'symbols'
    table.write_text('\n'.join(sorted(symbols)) + '\n')
    record = module.prepare(source, table, work / 'valid', 'thyme')
    selected = (work / 'valid/Private.arm64.exports').read_bytes()
    assert record['before']['missing_declarations'] == 35
    assert record['after']['checked_declarations'] == 91
    assert record['after']['missing_declarations'] == 0
    assert hashlib.sha256(selected).hexdigest() == profile['selected_sha256']
    assert b'_ml_mcache_flush_callback_register\n' in selected
    assert b'_mach_vm_map:_mach_vm_map_external\n' in selected
    assert (work / 'valid/Private.arm64.apple.exports').read_bytes() == original

    def reject(candidate_source=source, target='thyme', output=None):
        output = output or work / 'rejected'
        try:
            module.prepare(candidate_source, table, output, target)
        except (ValueError, OSError):
            assert not output.exists() or output.name == 'valid'
        else:
            raise AssertionError('invalid profile inputs accepted')

    reject(target='sweet')
    reject(output=work / 'valid')
    assert (work / 'valid/Private.arm64.exports').read_bytes() == selected
    changed = work / 'changed.exports'
    changed.write_bytes(original + b'_unexpected_export\n')
    reject(candidate_source=changed)
    table.write_text('\n'.join(sorted(symbols - {'_cpu_number'})) + '\n')
    reject()  # A new unrelated missing export must never be auto-filtered.
    table.write_text('\n'.join(sorted(symbols | {'_pmap_iommu_init'})) + '\n')
    reject()  # A newly provided interface requires a new reviewed contract.
    assert source.read_bytes() == original

print('Thyme export profile: exact selection, original preservation and refusal paths passed')
