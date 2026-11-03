# Official thyme firmware analysis

The official OS1.0.4.0.TGACNXM fastboot archive has downloaded completely:
6,031,400,362 bytes, MD5 `778cda43017aacde0d7a04ff85b872f8`. It was checked again
before selected images were extracted locally. No flash scripts were executed,
and no vendor images or firmware blobs are uploaded to this repository.

This matches the last observed Android release, not a fresh live comparison:
the phone currently does not enumerate in ADB, Fastboot or Windows Xiaomi USB
device discovery. A package is not a backup of the phone's unique partitions.

Official AOSP unpack_bootimg at revision
`6cb944b82c819b691bc21d8228e155f1db07a446` identifies boot.img as Android header
v3, Android 13.0.0, patch level 2024-04. vendor_boot.img is v3 and contains
1,613,832 bytes of concatenated device trees. The stock DTBO has 29 entries;
entry 5 identifies `Qualcomm Technologies, Inc. xiaomi thyme`, board ID `0x2d 0`.

For offline analysis, applying that overlay to the first kona v2.1 base with
dtc/fdtoverlay confirms:

| Component | Stock property |
| --- | --- |
| GPU zap reservation | `pil_gpu_region@8c41a000`, size `0x2000` |
| Touch | `/soc/i2c@a94000/fts@49`, `st,fts` |
| Touch firmware | `st_fts_j2s_default.ftb`; configuration `st_fts_j2s.ftb` |
| Touch dimensions | 1080 × 2340 |
| WiFi | `qcom,cnss-qca6390`; PCIe root-complex index 0 |

That analysis merge is not the phone's live, bootloader-patched tree and is not
an approved boot input. The base/overlay selection still needs live confirmation.

The FAT firmware image contains QCA6390 `amss20.bin`, `m3.bin`, `regdb.bin` and
both J1 and J2S board-data files. An actual source defect was found: the port's
firmware mapper still selected Mi 10 UMI's `bd_j1gl.elf` for the thyme target.
Xiaomi's [CNSS selection code](https://github.com/MiCode/Xiaomi_Kernel_OpenSource/blob/2fdca387269615872d4ff798e8da7d33345c1338/drivers/net/wireless/cnss2/qmi.c#L525)
selects `bd_j2s.elf` for `HARDWARE_PLATFORM_THYME` in the default ELF-BDF path.
The mapper now selects that file and refuses a missing file without falling back
to another phone's board data. The host test runs the actual C mapper and rejects
the old UMI selection. This does not prove that the radio boots or joins a network.

Next: stage the correct firmware into the matching system image, build/audit the
optional ath11k adaptation, review the phone-specific UEFI drivers, and obtain
the connected phone's current firmware/unique-partition backups before an iOS boot
or storage experiment.
