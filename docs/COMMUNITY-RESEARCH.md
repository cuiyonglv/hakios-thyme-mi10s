# Community findings for the Mi 10S port

Checked on 2026-11-02. Source review and host tests are distinct from a phone
boot. None of the changes below establishes working persistence on this phone.

## Sources and applicability

| Project / revision inspected | Relevant evidence | Decision for thyme |
| --- | --- | --- |
| [HakiOS common tree](https://github.com/oosexgang/hakios-xnu-kernel/tree/96ffbc532e970f3540885755d457e401e04d4707) | [Storage review](https://github.com/oosexgang/hakios-xnu-kernel/blob/96ffbc532e970f3540885755d457e401e04d4707/docs/driver-review/storage.md), findings F1 and F3 | The same buffer ownership and allocation patterns exist here. Fixed and covered by host regression tests. |
| [Mi 10S reference package](https://github.com/azzlantern/hackios-thyme/tree/be4e3edb18c3215c6fd958882f8caae68dadfc2b) | Direct ST FTS521 input, separate from the old vendor-library diagnostics | This port already has a complete normal path in `redwood_st_touch.c`. Keep it; exclude unavailable legacy diagnostics from the default build. |
| [UMI unified tree](https://github.com/JopangJovan/hackios-xnu-kernel-umi/tree/77fa050f34ec73c9d5dd8ee1025f82c32202a79a) | Same SoC family; matched kernel, loader, userland and image tools | Useful reference for dependencies and radio support. Newer userland/Metal integration must not be mixed into the older port without interface checks. |
| [ios-topaz](https://github.com/MakrSas/ios-topaz/tree/e4772911e116ab4ab6c4ac1f38e6106fefe8b483) | Patches 0128 (IOKit quiescence) and 0138 (IOAcceleratorES lookup delay) | Good boot-log hypotheses. Check NO_KEXTD and actual service matching first; neither patch is a storage restore fix or proof of GPU support. |
| [ios-sweet](https://github.com/dipa-studio/ios-sweet/tree/45263bee51df4a0f55cdf9c649af886399488ddb) | Patch 0030, bounded UFS writes; journaled HFS+ root; persistent/USB logs | A directly writable data filesystem avoids per-boot archive restore, but requires a new storage/filesystem integration. It is not a drop-in replacement for this APFS/RAM-root design. |

The user-supplied `ios-topaz/tree/topaz2` ref was not present when checked.
The repository advertised `topaz` as its default branch; that is the revision
listed above. No topaz/sweet images, GPIO values, MMIO addresses or power
sequences were transferred to the Mi 10S.

## Changes implemented

1. `loader.c`: give log read-back its own aligned buffer. The mailbox's read
   reply may still be in use when periodic log verification runs. The host
   test demonstrates that the old shared-buffer path corrupts that reply;
   whether that timing caused a particular phone hang remains unproven.
2. `redwood_ufs.c`: initialize the shared bounce buffer while holding
   `ufs_lock`. Concurrent first reads previously raced before taking that lock.
   Allocation failure releases the lock. The host test exercises two first
   readers, a block-crossing read, aligned reads, allocation failure and native
   controller exclusion; the old ordering fails the race assertion.
3. `bsd/conf/files` and `redwood_goodix.c`: keep the normal
   `redwood_st_touch_init` path and omit legacy vendor-library diagnostic
   sources. Modes 29/30/31/33/36 explicitly report that the optional library
   is unavailable. Mode 35 keeps its direct controller operations but omits
   the vendor-only frame-sum diagnostic. Restoring those old diagnostics
   requires their sources, build entries and `REDWOOD_FTS_VENDOR_LIBRARY`.
4. `var_store.c`: remove the unconditional O_RDWR diagnostic probe before a
   read-only restore/show operation. This is a cleanup, not a verified fix
   for the restore hang.

## Validation and current limits

Run the host checks on Linux:

```sh
python3 scripts/test-storage-mailbox.py
python3 scripts/test-loader-log-buffer.py
```

Both extract and compile the relevant production function with host mocks.
Each also runs the old implementation and requires its regression to fail.
They do not test DMA/cache behavior, real UEFI block I/O or device timing.

The loader cross-compiles and links as AArch64 UEFI. After isolating the
legacy touch diagnostics, the XNU tree produced linked unstripped/stripped
kernel files. The complete XNU build still fails in symbol-set export checks
and LLDB macro packaging. A linked file is not a fully accepted build or a
boot result. The firmware, UEFI and phone-specific storage integration remain
unfinished. No phone partition has been flashed by this work.

## Next investigations

- Instrument restore phases (slot header, checksum pass, unpack and last entry)
  and collect phone logs before choosing a timeout/recovery policy. A timeout
  that merely starts launchd over a half-restored var is not sufficient.
- Review F2 from the common storage audit: native UFS request waiters can spend
  repeated timeouts after the path is dead. This is not yet changed here.
- Measure IOKit quiet/service lookup waits before considering topaz 0128/0138.
- Complete a verified recovery/partition-backup path and extract this phone's
  firmware. Then test a bounded RAM session before persistent installation.
- Preserve the write guard when replacing the author's fixed partition
  constants with the actual device's validated layout.
