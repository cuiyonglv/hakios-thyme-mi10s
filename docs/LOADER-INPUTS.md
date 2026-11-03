# Loader input and memory bounds

The loader now checks the XNUP container before exposing entry pointers. Entry
names and text must terminate inside their declared fields; payload/entry/padding
lengths must fit the available bytes. Duplicate recognized entries, nonzero
reserved fields and overflowing/invalid numeric values are rejected. Recognized
entries remain kernel, ramdisk, cmdline, physbase, rdres, checkpoint and usb;
bounded unknown entries remain forward-compatible.

BootShim initially supplies a reserved 64 MiB region without an independent
transfer count. After a USB or installation read starts, the loader tracks the
actual completed length. A short or oversized USB receive cannot publish a mixed
old/new payload. An incomplete resident-image transfer also clears its valid size.

Installation reading had a separate trust-boundary problem: efi_main cached the
initial BootShim payload flag, while an installation attempt overwrote that same
RAM. A failed SHA check could leave a structurally parseable replacement for the
later RAM fallback. The loader now invalidates RAM-payload availability before
installation reads and publishes it only after digest and APFS-magic checks pass.
The earlier cached flag cannot bypass that state. This is integrity checking, not
publisher authentication.

Before copying any kernel segment, the loader checks the bounded Mach-O header,
arm64 executable type/subtype, command sizes/count/coverage, segment file ranges,
virtual extent overflow/overlap and the ARM64 UNIXTHREAD state. The entry must lie
in file-backed executable memory. This loader supports up to 128 nonempty kernel
segments and rejects arm64e subtype kernels. Actual XNU 8796 output passes these
checks. Unsupported or malformed images stay out of the copy/execute path.

Kernel, both ramdisks, device tree and boot arguments are planned using checked
offsets before constructing physical pointers. They must fit the claimed window,
with the existing kernel reserve and resident-image separation retained. EFI
memory descriptors, forced-base alignment/overflow and payload-window exclusion
are checked before allocation. Kernel marker scans also require the entire marker
structure or return slot to fit inside the loaded range.

`scripts/test-loader-payload.py` runs the actual production parser, validator,
memory planner, USB receiver and installation control flow with ASan/UBSan,
guard pages and thousands of mutations. It injects read, digest and APFS failures;
the old installation-publication behavior fails the regression. Optional
`--kernel FILE --root FILE` tests the real cross-built kernel and RAM root packed
by the production Python entry writer. These host checks do not simulate EFI
DMA/cache behavior, real USB timing or execution on the phone.
