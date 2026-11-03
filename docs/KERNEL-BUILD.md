# Native IIG and the thyme kernel export contract

This continues the existing patched XNU 8796.121.2 Linux build. It is not a
complete toolchain installer or a phone flashing procedure. Apply this port's
kernel overlay and the reviewed donor patches before these steps.

Use the committed native Apple IIG output instead of the old SDK-header/empty-TU
shim. Point make's IIG at:

```sh
IIG="python3 /absolute/port/scripts/cached-apple-iig.py"
```

On migration from a shim build, regenerate **all 17** DriverKit units and paired
headers, not just OSObject. Touching the original `.iig` input timestamps forces
normal make regeneration without changing their bytes. The cache adapter checks
the complete definition set. Old stand-in OSObject code must not be linked along
with the real generated implementations.

Linux also needs a seekable discard output for Apple's original symbol checker:

```sh
export KEXTSYMBOLTOOL_REAL="$XNU_SRC/BUILD/obj/SETUP/kextsymboltool/kextsymboltool"
# Pass this single executable path as a make variable, not a multiword command:
KEXT_CREATE_SYMBOL_SET=/absolute/port/scripts/linux-kextsymboltool.py
```

The wrapper only substitutes a private regular file for `-output /dev/null`.
It preserves the original checker and its failure status. With native IIG, the
first full build produces the linked kernel and a real `config/allsymbols`, then
fails on exactly 35 Apple-only Private.arm64 declarations under the original
Apple hardware contract. Any other failure must be fixed separately.

Prepare the explicit target contract:

```sh
python3 /absolute/port/scripts/prepare-thyme-exports.py \
  --target thyme --source "$XNU_SRC/config/Private.arm64.exports" \
  --symbols "$XNU_OBJ/config/allsymbols" --out-dir /new/thyme-contract
```

Review `profile.json` and preserve `Private.arm64.apple.exports`. Select the
candidate `Private.arm64.exports` in this **thyme build tree**, then run the
original complete make with the same IIG/checker settings. Do not reuse this
profile for another target. Re-running preparation after selection requires the
preserved original as the source; the adapter intentionally rejects an already
filtered input. This is a fixed hardware contract, not an ignore-missing switch.

Actual validation: low-priority `-j2` make with clang 18, cctools ld64 and
`DSYMUTIL=/usr/bin/dsymutil-18` exits zero. Native IIG compiled 17/17 source units;
all normal symbol sets and export checks ran. The target audit has 22,784 defined
symbols, 5,603 checked declarations and no missing entries. The original contract
is retained with its 35 unavailable Apple-only interfaces recorded separately:

- [Original contract audit](reports/kernel-exports.json)
- [Selected thyme contract audit](reports/kernel-exports-thyme.json)
- [Exact selection and input hashes](reports/thyme-export-profile.json)
- [Native IIG input hashes](reports/iig-native-input-files.json)
- [System extension static imports](reports/system-extension-imports.json)
- [Actual clean-cohort build record](reports/kernel-build.json)

The clean source checkout is port revision
`4fece7851ba07d40958024cfbe231c87fccd9b8d`. All 138 kernel overlay files and all
34 generated IIG source/header files match the build tree byte for byte. The
one old CRLF-only touch source was aligned before rebuilding. The record binds
the 5,115-file source manifest, selected contract, compiler wrapper, link flags,
complete make log and both output hashes. Both outputs are arm64 MH_EXECUTE with
MH_NOUNDEFS, and `llvm-nm -uj` reports no undefined imports.

The optional external ath11k object is **absent** in this build. That does not
certify the radio path as usable; review the phone's actual radio, firmware and
driver configuration before enabling it. Hardware mapping for touch, GPU and
other peripherals also still requires the phone's own firmware/device tree.

This is build acceptance for the selected target contract. Device firmware,
recovery, image composition and live phone functionality remain separate gates.
