#!/usr/bin/env python3
"""Check distributed source integrity and rejection before output replacement."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
cache = root / 'tools/apple-iig-8796'
record = json.loads((cache / 'manifest.json').read_text())
assert record['complete'] and len(record['generations']) == 17
for row in record['generations']:
    assert row['exit_code'] == 0
    for name, expected in row['outputs'].items():
        content = (cache / name).read_bytes()
        assert content and hashlib.sha256(content).hexdigest() == expected, name

with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    fixture_cache = work / 'cache'
    fixture_cache.mkdir()
    row = next(r for r in record['generations'] if r['definition'].endswith('/OSObject.iig'))
    definition = work / 'OSObject.iig'
    definition.write_bytes(b'fixture: known input bytes\n')
    row['input_sha256'] = hashlib.sha256(definition.read_bytes()).hexdigest()
    (fixture_cache / 'manifest.json').write_text(json.dumps(record))
    for name in row['outputs']:
        shutil.copyfile(cache / name, fixture_cache / name)
    header, implementation = work / 'output.h', work / 'output.cpp'

    def run():
        return subprocess.run([sys.executable, str(root / 'scripts/cached-apple-iig.py'),
            '--cache', str(fixture_cache), '--def', str(definition),
            '--header', str(header), '--impl', str(implementation),
            '--framework-name', 'DriverKit', '--', '-D__IIG=1'], capture_output=True)

    assert run().returncode == 0
    assert header.read_bytes() == (cache / 'OSObject.h').read_bytes()
    assert implementation.read_bytes() == (cache / 'OSObject.iig.cpp').read_bytes()
    original = (header.read_bytes(), implementation.read_bytes())
    definition.write_bytes(b'changed definition\n')
    assert run().returncode != 0
    assert (header.read_bytes(), implementation.read_bytes()) == original
    definition.write_bytes(b'fixture: known input bytes\n')
    (fixture_cache / 'OSObject.iig.cpp').write_bytes(b'truncated or changed source')
    assert run().returncode != 0
    assert (header.read_bytes(), implementation.read_bytes()) == original
    shutil.copyfile(cache / 'OSObject.iig.cpp', fixture_cache / 'OSObject.iig.cpp')
    header = definition
    assert run().returncode != 0
    assert definition.read_bytes() == b'fixture: known input bytes\n'

print('Apple IIG cache: 34 source hashes and fail-before-replace checks passed')
