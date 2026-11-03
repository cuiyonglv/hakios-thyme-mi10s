#!/usr/bin/env python3
"""Run the production copy functions on regular files, never block devices."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'scripts/backup-phone-partitions.sh').read_text()
functions = source[source.index('hash_range() {'):source.index('# The required calibration')]
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    data = work / 'source.bin'
    data.write_bytes(b'A' * 1048576 + b'B' * 1048576 + b'C' * 1048576)
    original = hashlib.sha256(data.read_bytes()).hexdigest()
    script = work / 'test.sh'
    script.write_text(r'''
set -eu
out=$1
input=$2
mkdir "$out"
errors=$out/errors.log
manifest=$out/files.tsv
: > "$errors"
: > "$manifest"
chown() { :; }
restorecon() { :; }
sleep() { :; }
''' + functions + r'''
source=outside_source; name=outside_name; skip=512; count=256
copy_checked "$input" full.img 3145728 1048576 0 3
[ "$source" = outside_source ] && [ "$name" = outside_name ]
[ "$skip" = 512 ] && [ "$count" = 256 ]
copy_checked "$input" first.bin 1048576 4096 0 "$count"
copy_checked "$input" last.bin 1048576 4096 "$skip" "$count"
hash_range() { printf '%064d\n' 0; }
if copy_checked "$input" unstable.img 1048576 4096 0 256; then exit 10; fi
[ ! -e "$out/unstable.img" ] && [ ! -e "$out/unstable.img.partial" ]
[ "$(wc -l < "$manifest")" = 3 ]
''')
    subprocess.run(['bash', str(script), str(work / 'backup'), str(data)], check=True)
    assert (work / 'backup/full.img').read_bytes() == data.read_bytes()
    assert (work / 'backup/first.bin').read_bytes() == b'A' * 1048576
    assert (work / 'backup/last.bin').read_bytes() == b'C' * 1048576
    assert hashlib.sha256(data.read_bytes()).hexdigest() == original
    for row in (work / 'backup/files.tsv').read_text().splitlines():
        name, size, digest, _ = row.split('\t')
        file = work / 'backup' / name
        assert file.stat().st_size == int(size)
        assert hashlib.sha256(file.read_bytes()).hexdigest() == digest
print('Phone backup: source unchanged, full/prefix/suffix exact, unstable reads refused, caller state preserved')
