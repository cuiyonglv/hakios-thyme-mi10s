# Unattended read-only partition capture

`scripts/backup-phone-partitions.sh RUNID` is intended for a one-shot root service
inside a temporary Android RAM boot. It checks UID, thyme, the verified Android
release and the current B slot before starting. It is not a flashing script.

The source list is explicit: both boot/vendor_boot/dtbo/vbmeta variants, device
calibration and identity partitions, metadata, and available firmware partitions.
No full super or userdata image is copied. Each source must resolve to a block
device, and each ordinary partition is limited to 512 MiB. Whole UFS LUNs contribute
only their first and last 1 MiB, with actual byte and sector sizes recorded for
subsequent host GPT CRC validation.

For each copy, source SHA-256 before the read, output SHA-256 and source SHA-256
afterwards must all agree. Size must match too. There are three bounded retries;
an unstable or unreadable source is recorded as a failure and is not published.
Files are created under a new private `/data/local/tmp/thyme-backup-RUNID` directory,
with owner shell and mode 0600 for authenticated ADB retrieval. Block devices are
always input files, never output files. The script does not flash, erase, select a
slot or edit a partition table.

The host regression executes the actual copy functions on regular temporary files,
checks full/prefix/suffix bytes and hashes, rejects mismatched source hashes and
checks that function-local variables preserve the caller's GPT suffix offset.
Live capture, host-side verification and a return to the original Android boot
remain required before using any backup as a recovery guarantee. Backups contain
private identifiers and possibly key material; keep them outside the public repo.
