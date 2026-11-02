#!/bin/bash
# Build the XNU loader as a freestanding AArch64 UEFI application.
set -e
cd "$(dirname "$0")"
IDENTITY_DIR=${IDENTITY_DIR:-.}
if [ ! -f "$IDENTITY_DIR/identity.h" ]; then
  echo "Missing identity.h: generate it locally with mkidentity.py, or set IDENTITY_DIR." >&2
  exit 1
fi
clang --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector -fshort-wchar \
  -mgeneral-regs-only -O1 -Wall -Wextra -Wno-unused-parameter -I"$IDENTITY_DIR" -c loader.c -o loader.obj
clang --target=aarch64-unknown-windows -ffreestanding -fno-builtin -fno-stack-protector \
  -mgeneral-regs-only -O2 -Wall -Wextra -c sha256.c -o sha256.obj
# /base: UEFI loads an image at its preferred address when that is free. The PE default (0x140000000)
# is in the middle of the phone's largest block of RAM, which XNU needs in one piece.
lld-link /subsystem:efi_application /entry:efi_main /nodefaultlib /base:0xA1000000 /out:loader.efi loader.obj sha256.obj
ls -la loader.efi
