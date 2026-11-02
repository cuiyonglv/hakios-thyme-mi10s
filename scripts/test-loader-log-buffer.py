#!/usr/bin/env python3
"""Host test: actual write_logs must preserve an outstanding mailbox reply."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/loader/loader.c').read_text()
start = source.index('static void\nwrite_logs(')
end = source.index('\nstatic void\nflush_log(', start)
function = source[start:end]
prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t efi_status;
#define EFI_ERROR(s) ((s) != 0)
typedef struct efi_block_io efi_block_io;
struct media { uint32_t block_size, media_id; } media = {4096, 1};
struct efi_block_io {
    struct media *media;
    efi_status (*write_blocks)(efi_block_io *, uint32_t, uint64_t, uint64_t, void *);
    efi_status (*read_blocks)(efi_block_io *, uint32_t, uint64_t, uint64_t, void *);
    void (*flush_blocks)(efi_block_io *);
};
static char out_buf[2 * 1024 * 1024];
static uint8_t block_buffer[4u << 20];
static uint8_t log_verify[sizeof(out_buf) + 8192];
static uint8_t disk[sizeof(log_verify)];
static uint64_t staged_len;
static unsigned hc_log_skips, writes, reads;
static bool uefi_list = true, partition = true;
static efi_status write_blocks(efi_block_io *b, uint32_t id, uint64_t lba,
    uint64_t len, void *data) {
    (void)b; (void)id; assert(lba == 0 && len <= sizeof(disk));
    memcpy(disk, data, len); ++writes; return 0;
}
static efi_status read_blocks(efi_block_io *b, uint32_t id, uint64_t lba,
    uint64_t len, void *data) {
    (void)b; (void)id; assert(lba == 0 && len <= sizeof(disk));
    memcpy(data, disk, len); ++reads; return 0;
}
static void flush_blocks(efi_block_io *b) { (void)b; }
static efi_block_io io = {&media, write_blocks, read_blocks, flush_blocks};
static efi_block_io *find_log_partition(void) { return partition ? &io : NULL; }
static void compose_logs(const char *log, uint64_t len) {
    assert(len <= sizeof(out_buf)); memset(out_buf, 0, sizeof(out_buf));
    if (len) memcpy(out_buf, log, len); staged_len = len;
}
static int hc_list_is_uefis(void) { return uefi_list; }
static void hc_print_state(const char *s) { (void)s; }
static void print(const char *s, ...) { (void)s; }
static void put_char(char c) { (void)c; }
static void flush_dcache(uint64_t start, uint64_t end) { assert(end >= start); }
'''
suffix = r'''
int main(void) {
    memset(block_buffer, 0xa5, sizeof(block_buffer));
    write_logs("boot log", 8);
    assert(writes == 1 && reads == 1 && memcmp(disk, "boot log", 8) == 0);
    for (unsigned i = 0; i < sizeof(block_buffer); ++i)
        assert(block_buffer[i] == 0xa5);
    uefi_list = false;
    write_logs("native controller owns the list", 31);
    assert(writes == 1 && reads == 1);
    partition = false;
    write_logs("RAM session", 11);
    assert(writes == 1 && reads == 1);
    puts("PASS: log verification preserves mailbox; native ownership and no-partition paths");
}
'''

def run(code, directory, name):
    cfile = directory / (name + '.c')
    binary = directory / name
    cfile.write_text(prefix + code + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    str(cfile), '-o', str(binary)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)

with tempfile.TemporaryDirectory(prefix='thyme-log-buffer-') as temp:
    directory = Path(temp)
    result = run(function, directory, 'fixed')
    print(result.stdout, end='')
    if result.returncode:
        raise SystemExit(result.stderr)
    regression = run(function.replace('log_verify', 'block_buffer'), directory, 'before_fix')
    if regression.returncode == 0 or 'block_buffer' not in regression.stderr:
        raise SystemExit('Regression harness did not detect the original mailbox clobber')
    print('PASS: harness detects the original shared-buffer corruption')
