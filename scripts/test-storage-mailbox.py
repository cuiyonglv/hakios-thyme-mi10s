#!/usr/bin/env python3
"""Exercise the actual mailbox read function under concurrent first reads.

Host-only mocks cover locking, allocation failure, aligned/unaligned reads and
the native-controller exclusion. No device or firmware is accessed.
"""
from pathlib import Path
import os
import resource
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = (root / 'src/kernel/bsd/dev/redwood_ufs.c').read_text()
start = source.index('static int\nufs_read_mailbox(')
end = source.index('\n/* The same, through', start)
function = source[start:end]

prefix = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <unistd.h>
#define Z_WAITOK 0
#define BLOCK_OP_READ 1
static pthread_mutex_t ufs_lock = PTHREAD_MUTEX_INITIALIZER;
#define lck_mtx_lock pthread_mutex_lock
#define lck_mtx_unlock pthread_mutex_unlock
static struct { struct { uint64_t block_size; char name[16]; } part[2]; } page;
static __typeof__(page) *ufs_page = &page;
static uint64_t ufs_buffer_size = 8192;
static unsigned ufs_mailbox_refused;
static bool native;
static atomic_int allocations, requests;
static bool fail_allocation = true;
static bool ufshc_list_in(void) { return native; }
static void *kalloc_data(size_t size, int flags) {
    (void)flags;
    if (fail_allocation) { fail_allocation = false; return NULL; }
    atomic_fetch_add(&allocations, 1);
    usleep(50000); /* make two unsynchronized initial allocations overlap */
    return malloc(size);
}
static int ufs_request(int op, uint32_t part, uint64_t block,
    uint64_t count, void *dst, void *unused) {
    (void)op; (void)block; (void)unused;
    atomic_fetch_add(&requests, 1);
    memset(dst, 0x40 + part, count * page.part[part].block_size);
    return 0;
}
'''
suffix = r'''
static pthread_barrier_t begin;
static void *reader(void *arg) {
    uint32_t part = (uint32_t)(uintptr_t)arg;
    unsigned char data[3];
    pthread_barrier_wait(&begin);
    assert(ufs_read_mailbox(part, 4095, data, sizeof(data)) == 0);
    for (unsigned i = 0; i < sizeof(data); ++i) assert(data[i] == 0x40 + part);
    return NULL;
}
int main(void) {
    unsigned char data[4096];
    page.part[0].block_size = page.part[1].block_size = 4096;
    assert(ufs_read_mailbox(0, 1, data, 1) == ENOMEM);
    assert(pthread_mutex_trylock(&ufs_lock) == 0);
    pthread_mutex_unlock(&ufs_lock);
    pthread_t threads[2];
    pthread_barrier_init(&begin, NULL, 2);
    for (uintptr_t p = 0; p < 2; ++p)
        assert(pthread_create(&threads[p], NULL, reader, (void *)p) == 0);
    for (unsigned p = 0; p < 2; ++p) pthread_join(threads[p], NULL);
    assert(atomic_load(&allocations) == 1);
    assert(ufs_read_mailbox(1, 0, data, sizeof(data)) == 0);
    for (unsigned i = 0; i < sizeof(data); ++i) assert(data[i] == 0x41);
    native = true;
    int before = atomic_load(&requests);
    assert(ufs_read_mailbox(0, 1, data, 1) == EIO);
    assert(atomic_load(&requests) == before);
    assert(pthread_mutex_trylock(&ufs_lock) == 0);
    pthread_mutex_unlock(&ufs_lock);
    puts("PASS: concurrent first reads, block crossing, alignment, ENOMEM, native exclusion");
}
'''

def run(code, directory, name):
    cfile = directory / (name + '.c')
    binary = directory / name
    cfile.write_text(prefix + code + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-pthread',
                    '-O1', str(cfile), '-o', str(binary)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)

with tempfile.TemporaryDirectory(prefix='thyme-mailbox-') as temp:
    directory = Path(temp)
    result = run(function, directory, 'fixed')
    print(result.stdout, end='')
    if result.returncode:
        raise SystemExit(result.stderr)
    # The pre-fix ordering must fail the concurrent-first-allocation assertion.
    old = function.replace('\tlck_mtx_lock(&ufs_lock);\n', '', 1)
    old = old.replace('\t\t\tlck_mtx_unlock(&ufs_lock);\n', '', 1)
    old = old.replace('\tif (ufshc_list_in()) {',
                      '\tlck_mtx_lock(&ufs_lock);\n\tif (ufshc_list_in()) {', 1)
    regression = run(old, directory, 'before_fix')
    detected = 'allocations' in regression.stderr or 'data[i]' in regression.stderr
    if regression.returncode == 0 or not detected:
        raise SystemExit('Regression harness did not detect the old allocation race: ' + regression.stderr)
    print('PASS: harness detects the original first-allocation race')
