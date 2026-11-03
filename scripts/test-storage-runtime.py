#!/usr/bin/env python3
"""Native tests of mailbox ABI, consumed loader lease and saver layout negotiation."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
loader = (root / 'src/loader/loader.c').read_text()
kernel = (root / 'src/kernel/bsd/dev/redwood_ufs.c').read_text()
saver = (root / 'src/userland/var_store.c').read_text()


def structures(source, suffix):
    start = source.index('struct loader_block_part {')
    end = source.index('\n_Static_assert(', start)
    return source[start:end].replace('loader_block_part', 'block_part_' + suffix).replace(
        'struct loader_block {', 'struct block_' + suffix + ' {')


lease = loader[loader.index('static void\nblock_storage_begin_run('):loader.index('static void\nblock_start(')]
negotiate = saver[saver.index('static int\nstorage_layout_init('):saver.index('/* ---- checksum')]
import_layout = kernel[kernel.index('static void\nufs_storage_import('):kernel.index('void\nredwood_ufs_init(')]
assert '#define BLOCK_VERSION      THYME_BLOCK_PROTOCOL_VERSION' in loader
assert '#define BLOCK_VERSION      THYME_BLOCK_PROTOCOL_VERSION' in kernel
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "thyme_storage_layout.h"
#define BLOCK_MAX_PARTS 160
#define BLOCK_PART_UNIT 1
typedef struct {int dummy;} efi_block_io;
static struct thyme_store_info pending_storage,run_storage,offered;
static efi_block_io *pending_storage_bio,*run_storage_bio;
static int block_enabled,block_write_requested,block_write_part;
static uint32_t block_part_count;
static uint64_t storage_offset,storage_slot_bytes;
static int query_error,wrong_size;
static int sysctlbyname(const char *name,void *out,size_t *size,void *in,size_t in_size){
    assert(!strcmp(name,"kern.redwood.var_layout")&&*size==sizeof(offered)&&!in&&!in_size);
    if(query_error)return -1;
    memcpy(out,&offered,sizeof(offered));if(wrong_size)--*size;return 0;
}
'''
suffix = r'''
int main(void){
    assert(THYME_BLOCK_PROTOCOL_VERSION==4);
    assert(thyme_block_protocol_valid(4,sizeof(struct block_kernel),sizeof(struct block_kernel)));
    assert(!thyme_block_protocol_valid(3,sizeof(struct block_kernel),sizeof(struct block_kernel)));
    assert(!thyme_block_protocol_valid(5,sizeof(struct block_kernel),sizeof(struct block_kernel)));
    assert(!thyme_block_protocol_valid(4,sizeof(struct block_kernel)-1,sizeof(struct block_kernel)));
    assert(sizeof(struct block_loader)==sizeof(struct block_kernel));
    assert(offsetof(struct block_loader,ask)==128&&offsetof(struct block_kernel,ask)==128);
    assert(offsetof(struct block_loader,storage)==48&&offsetof(struct block_kernel,storage)==48);
    assert(offsetof(struct block_loader,storage_part_plus_one)==104);
    assert(offsetof(struct block_loader,part)==offsetof(struct block_kernel,part));
    assert(offsetof(struct block_loader,native)==offsetof(struct block_kernel,native));
    assert(offsetof(struct block_loader,kernel_native)==offsetof(struct block_kernel,kernel_native));
    offered=(struct thyme_store_info){2,0,9000,4u<<20,{12288,65536,2,4096,0}};
    assert(thyme_store_info_valid(&offered));
    struct block_kernel page={0};ufs_page=&page;ufs_parts=2;
    page.storage=offered;page.storage_part_plus_one=1;
    strcpy(page.part[0].name,"darwin");page.part[0].blocks=offered.partition_bytes/4096;
    page.part[0].block_size=4096;
    ufs_storage_import(1,1);assert(ufs_store_part==0&&ufs_store_info.version==2);
    ufs_storage_import(1,2);assert(ufs_store_part==-1&&!ufs_store_info.version);
    ufs_storage_import(0,1);assert(ufs_store_part==-1);
    page.part[0].block_size=512;ufs_storage_import(1,1);assert(ufs_store_part==-1);page.part[0].block_size=4096;
    page.part[0].flags=1;ufs_storage_import(1,1);assert(ufs_store_part==-1);page.part[0].flags=0;
    page.part[0].blocks++;ufs_storage_import(1,1);assert(ufs_store_part==-1);page.part[0].blocks--;
    page.storage_part_plus_one=2;ufs_storage_import(1,1);assert(ufs_store_part==-1);
    page.storage_part_plus_one=3;ufs_storage_import(1,1);assert(ufs_store_part==-1);
    page.storage_part_plus_one=1;ufs_storage_import(1,1);assert(ufs_store_part==0);
    uint64_t end=offered.layout.store_at+2*offered.layout.slot_bytes;
    assert(thyme_store_write_valid(&offered,12288,4096));
    assert(thyme_store_write_valid(&offered,end-4096,4096));
    assert(!thyme_store_write_valid(&offered,8192,4096));
    assert(!thyme_store_write_valid(&offered,end-4096,8192));
    assert(!thyme_store_write_valid(&offered,end,4096));
    assert(!thyme_store_write_valid(&offered,12288,0));
    assert(!thyme_store_write_valid(&offered,12289,4096));
    assert(!thyme_store_write_valid(&offered,UINT64_MAX-4095,8192));
    efi_block_io io;
    pending_storage=offered;pending_storage_bio=&io;block_enabled=block_write_requested=1;block_write_part=4;
    block_storage_begin_run();
    assert(run_storage_bio==&io&&run_storage.layout.store_at==12288);
    assert(pending_storage.version==0&&pending_storage_bio==NULL);
    assert(!block_enabled&&!block_write_requested&&block_write_part==-1);
    block_storage_begin_run();assert(!run_storage_bio&&!run_storage.version);
    pending_storage=offered;pending_storage.version=99;pending_storage_bio=&io;
    block_storage_begin_run();assert(!run_storage_bio&&!run_storage.version);
    assert(storage_layout_init()&&storage_offset==12288&&storage_slot_bytes==65536);
    query_error=1;assert(!storage_layout_init()&&!storage_offset&&!storage_slot_bytes);query_error=0;
    wrong_size=1;assert(!storage_layout_init()&&!storage_offset);wrong_size=0;
    offered.version=99;assert(!storage_layout_init());
    offered.version=1;assert(!storage_layout_init()); /* no old-offset guessing */
    offered.layout=(struct thyme_store_layout){THYME_STORE_AT,THYME_STORE_SLOT_BYTES,2,4096,0};
    offered.partition_bytes=4ULL<<30;assert(storage_layout_init());assert(storage_offset==THYME_STORE_AT);
    offered.reserved=1;assert(!storage_layout_init());
    puts("PASS: v4 mailbox ABI, dynamic write bounds, per-run lease reset, validated saver negotiation and no legacy fallback");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-runtime-layout-') as temporary:
    directory = Path(temporary)
    cfile, binary = directory / 'runtime.c', directory / 'runtime'
    kernel_state = '''static volatile struct block_kernel *ufs_page;
static uint32_t ufs_parts;
static struct thyme_store_info ufs_store_info;
static int ufs_store_part;
'''
    cfile.write_text(prefix + structures(loader, 'loader') + structures(kernel, 'kernel') +
                     kernel_state + import_layout + lease + negotiate + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', '-I', str(root / 'src/kernel/bsd/sys'),
                    str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=20)
