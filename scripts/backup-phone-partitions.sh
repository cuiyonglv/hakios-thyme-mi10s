#!/system/bin/sh
# Read-only block sources. Outputs are new private regular files in shell's tmp directory.
# This runs as the one-shot service in a temporary Android RAM boot, never as a flasher.
# Invoke with the verified 64-bit BusyBox ash; Android mksh arithmetic wraps at 32 bits.
set -u
umask 077
run=${1:-}
case "$run" in ''|*[!A-Za-z0-9_-]*) exit 2;; esac
[ "${#run}" -le 32 ] || exit 2
[ "$((4294967296))" = 4294967296 ] || exit 6
[ "$(id -u)" = 0 ] || exit 3
[ "$(getprop ro.product.device)" = thyme ] || exit 4
[ "$(getprop ro.build.version.incremental)" = V816.0.4.0.TGACNXM ] || exit 4
[ "$(getprop ro.boot.slot_suffix)" = _b ] || exit 4
out=/data/local/tmp/thyme-backup-$run
[ ! -e "$out" ] || exit 5
mkdir "$out" || exit 5
chown 2000:2000 "$out"
restorecon "$out" 2>/dev/null || true
errors=$out/errors.log
manifest=$out/files.tsv
geometry=$out/geometry.tsv
: > "$errors"
: > "$manifest"
: > "$geometry"
failures=0

status() {
    printf '%s\n' "$1" > "$out/status.partial"
    chown 2000:2000 "$out/status.partial"
    chmod 600 "$out/status.partial"
    mv "$out/status.partial" "$out/status"
}
status running

hash_range() {
    dd if="$1" bs="$2" skip="$3" count="$4" 2>> "$errors" | sha256sum | cut -d ' ' -f 1
}

copy_checked() {
    local source name expected block skip count temporary attempt before copied after
    source=$1
    name=$2
    expected=$3
    block=$4
    skip=$5
    count=$6
    temporary=$out/$name.partial
    for attempt in 1 2 3; do
        before=$(hash_range "$source" "$block" "$skip" "$count")
        [ "${#before}" = 64 ] || continue
        dd if="$source" of="$temporary" bs="$block" skip="$skip" count="$count" 2>> "$errors" || continue
        [ "$(stat -c %s "$temporary")" = "$expected" ] || continue
        copied=$(sha256sum "$temporary" | cut -d ' ' -f 1)
        after=$(hash_range "$source" "$block" "$skip" "$count")
        if [ "$before" = "$copied" ] && [ "$after" = "$copied" ]; then
            mv "$temporary" "$out/$name" || return 1
            chmod 600 "$out/$name"
            chown 2000:2000 "$out/$name"
            restorecon "$out/$name" 2>/dev/null || true
            printf '%s\t%s\t%s\t%s\n' "$name" "$expected" "$copied" "$source" >> "$manifest"
            chown 2000:2000 "$manifest"
            return 0
        fi
        sleep 1
    done
    rm -f "$temporary"
    printf 'UNSTABLE_OR_UNREADABLE\t%s\n' "$name" >> "$errors"
    return 1
}

# The required calibration/identity partitions were confirmed present on this phone.
required='boot_a boot_b vendor_boot_a vendor_boot_b dtbo_a dtbo_b vbmeta_a vbmeta_b vbmeta_system_a vbmeta_system_b persist persistbak metadata modemst1 modemst2 fsg fsc'
optional='xbl_a xbl_b xbl_config_a xbl_config_b abl_a abl_b misc devcfg_a devcfg_b tz_a tz_b hyp_a hyp_b uefisecapp_a uefisecapp_b keymaster_a keymaster_b cmnlib_a cmnlib_b cmnlib64_a cmnlib64_b qupfw_a qupfw_b'
for partition in $required $optional; do
    link=/dev/block/by-name/$partition
    if [ ! -b "$link" ]; then
        case " $required " in *" $partition "*) failures=$((failures + 1));; esac
        printf 'ABSENT\t%s\n' "$partition" >> "$errors"
        continue
    fi
    source=$(readlink -f "$link")
    case "$source" in /dev/block/sd*) ;; *) failures=$((failures + 1)); continue;; esac
    bytes=$(blockdev --getsize64 "$source" 2>> "$errors")
    case "$bytes" in ''|*[!0-9]*) failures=$((failures + 1)); continue;; esac
    if [ "$bytes" -le 0 ] || [ "$bytes" -gt 536870912 ]; then
        failures=$((failures + 1)); continue
    fi
    count=$(((bytes + 1048575) / 1048576))
    status "copying $partition"
    copy_checked "$source" "$partition.img" "$bytes" 1048576 0 "$count" || failures=$((failures + 1))
done

# Capture both GPT regions from every whole UFS LUN. Never dump an entire LUN.
luns=0
for source in /dev/block/sd[a-z]; do
    [ -b "$source" ] || continue
    disk=${source##*/}
    bytes=$(blockdev --getsize64 "$source" 2>> "$errors")
    sector=$(blockdev --getss "$source" 2>> "$errors")
    case "$bytes" in ''|*[!0-9]*) failures=$((failures + 1)); continue;; esac
    case "$sector" in 512|4096) ;; *) failures=$((failures + 1)); continue;; esac
    [ "$bytes" -ge 2097152 ] && [ "$((bytes % sector))" = 0 ] || continue
    luns=$((luns + 1))
    count=$((1048576 / sector))
    skip=$(((bytes - 1048576) / sector))
    printf '%s\t%s\t%s\n' "$disk" "$bytes" "$sector" >> "$geometry"
    status "copying $disk GPT"
    copy_checked "$source" "$disk-gpt-first.bin" 1048576 "$sector" 0 "$count" || failures=$((failures + 1))
    copy_checked "$source" "$disk-gpt-last.bin" 1048576 "$sector" "$skip" "$count" || failures=$((failures + 1))
done
[ "$luns" -gt 0 ] || failures=$((failures + 1))
printf '%s\n' "$failures" > "$out/failures"
chown 2000:2000 "$errors" "$manifest" "$geometry" "$out/failures"
chmod 600 "$errors" "$manifest" "$geometry" "$out/failures"
restorecon -R "$out" 2>/dev/null || true
sync
if [ "$failures" = 0 ]; then status complete; else status incomplete; fi
sync
