# hakios-thyme — Hakios on the Xiaomi Mi 10S (thyme / Snapdragon 870)

本开发分支正在完善小米10S移植，当前不具备正式安装条件。
请先看[推进计划](docs/PLAN.md)和[验收状态](docs/ACCEPTANCE.md)；下方原始移植记录不代表本机已经通过验收。

[中文说明](#中文说明) · [Status](#status) · [Architecture](#architecture) · [Repository layout](#repository-layout) · [Legal](#legal-and-attribution)

A port of [Hakios](https://github.com/DBC1349/hackios-mi10ultra) — unmodified iOS 16.5 userland
(launchd → backboardd → SpringBoard) on top of a self-built XNU — to a **Xiaomi Mi 10S**
(codename *thyme*, Qualcomm **Snapdragon 870**, SM8250-AC "kona", Adreno 650, UFS 3.1).

This repository is the **port's working record**: how the phone was brought up, the scripts that
build and deploy it, and — most importantly — an honest list of what works and what does not yet.

> iOS itself is **not** in this repository. The system image has to be built by whoever runs it,
> from Apple's own IPSW, on their own hardware (see [Legal](#legal-and-attribution)). The same
> goes for every vendor firmware blob: they are extracted from the phone's own Android
> partitions.

---

## 中文说明

这是一个把 **未改动的 iOS 16.5 用户态**（launchd → backboardd → SpringBoard）移植到
**小米 10S（thyme，骁龙 870 / SM8250-AC "kona"，Adreno 650，UFS 3.1）** 的项目，内核为自行构建的 XNU。

本仓库是**移植过程的工作记录**：启动链、构建/部署脚本，以及最重要的——**已实现和未实现目标的清单**。

> 仓库中**不包含** iOS 系统本体。系统镜像需要使用者自己用苹果官方 IPSW 在自己的设备上生成
>（见[法律与致谢](#legal-and-attribution)）。所有厂商固件（GPU/WiFi/音频等 blob）都从手机
> 自己的安卓分区提取。

---

## Status

### Working / 已实现

| Item | 项目 | Notes / 备注 |
|---|---|---|
| Boot chain | 启动链 | UEFI (Project Silicium on kona) → custom EFI loader → XNU 8796.121.2 (Darwin 22.5.0, iPhone10,3) |
| Touch | 触摸 | ST FTS521 over I2C, multi-touch, gestures, 0 errors in runs |
| GPU | GPU | Adreno 650 via Mesa freedreno FD650, GMU 587 MHz, hardware-rendered CoreAnimation |
| Display | 显示 | AMOLED, TE-synced 60.33 Hz, 7083 ns/line, 2340 lines; 60.2 fps measured with `cpufreq=1` |
| Screen off / buttons | 熄屏/按键 | PM8150 PON side button + volume keys, framebuffer power selector 12 |
| Wi-Fi | WiFi | QCA6390 over PCIe/MHI/QRTR/QMI → ath11k on FreeBSD LinuxKPI; scanning works (SCAN_REQ/SCAN_RESULT answered) |
| **Boot from the phone's own flash** | **手机自主启动** | UEFI + loader on `boot_b`; a 4 GiB `darwin` partition carved off `userdata` carries payload + 2.33 GiB system image; the loader reads them and boots — **no PC needed** |
| **Data persistence (save)** | **数据持久化（保存）** | `/private/var` packed and written into `darwin` at the end of a run (`var_store=1 ufs_write=1`); first successful save verified: 3 writes, 28 672 bytes, read back identical |
| Android kept intact | 安卓数据保留 | The carve keeps Android's `/data` (including a 3.3 GB Termux container), no repartition of other entries |
| A/B dual-boot wiring | A/B 双系统接线 | Android on slot `a` (`boot_a` untouched), iOS chain on slot `b` (`boot_b`) |

### Not yet working / 未实现（"TODO"）

| Item | 项目 | Current state / 现状 |
|---|---|---|
| **Third-party app installation** | **第三方应用安装** | A test app can be placed into `/Applications` via APFS synthetic entry + `bindfs` (verified: `sideload: Hello.app -> /Applications/Hello.app`), but the **boot then hangs** — Launch Services' scan of the bind-mounted bundle is suspected. Needs a Launch Services-friendly install (installd XPC with the stub-AMFI entitlement, or the base project's Sileo/rootless-JB path), not a raw bind mount. |
| **Persistence restore** | **持久化恢复** | The save works; the **load at boot hangs** when a store is present (init's `var_store load`). The store format/unpack path needs investigation. Erasing the two slot headers restores (empty) booting. |
| Android ⇄ iOS switching without a PC | 无电脑切换安卓/iOS | Today: `fastboot --set-active=a\|b` needs a PC. The `misc` BCB slot encoding has not been decoded yet; writing it from the loader/Android would make the switch self-service. |
| Full storage capacity display | 完整存储容量显示 | iOS "About" shows ~2 GB (the RAM-loaded system image). The base project presents a 128 GiB data volume via a bitmap block allocator; that has not been ported. |
| Phantom touches | 幻触 | Unescaped `0x23` motion events cause phantom touches; the three filters tried made the panel feel worse and were reverted. |
| Bluetooth | 蓝牙 | Not started (Wi-Fi shares the QCA6390 but Bluetooth needs its own PMU bring-up). |
| Sound / DSP | 音频 | Not part of this port's goals yet. |
| Post-run USB / reboot hygiene | 运行结束后的收尾 | The loader used to hang trying to bring USB up after the kernel returned; it now cold-resets instead (see `scripts/loader-autoboot.patch`). |

---

## Architecture

```
  Power on
     │
     ▼
  ABL (Qualcomm bootloader, A/B slots, unlocked)
     │  slot b
     ▼
  boot_b: UEFI (Project Silicium, kona drivers) + BootShim + XNUP payload
     │
     ▼
  xnuloader (the port's EFI loader)
     │   reads the `darwin` partition: header ("IOSSTOR1") → payload (kernel + small image + cmdline)
     │                                 → 2.33 GiB system image into RAM at 0xFC000000
     │   cmdline: ufs=2 ufs_write=1 var_store=1 …
     ▼
  XNU (self-built) → iOS userland (launchd, SpringBoard, …)

  Storage:
    userdata  ── shrunk by 4 GiB with f2fs resize (Android data kept)
    darwin    ── new partition: [4 KiB header][XNUP payload][2.33 GiB system image][var store: 2×512 MiB slots]
    boot_a    ── Android (untouched)          boot_b ── the iOS chain
```

### The two storage mechanisms

* **System image**: loaded into RAM by the loader at `0xFC000000` (same address the USB-based
  workflow used), so the kernel sees it as `md0` — the root.
* **Data volume**: `/private/var` is a RAM filesystem rebuilt at every boot. A saver
  (`var_store watch`, started from `/redwood/etc/rc` as root) registers with the kernel
  (`kern.redwood.var_save`); when the run ends the kernel asks it to pack `/private/var` and write
  it through `/dev/rufs_darwin` (the loader serves the writes, verifies each read-back) into the
  store's two alternating slots. At boot `init` runs `var_store load` before launchd.

---

## Repository layout

```
README.md                 this file (bilingual)
docs/
  ROADMAP.md              the TODO list, in more detail (bilingual)
scripts/
  loader-autoboot.patch   our loader changes: boot iOS from the phone's own `darwin` partition,
                          partition identity check, post-run reset
  build-loader.sh         build the patched loader and repack the boot image
  build-image.py          build the `darwin` partition image (header + payload + system)
  build-sideload.sh       build the app-placement test tool (static, mini libc)
  deploy.md               how the image and the chain get onto the phone
src/
  kernel/                 the port's kernel file overlay — the drivers (touch, display, GPU,
                          UFS, buttons, battery, Wi-Fi glue, …), the APFS/ramfs/bindfs work,
                          the stub AMFI, minicrypto, and bsd/conf/files        [APSL 2.0]
  loader/                 the XNUP loader source (base + this port's patch)   [MIT]
  userland/               var_store.c — the persistence saver, adapted       [MIT]
  sideload.c              the "install an app into /Applications" test tool   [MIT]
  kernel-patches/         the scripts that applied the last kernel/loader changes (record only)
NOTICE.md                 attribution and legal notes
LICENSE                   the split licence: MIT for the port content, APSL-2.0 for
                          src/kernel/ (see LICENSE.APSL)
```

## Building (outline)

1. **XNU**: the base project's build (`xnu-build.sh` with the port's file overlay) — the port's
   kernel sources live outside this repo (derived from Apple's XNU and the base project).
2. **Loader**: `scripts/build-loader.sh` — compiles `xnuloader/loader.c` (base project + our patch)
   with clang/lld and repacks the Android boot image.
3. **System image**: build the iOS rootfs from Apple's IPSW with the base project's userland build,
   then `scripts/build-image.py` wraps it: `[header][XNUP payload][system image]`.
4. **Phone**: shrink `userdata` by 4 GiB (f2fs resize, keep Android data), carve a `darwin`
   partition (GPT with CRCs), write the image, flash the chain to `boot_b`, set the active slot.
   The steps are in `scripts/deploy.md`; the exact values this port used are in the commit log and
   `docs/ROADMAP.md`.

## Legal and attribution

* **No Apple code or binaries** are distributed here. The iOS system image is Apple's copyrighted
  software; each user builds it from an IPSW they obtained themselves, for their own device.
* **No vendor firmware** is distributed. GPU zap shader, Wi-Fi firmware, etc. are extracted from
  the user's own phone's Android partitions at build time.
* The kernel work is a derivative of **Apple's XNU** (APSL 2.0) and of the base project
  **[DBC1349/hackios-mi10ultra](https://github.com/DBC1349/hackios-mi10ultra)**: the port's file
  overlay, the loader and the userland build come from there. The Wi-Fi stack runs **Linux
  ath11k** (GPL-2.0) through a FreeBSD LinuxKPI layer.
* **Reference port — [azzlantern/hackios-thyme](https://github.com/azzlantern/hackios-thyme)**:
  the driver approach followed it. In particular the QCA6390 Wi-Fi bring-up
  (PCIe → MHI → QRTR → QMI/WLFW → ath11k on LinuxKPI, with its own `umi_*` glue and `lx_*`
  layer) and the boot/storage workflow ideas come from reading that project's code; the sources
  were audited before use and then adapted to this device (kona), with the port's own names
  (`redwood_*`). Its Apple/vendor binaries are not redistributed here either.
* This is a research/hobby project for hardware you own. Do not use it to circumvent Apple's
  software licensing.
