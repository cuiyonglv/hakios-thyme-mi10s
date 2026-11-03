#!/usr/bin/env python3
"""Test the production Block I/O write gate in both loader build profiles.

Also audit that this loader's direct write calls go only through that gate.
Does not cover UEFI firmware internals or independent kernel write paths.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/loader/loader.c').read_text()
start = source.index('static efi_status\nstorage_write_blocks(')
function = source[start:source.index('\n/* ------------------------------------------------------------------------ */', start)]
if source.count('->write_blocks(') != 1 or function.count('->write_blocks(') != 1:
    raise SystemExit('A direct loader write bypasses the central storage policy')
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define EFI_ERROR_BIT (1ULL << 63)
typedef uint64_t efi_status;
typedef struct efi_block_io efi_block_io;
struct efi_block_io {
    void *media;
    efi_status (*write_blocks)(efi_block_io *, uint32_t, uint64_t, uint64_t, void *);
};
static unsigned writes;
static uint8_t disk[4096];
static efi_status fake_write(efi_block_io *bio, uint32_t media_id, uint64_t lba,
    uint64_t length, void *data) {
    assert(bio && media_id == 7 && lba == 2 && length == sizeof(disk));
    ++writes;
    memcpy(disk, data, length);
    return 0;
}
'''
suffix = r'''
int main(void) {
    uint8_t data[4096];
    memset(data, 0x5a, sizeof(data)); memset(disk, 0xa5, sizeof(disk));
    efi_block_io io = {&io, fake_write};
    efi_status status = storage_write_blocks(&io, 7, 2, sizeof(data), data);
    if (THYME_STORAGE_WRITES) {
        assert(status == 0 && writes == 1 && memcmp(disk, data, sizeof(disk)) == 0);
    } else {
        assert(status == (EFI_ERROR_BIT | 15) && writes == 0);
        for (unsigned i = 0; i < sizeof(disk); ++i) assert(disk[i] == 0xa5);
    }
    unsigned before = writes;
    assert(storage_write_blocks(NULL, 7, 2, sizeof(data), data) & EFI_ERROR_BIT);
    io.media = NULL;
    assert(storage_write_blocks(&io, 7, 2, sizeof(data), data) & EFI_ERROR_BIT);
    io.media = &io; io.write_blocks = NULL;
    assert(storage_write_blocks(&io, 7, 2, sizeof(data), data) & EFI_ERROR_BIT);
    assert(writes == before);
    printf("PASS: direct storage write policy=%d and invalid protocol rejection\n", THYME_STORAGE_WRITES);
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-write-policy-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'policy.c'
    cfile.write_text(prefix + function + suffix)
    for writable in (0, 1):
        binary = directory / f'policy-{writable}'
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                        '-fsanitize=address,undefined', f'-DTHYME_STORAGE_WRITES={writable}',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=20)
