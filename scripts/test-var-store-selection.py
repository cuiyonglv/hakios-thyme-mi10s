#!/usr/bin/env python3
"""Host tests of production slot selection and restore fallback, with mock unpack.

CRC/header routines are real; the mock pass verifies tiny payloads and records
which generation would be applied. This is not a filesystem restore test.
"""
from pathlib import Path
import os
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'src/userland/var_store.c').read_text()
structures = source[source.index('struct head {'):source.index('/* ---- the kernel\'s side')]
checksums = source[source.index('static uint32_t crc_table'):source.index('/* ---- the store, as a stream')]
selectors = source[source.index('static int\nread_head('):source.index('static uint64_t last_saved_at')]
restore = source[source.index('static int\nload(void)'):source.index('static void\nshow(void)')]
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#define BLOCK 4096
#define SLOTS 2
#define SLOT_BYTES 0x20000000LL
#define HEAD_MAGIC "RDWVAR\0"
#define HEAD_VERSION 1
'''
mocks = r'''
static uint8_t heads[SLOTS][BLOCK], payloads[SLOTS][96];
static uint64_t applied, made_dirs, made_files, made_bytes, made_links, short_files, lost_bytes;
static int apply_error;
static off_t slot_at(int slot) { return slot * SLOT_BYTES; }
static int device_read(off_t where, void *into, uint64_t length) {
    int slot = (int)(where / SLOT_BYTES);
    assert(slot >= 0 && slot < SLOTS && length == BLOCK);
    memcpy(into, heads[slot], length); return 1;
}
static int pass(const struct head *h, int make) {
    if (h->bytes != sizeof(payloads[0]) ||
        (crc_add(0xffffffffu, payloads[h->slot], h->bytes) ^ 0xffffffffu) != h->crc) return 0;
    if (make) {
        if (apply_error) return 0;
        applied = h->generation;
    }
    return 1;
}
static void fill(int slot, uint64_t generation) {
    struct head h = {0};
    memset(payloads[slot], 0x30 + slot, sizeof(payloads[slot]));
    memcpy(h.magic, HEAD_MAGIC, sizeof(h.magic));
    h.version = 1; h.slot = slot; h.generation = generation;
    h.bytes = sizeof(payloads[slot]); h.entries = 2;
    h.crc = crc_add(0xffffffffu, payloads[slot], h.bytes) ^ 0xffffffffu;
    h.head_crc = head_checksum(&h);
    memcpy(heads[slot], &h, sizeof(h));
}
'''
suffix = r'''
int main(void) {
    struct head h;
    crc_init();
    assert(load() == 1 && applied == 0);
    fill(0, 1); fill(1, 2);
    assert(newest_verified(&h) == 1 && h.generation == 2);
    assert(load() == 1 && applied == 2);
    /* Newer header remains valid but its payload was torn. */
    payloads[1][0] ^= 0xff;
    assert(newest(&h) == 1);
    assert(newest_verified(&h) == 0);
    assert((newest_verified(&h) + 1) % SLOTS == 1); /* next save preserves slot 0 */
    applied = 0;
    assert(load() == 1 && applied == 1);
    payloads[0][0] ^= 0xff;
    applied = 0;
    assert(newest_verified(&h) == -1);
    assert(load() == 1 && applied == 0);
    /* An unfinished save cleared the destination header, not the old one. */
    fill(0, 3); memset(heads[1], 0, BLOCK);
    assert(newest_verified(&h) == 0 && h.generation == 3);
    assert(load() == 1 && applied == 3);
    /* Good payload, impossible entry count, recomputed header checksum. */
    fill(1, 4);
    memcpy(&h, heads[1], sizeof(h));
    h.entries = UINT64_MAX; h.head_crc = head_checksum(&h);
    memcpy(heads[1], &h, sizeof(h));
    assert(!read_head(1, &h));
    assert(newest_verified(&h) == 0);
    apply_error = 1; applied = 0;
    assert(load() == 0 && applied == 0);
    puts("PASS: empty/torn/corrupt slots, fallback, interrupted save, bad counts, apply failure");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-var-selection-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'selection.c'
    binary = directory / 'selection'
    cfile.write_text(prefix + structures + checksums + mocks + selectors + restore + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=20)
