# Thyme UEFI source overlays

These sources replace the corresponding files under `umiPkg/Library/` in the
separately staged Mu-Silicium platform. The map and `UmiXnu.c` derive from the UMI package in
azzlantern/hackios-thyme, revision `be4e3edb18c3215c6fd958882f8caae68dadfc2b`.
That repository distributes the package under APSL 2.0; those files retain that
license, whose complete text is in `LICENSE.APSL` at this repository's root.

The map uses this phone's captured active-slot XBL and bootloader-patched FDT.
The secure/PIL descriptors are disjoint rather than literal copies of overlapping
firmware configuration entries. The original boot information and log buffers
stay reserved, including after UEFI boot-services memory becomes reclaimable.
The live display-dump reservation and DDR hole are also excluded from allocation.

Static validation found 29 non-overlapping RAM descriptors and complete
non-conventional coverage of all 22 static live-FDT reserved-memory ranges.
Those checks do not establish a UEFI boot or validate DMA behavior. The full
platform build, bounded RAM boot and unattended recovery path are still required.
See `docs/UEFI-BRINGUP.md`; this directory is not a ready-to-flash image.

`BootManagerLib/PayloadView.h` is a new MIT-licensed bounded XNUP selector.
The handoff validates the whole declared container before calling `LoadImage`,
including its terminal area and any entries after the executable. It rejects
duplicate executables, unbounded names, reserved fields, truncated records and
overflowing rounded lengths. Failure clears the candidate output. The host test
executes this actual header at protected page boundaries with ASan and UBSan;
it does not emulate a firmware handoff or certify a watchdog recovery path.
