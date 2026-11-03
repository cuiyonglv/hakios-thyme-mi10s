# Unattended recovery checks

The first thyme resident-loader RAM boot and its normal cold-reset return to
Android passed. It did not arm the virtual watchdog: the original check required
the exact Redwood platform pointer, while thyme selected the shared
`unknown-kona` fallback. A framebuffer address does not establish watchdog
capability.

The updated loader first locates the Qualcomm SCM protocol, then uses the
standard vendor-hypervisor UID discovery call. Only the complete known Qualcomm
UID permits the Haven watchdog STATUS call. A missing protocol, different UID
or rejected status stops negotiation before timeout/control writes. This also
keeps a QEMU GOP fallback from blindly issuing device-specific SMCs.

The primary references are Xiaomi's
[Haven control driver](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/7d08fb515ffb638ec39b317ff85ff2646520718c/drivers/virt/haven/hh_ctrl.c)
and [virtual watchdog driver](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/7d08fb515ffb638ec39b317ff85ff2646520718c/drivers/virt/haven/hh_virt_wdt.c).
They define the vendor UID, millisecond timeout arguments, success/error returns,
enable-state bit and mandatory CONTROL bit 1. The new loader skips redundant
enable calls, verifies timeout/pet results and reads back the enable state. A
failed check clears acceptance and rolls back a newly enabled watchdog using
CONTROL=2. It leaves a previously enabled service in its original enable state.

`oem hang` now refuses the request unless watchdog negotiation completed. Host
regression executes the production negotiation and refusal code with simulated
protocol/SMC responses under ASan/UBSan. It covers missing/null interfaces,
unknown UID, rejected operations, an already enabled service, bad readback and
rollback. These tests establish decision behavior, not a phone reset.

The live thyme FDT describes `qcom,msm-watchdog` at `0x17c10000`; it does not prove
that the Haven service exists. Xiaomi's related Kona
[physical watchdog driver](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/2fdca387269615872d4ff798e8da7d33345c1338/drivers/soc/qcom/watchdog_v2.c)
provides a separate register-based interface. No physical-watchdog MMIO writes
have been added or tested here. If virtual discovery fails, report it as
unsupported and investigate that matching hardware path rather than treating a
successful unrelated SMC or normal reboot as watchdog evidence.

Phone acceptance still requires readback from the new loader and, only after
successful negotiation, a bounded expiry test that returns to the original
Android build without a button press. Until that test passes, automatic recovery
from a hung XNU kernel is unproven.

## Thyme discovery result

A second RAM-only phone session used loader revision
`76bf72906df47921980fad055e0e62d420c5e139`. The resident log reported vendor UID
`ffffffffffffffff 0 0 0`, which does not identify the required Qualcomm virtual
ABI. The loader therefore stopped before Haven STATUS, timeout or CONTROL calls.
USB initialization and a normal resident cold reset back to original Android
slot B passed again. The read-only image included no XNU kernel.

This establishes that the virtual watchdog path is unavailable in this tested
firmware environment. No deliberate hang was requested, and no physical
watchdog registers were changed. The next recovery work must examine the actual
thyme physical-watchdog interface, with read-only register observations before
configuration and a separate expiry test before relying on it.
