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

`oem thyme-wdt-read` provides that initial read-only snapshot of ENABLE, STATUS,
BARK and BITE at the captured `0x17c10000` base. It checks the observed Qualcomm
MIDR implementer/part, display geometry/base and SCM interface before any read,
accepts no address argument, and refuses to run while a kernel session is active.
It neither arms the physical watchdog nor changes any of its registers. This
diagnostic is intended only for the matching, separately verified thyme UEFI map.

Phone readback with loader `72510662425ca7e8c818c9798dbba6f493d40319` passed the
profile checks and returned ENABLE=0, STATUS=0, BARK=`0x7ffff`, BITE=`0xfffff`.
The register page is accessible from the resident loader and the physical
watchdog was disabled during this observation. Retrieval of the log and a normal
cold reset to original Android slot B also passed. No watchdog registers were
changed and this was not an expiry test.

The explicit `oem thyme-wdt-test` experiment programs an eight-second BITE only
when the matching physical watchdog is disabled, its existing BARK is later
than that deadline, and no virtual watchdog or previous trial is active. It
preserves BARK, verifies register readback and samples the counter against the
architectural timer over 20 ms. Rejected configuration or clock evidence causes
disable/restore with a separate rollback readback result. Accepted trials leave
USB responsive and do not pet the timer; the host can request a normal reset if
expiry fails. Kernel launches and repeated trials are blocked while it runs.
Host tests cover that policy with simulated registers.

The phone expiry experiment with loader
`b55b663dbdfc9c91c8319a8330eec90c3a45bccc` then passed: configuration readback
matched, the sampled counter measured 32599 Hz, and the host issued no reset
after starting the trial. Resident USB disappearance was observed at 13.063 s
(including host polling/device-disconnect delays). Original Android slot B
finished booting automatically with a new kernel boot ID. The firmware reported
boot reason `bootloader`, which does not independently identify the reset cause.
The evidence is the accepted physical configuration, advancing counter and
automatic return without a host reboot; it is not a precise expiry-time trace.

This verifies the explicit physical expiry experiment. Integration of periodic
physical petting, timeout changes and kernel handoff remains necessary. In
particular, the loader must not request XNU's virtual-SMC pet path when only a
physical backend is active. No XNU hang or iOS persistence result follows from
this experiment.

## Periodic physical backend

`oem thyme-wdt-arm` reuses the same checked configuration and counter sampling
with a 12-second BITE and periodic petting. The original later BARK stays intact.
Timeout updates are limited to 1–12 seconds and verified by readback; unsupported
updates refuse kernel handoff. `getvar watchdog` distinguishes physical, virtual
and inactive states, and `getvar watchdog-timeout-ms` reports the physical limit.
`getvar loader-mpidr` identifies the loader core for a separate-core launch.

Physical mode omits XNU's virtual-SMC pet boot argument and refuses a real kernel
handoff on the loader's own core. The existing dedicated-core monitor pets the
physical timer while watching XNU. USB event servicing also checks the pet
deadline before returning successful transfers, so sustained traffic cannot
starve the timer. Production-function host tests cover configuration, timeout
clamping, pet operations and continuous USB completion behavior.

This backend still needs phone validation: remain resident longer than its
deadline while petting, then stop petting and confirm automatic Android return.
The earlier explicit expiry experiment does not establish that integration.

The hang-test path suppresses petting **before** transmitting its OKAY reply.
Reply transmission pumps USB events too; waiting until after that transfer could
keep feeding the timer while the host had already received the acknowledgement.
The host observes Windows device-tree state rather than treating a failed
Fastboot command as proof of physical disconnection. USB re-enumeration timing
can include the subsequent firmware/Android boot and is not an exact bite trace.
