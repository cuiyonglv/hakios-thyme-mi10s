This is the explicit export contract for **Qualcomm thyme**, XNU 8796.121.2.
It does not provide Apple's SoC-specific driver ABI.

The original Apple contract is retained verbatim in `Private.arm64.apple.exports`
at SHA-256 `00e242d4bb226e76c5d759be06606722bcd006dba267cfc5b5e42a0b44ea9dc9`.
Source: [Apple XNU](https://github.com/apple-oss-distributions/xnu/blob/c54f35ca767986246321eb901baf8f5ff7923f6a/config/Private.arm64.exports),
under the [Apple Public Source License](../apple-iig-8796/APPLE_LICENSE).
The target selection is independently described by `profile.json`.

| Unavailable interface group | Declarations | Reason |
| --- | ---: | --- |
| Apple pmap IOMMU | 7 | No Apple protected IOMMU implementation in this target; original same-tag source is a placeholder |
| Apple NVMe PPL descriptor | 1 | Qualcomm UFS target does not supply Apple's NVMe protected mapping descriptor |
| Apple SART and T8020/T6000/T8110 DART | 9 | Apple SoC hardware and its implementation are absent on kona |
| Apple UAT and IOUnifiedAddressTranslator | 18 | Apple unified address translator is not the custom Adreno path |

This does **not** remove Qualcomm UFS, GPU, WiFi or their implementations, and
does not add fake-success functions. Clients importing these Apple interfaces
remain unsupported and must fail linking/loading rather than receive fake mappings.
The two previously repaired non-monitor pmap capability queries remain exported.

`scripts/prepare-thyme-exports.py` requires the exact original input hash, explicit
`--target thyme`, and a real linked symbol list with exactly these missing entries.
It rejects new unrelated missing entries, changed contracts, newly provided listed
interfaces and existing output directories. It writes a new candidate directory
containing the selected table, preserved original table and before/after record.
Nothing is installed automatically. The ordinary XNU symbol-set checks still run
after selecting the candidate in the target build tree.

The core relink has no undefined imports. The actual system APFS inventory has
62,355 entries; all 96 regular files under System/Library/Extensions were inspected.
The 11 Mach-O files include five MH_KEXT_BUNDLE files; none imports a selected-out
interface. See [the image-bound static scan](../../docs/reports/system-extension-imports.json).
This does not prove that those extensions load, or that all other imports match.
The loader's XNUP payload contains a kernel, ramdisk and command line; it does not
load an Apple kernel collection to supply the absent hardware implementations.

The original full-contract audit still records 35 missing declarations. The
selected-target audit checks 5,603 declarations and reports zero missing entries.
Full target make exits zero with the original checks enabled. These build results
do not establish phone boot, DMA correctness, security isolation or stable iOS use.
