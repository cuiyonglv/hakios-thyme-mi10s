#!/system/bin/sh
# Run from the temporary Android root init service; all device inputs are read-only.
set -u
umask 077
run=${1:-}
case "$run" in ''|*[!A-Za-z0-9_-]*) exit 2;; esac
[ "${#run}" -le 32 ] || exit 2
[ "$(id -u)" = 0 ] || exit 3
[ "$(getprop ro.product.device)" = thyme ] || exit 4
[ "$(getprop ro.build.version.incremental)" = V816.0.4.0.TGACNXM ] || exit 4
[ "$(getprop ro.boot.slot_suffix)" = _b ] || exit 4
out=/data/local/tmp/thyme-backup-$run
[ ! -e "$out" ] || exit 5
mkdir "$out" || exit 5
chown 2000:2000 "$out"
restorecon "$out" 2>/dev/null || true
printf 'running\n' > "$out/status"
errors=$out/errors.log
: > "$errors"
: > "$out/files.tsv"
failures=0

# Input names are fixed here. Bound every read, including virtual kernel files.
capture() {
    local name=$1 source=$2 cap=$3 required=$4
    if ! head -c "$cap" "$source" > "$out/$name.partial" 2>> "$errors"; then
        rm -f "$out/$name.partial"
        [ "$required" = 0 ] || failures=$((failures + 1))
        return
    fi
    local bytes
    bytes=$(stat -c %s "$out/$name.partial")
    if [ "$bytes" = 0 ] || [ "$bytes" -ge "$cap" ]; then
        printf 'empty or truncated: %s\n' "$source" >> "$errors"
        rm -f "$out/$name.partial"
        [ "$required" = 0 ] || failures=$((failures + 1))
        return
    fi
    mv "$out/$name.partial" "$out/$name"
    printf '%s\t%s\t%s\t%s\n' "$name" "$bytes" "$(sha256sum "$out/$name" | cut -d ' ' -f 1)" "$source" >> "$out/files.tsv"
}

capture live-bootloader.dtb /sys/firmware/fdt 4194304 1
capture iomem.txt /proc/iomem 1048576 1
capture cmdline.txt /proc/cmdline 65536 1
capture meminfo.txt /proc/meminfo 65536 1
capture fb-virtual-size.txt /sys/class/graphics/fb0/virtual_size 4096 0
capture fb-stride.txt /sys/class/graphics/fb0/stride 4096 0
capture fb-bits-per-pixel.txt /sys/class/graphics/fb0/bits_per_pixel 4096 0
capture fb-name.txt /sys/class/graphics/fb0/name 4096 0
capture panel-modes.txt /sys/class/drm/card0-DSI-1/modes 65536 0
# /proc/kmsg is a destructive stream read; deliberately use the dmesg snapshot API.
if dmesg > "$out/dmesg.txt" 2>> "$errors"; then
    bytes=$(stat -c %s "$out/dmesg.txt")
    if [ "$bytes" -gt 16777216 ]; then
        rm -f "$out/dmesg.txt"
        printf 'dmesg exceeded capture limit\n' >> "$errors"
    else
        printf 'dmesg.txt\t%s\t%s\tdmesg\n' "$bytes" "$(sha256sum "$out/dmesg.txt" | cut -d ' ' -f 1)" >> "$out/files.tsv"
    fi
fi
printf '%s\n' "$failures" > "$out/failures"
if [ "$failures" = 0 ]; then
    printf 'complete\n' > "$out/status"
else
    printf 'incomplete\n' > "$out/status"
fi
chown 2000:2000 "$out"/*
chmod 600 "$out"/*
restorecon -R "$out" 2>/dev/null || true
[ "$failures" = 0 ]
