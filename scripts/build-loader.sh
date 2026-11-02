#!/bin/bash
# Build the port's loader (the EFI application the firmware starts) and repack the Android boot
# image that carries it.
#
# The boot image's "kernel" is gzip(BootShim + UEFI FD + XNUP payload), followed by the device tree;
# the XNUP payload's "loader" entry is this loader. Everything except that payload is kept from
# working image given in SRC_IMG.
#
# Usage: scripts/build-loader.sh [SOURCE_IMAGE]
#   SOURCE_IMAGE  the boot image that works today (has the UEFI + DTB)
#                 default: $IOS/thyme/thyme-start.img
#
# Requires: clang, lld-link (the loader is a freestanding AArch64 UEFI application).
set -e

IOS=${IOS:-$HOME/hackios}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC_IMG=${1:-$IOS/thyme/thyme-start.img}
LOADER_SRC=${LOADER_SRC:-$ROOT/src/loader}        # build this checkout's validated loader
OUT_IMG=${OUT_IMG:-$IOS/thyme/thyme-start-new.img}

cd "$ROOT"

echo "== building loader.efi"
( cd "$LOADER_SRC" && bash build.sh )

echo "== repacking the boot image"
python3 - "$SRC_IMG" "$LOADER_SRC/loader.efi" "$OUT_IMG" <<'PY'
import gzip, struct, sys, zlib

src, loader_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(src, "rb").read()
assert data[:8] == b"ANDROID!", "not an Android boot image"
ks, ka, rs, ra, ss, sa, ta, ps = struct.unpack("<8I", data[8:40])

# the kernel section: gzip(BootShim + UEFI FD [+ XNUP payload]) + DTB
d = zlib.decompressobj(31)
unpacked = d.decompress(data[ps:ps + ks]) + d.flush()
dtb = d.unused_data
xn = unpacked.find(b"XNUP")
shim_fd, old = unpacked[:xn], unpacked[xn:]

# keep the old payload entries, swap the loader
osize = struct.unpack("<I", old[4:8])[0]
entries, p = [], 8
while p + 24 <= 8 + osize:
    name, ln, _ = struct.unpack("<16sII", old[p:p + 24])
    name = name.split(b"\0")[0].decode()
    if not name:
        break
    entries.append((name, old[p + 24:p + 24 + ln]))
    p += 24 + ((ln + 15) & ~15)

loader = open(loader_path, "rb").read()
body = b""
for name, val in entries:
    val = loader if name == "loader" else val
    body += struct.pack("<16sII", name.encode(), len(val), 0) + val + b"\0" * (-len(val) % 16)
payload = struct.pack("<II", 0x50554E58, len(body)) + body

knew = gzip.compress(shim_fd + payload, 6) + dtb
header = bytearray(data[:ps])
struct.pack_into("<I", header, 8, len(knew))
pad = lambda n: (n + ps - 1) // ps * ps
out = bytearray(header) + knew + b"\0" * (pad(len(knew)) - len(knew))
rd = data[ps + pad(ks):ps + pad(ks) + rs]
out += rd + b"\0" * (pad(rs) - rs)
open(out_path, "wb").write(out)
print("boot image: %d bytes (loader %d)" % (len(out), len(loader)))
PY

echo "== done: $OUT_IMG"
