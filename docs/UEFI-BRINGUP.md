# Thyme UEFI preparation

Status: native UEFI build and firmware readback checks passed. No new UEFI phone boot has
been accepted. Kernel, root image and loader builds do not establish that the
firmware handoff, memory map or recovery timer works on this phone.

The stock Android RAM boot and subsequent return to original Android slot B
passed without selecting another slot or flashing a partition. A separate
temporary root service captured 52 stable recovery files and all six UFS LUNs'
primary/secondary GPT prefixes and suffixes. Host verification passed file
hashes, both GPT CRCs, exact table agreement and partition name/size mappings.
This is a verified capture, not a destructive restore test.

A second root service captured the actual bootloader-patched FDT (933970 bytes),
memory map, command line and boot logs, then returned to the original Android.
All captured hashes and the FDT header size matched. The live FDT confirms:

- the thyme model and GPU zap region `0x8c41a000 + 0x2000`;
- DDR banks `0x80000000 + 0x3bb00000`, `0xc0000000 + 0xc0000000`,
  and `0x180000000 + 0x100000000`;
- the DDR hole at `0xbbb00000..0xc0000000`;
- splash reservation `0x9c000000 + 0x2300000`, DFPS reservation
  `0x9e300000 + 0x100000`, and display dump `0xb0400000 + 0x1000000`.

The stock DRM modes report 1080x2340 at both 60 and 90 Hz. These are Android
measurements; they do not prove that either refresh rate works under XNU.

## TzDxe profile

The captured active-slot XBL contains 146 non-padding FFS files. The TzDxe GUID is
`6925fad3-e243-4b69-ad61-3f978c8abc60`. Its 61440-byte PE32+ ARM64 executable has
SHA-256 `dfc36c4c0ee54d251b9bb9cae1a5782526305d815af7ce23b3ca9b3406e57601`.

The reference thyme repository's legacy SM8250 FD contains a module with the
same GUID, size and exact code section. Comparing every byte reveals exactly
one difference: the reference sets byte `0xd469` to 1; the phone's original
has 0. The resulting module SHA-256 is
`3494282f851483f85bff6b8a173bfcb789cb976aaeb490531ba6e4423d24d0e9`.
Disassembly of the original establishes the state checks at `0x2e28` and
`0x32d8`, the corresponding state stores, and the shared load path at `0x2fc4`.
This is the existing app-loaded state adjustment used for the UEFI RAM handoff.
It does not modify TrustZone itself or a persistent partition.

`scripts/prepare-thyme-tz.py` accepts only that complete original hash and size,
checks the reviewed ARM64 instructions and PE identity, modifies that one byte,
checks the complete resulting hash, and creates a new file without overwriting.
The original extraction remains unchanged. A different firmware revision must
be investigated separately; do not replace the hash to bypass the guard.

## Remaining firmware gates

The captured `uefiplat.cfg` differs from the UMI reference: its TZApps and PIL
reservations have different extents and overlap legacy heap descriptions.
The source map under `src/uefi/Library/MemoryMapLib/` now uses disjoint QTEE,
TZApps and PIL descriptors derived from those captures, with the actual display
dump and DDR-hole reservations. Static checks passed 29 disjoint RAM descriptors
and complete non-conventional coverage of all 22 static live-FDT reservations.
Original boot-info and log buffers remain reserved. The map still needs the
complete UEFI build and phone validation; do not treat static checks as a boot.

Use Mu-Silicium `42fe563791e51bf5500c2e470ed294ca0c94b97c` and its exact
submodule revisions. Match required driver INF GUIDs to the phone's own XBL;
do not substitute another phone's firmware. Keep extracted modules, FDT,
backups, logs and all device identifiers outside the public repository. Before
a temporary UEFI test, verify the full cohort and an unattended return path.
Storage writes, GPT changes and slot changes remain disabled.

## Native build result

The RELEASE CLANGPDB build completed with the staged thyme source, producing a
3145728-byte FD. Postbuild validation checked 48 compiled PE images. Independent
parsing of the completed FD found all 43 active Qualcomm driver GUIDs and checked
their executable/dependency section hashes against the phone's captured XBL,
with only the explicitly reviewed TzDxe state byte changed.

That audit initially found a reference APRIORI entry for `ResetRuntimeDxe` with no
corresponding driver embedded in the FD. `thyme-platform.patch` removes the stale
entry and retains the source-built `ResetSystemRuntimeDxe` with the PSCI library.
The actual build report selects SMC (`PcdMonitorConduitHvc=0`), matching the live
FDT's PSCI method. The boot-manager fallback requests a cold reset after its
10-second wait, so a missing payload follows the normal Android boot path rather
than intentionally shutting the phone down. These are source and build checks;
the reset and watchdog paths still require phone validation.

The platform patch also selects SM8250-AC and the Mi 10S model identity. It applies
cleanly to the pinned reference package and reproduces the three changed native
source files exactly. The bounded handoff and memory map are separate source
overlays under `src/uefi/Library/`. No prebuilt firmware is published here.
