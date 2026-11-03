#!/bin/bash
# Build the XNU loader as a freestanding AArch64 UEFI application.
set -e
cd "$(dirname "$0")"
IDENTITY_DIR=${IDENTITY_DIR:-.}
ALLOW_STORAGE_WRITES=${ALLOW_STORAGE_WRITES:-0}
case "$ALLOW_STORAGE_WRITES" in
  0|1) ;;
  *) echo "ALLOW_STORAGE_WRITES must be 0 or 1" >&2; exit 2 ;;
esac
if [ ! -f "$IDENTITY_DIR/identity.h" ]; then
  echo "Missing identity.h: generate it locally with mkidentity.py, or set IDENTITY_DIR." >&2
  exit 1
fi
clang --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector -fshort-wchar \
  -mgeneral-regs-only -O1 -Wall -Wextra -Wno-unused-parameter \
  -DTHYME_STORAGE_WRITES="$ALLOW_STORAGE_WRITES" -I"$IDENTITY_DIR" -c loader.c -o loader.obj
clang --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector \
  -mgeneral-regs-only -O2 -Wall -Wextra -c sha256.c -o sha256.obj
# /base: UEFI loads an image at its preferred address when that is free. The PE default (0x140000000)
# is in the middle of the phone's largest block of RAM, which XNU needs in one piece.
lld-link /subsystem:efi_application /entry:efi_main /nodefaultlib /base:0xA1000000 /out:loader.efi loader.obj sha256.obj
ls -la loader.efi
echo "loader direct storage writes: $ALLOW_STORAGE_WRITES (0=disabled)"
python3 - "$ALLOW_STORAGE_WRITES" "$IDENTITY_DIR/identity.h" <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess
import sys

artifact = Path('loader.efi')
sources = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
           for name in ('loader.c', 'efi.h', 'sha256.c', 'sha256.h', 'build.sh')}
revision = subprocess.run(['git', 'rev-parse', 'HEAD'], text=True, capture_output=True)
dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'],
                       text=True, capture_output=True)
manifest = {
    'schema': 1,
    'artifact': {'file': artifact.name, 'bytes': artifact.stat().st_size,
                 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()},
    'target': 'aarch64-unknown-windows',
    'image_base': '0xA1000000',
    'direct_storage_writes': sys.argv[1] == '1',
    'sources': sources,
    'identity_sha256': hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest(),
    'revision': revision.stdout.strip() if revision.returncode == 0 else None,
    'tracked_source_dirty': bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
    'clang': subprocess.check_output(['clang', '--version'], text=True).splitlines()[0],
    'linker': subprocess.check_output(['lld-link', '--version'], text=True).strip(),
}
Path('loader.build.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('build manifest: loader.build.json')
PY
