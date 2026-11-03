#!/usr/bin/env python3
"""Production block_poll on a fake EFI disk; native cache/fence timing is not modeled."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/loader/loader.c').read_text()
begin = source.index('struct loader_block_part {')
structures = source[begin:source.index('\n_Static_assert(', begin)]
begin = source.index('static void\nblock_poll(')
poll = source[begin:source.index('\n}\n', begin)+3]
poll = poll.replace('__asm__ volatile ("dmb sy");', '__sync_synchronize();')
begin = source.index('static efi_status\nstorage_write_blocks(')
write_gate = source[begin:source.index('\n/* ------------------------------------------------------------------------ */', begin)]
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "thyme_storage_layout.h"
#define BLOCK_MAX_PARTS 160
#define BLOCK_BUFFER_SIZE 16384
#define BLOCK_OP_READ 0
#define BLOCK_OP_WRITE 1
#define THYME_STORAGE_WRITES 1
#define EFI_ERROR_BIT (1ULL<<63)
#define EFI_ERROR(s) (((s)&EFI_ERROR_BIT)!=0)
#define READ_SYSREG(r) 1ULL
typedef uint64_t efi_status;
typedef struct efi_block_io efi_block_io;
struct media {uint32_t block_size,media_id;uint64_t last_block;};
struct efi_block_io {
    struct media *media;
    efi_status (*write_blocks)(efi_block_io *,uint32_t,uint64_t,uint64_t,void *);
    efi_status (*read_blocks)(efi_block_io *,uint32_t,uint64_t,uint64_t,void *);
    efi_status (*flush_blocks)(efi_block_io *);
};
static unsigned writes,reads;static int flush_error,read_corrupt,native_in;
static uint8_t disk[262144],block_buffer[BLOCK_BUFFER_SIZE];
static void print(const char *format,...){(void)format;}
static void flush_dcache(uint64_t a,uint64_t b){}
static void clean_dcache(uint64_t a,uint64_t b){}
static int hc_list_is_uefis(void){return !native_in;}
static unsigned hc_refused_reads;
static uint32_t ufs_crc32(const void *p,uint64_t n){
    const uint8_t *b=p;uint32_t c=2166136261u;while(n--)c=(c^*b++)*16777619u;return c;
}
static efi_status wr(efi_block_io *io,uint32_t id,uint64_t lba,uint64_t bytes,void *data){
    assert(lba*io->media->block_size+bytes<=sizeof(disk));writes++;
    memcpy(disk+lba*io->media->block_size,data,bytes);return 0;
}
static efi_status rd(efi_block_io *io,uint32_t id,uint64_t lba,uint64_t bytes,void *data){
    assert(lba*io->media->block_size+bytes<=sizeof(disk));reads++;
    memcpy(data,disk+lba*io->media->block_size,bytes);if(read_corrupt)((uint8_t *)data)[0]^=1;return 0;
}
static efi_status flush(efi_block_io *io){return flush_error?EFI_ERROR_BIT|7:0;}
'''
state = r'''
static struct loader_block block_shared;
static efi_block_io *block_bios[BLOCK_MAX_PARTS],*run_storage_bio;
static uint32_t block_part_count=1;
static int block_write_part=0;
static struct thyme_store_info run_storage;
static uint64_t block_writes,block_writes_refused,block_write_bytes;
'''
suffix = r'''
static int request(uint32_t part,uint64_t lba,uint64_t count,uint32_t op){
    memset(block_buffer,0xa5,sizeof(block_buffer));
    block_shared.ask.part=part;block_shared.ask.lba=lba;block_shared.ask.count=count;block_shared.ask.op=op;
    ++block_shared.ask.request;block_poll();
    assert(block_shared.answer.answered==block_shared.ask.request);
    return !EFI_ERROR((uint64_t)block_shared.answer.status);
}
int main(void){
    struct media media={4096,1,sizeof(disk)/4096-1};
    efi_block_io io={&media,wr,rd,flush};block_bios[0]=run_storage_bio=&io;
    run_storage=(struct thyme_store_info){2,0,9000,sizeof(disk),{12288,65536,2,4096,0}};
    block_shared.parts=1;block_shared.part[0].block_size=4096;block_shared.part[0].blocks=sizeof(disk)/4096;
    assert(request(0,3,1,BLOCK_OP_WRITE)&&writes==1&&block_writes==1);
    assert(disk[12288]==0xa5&&disk[8192]==0);
    unsigned before=writes;block_poll();assert(writes==before); /* consumed request */
    assert(!request(0,2,1,BLOCK_OP_WRITE)&&writes==before);
    assert(request(0,34,1,BLOCK_OP_WRITE));before=writes;
    assert(!request(0,34,2,BLOCK_OP_WRITE)&&writes==before); /* crosses store end */
    assert(!request(0,35,1,BLOCK_OP_WRITE)&&writes==before);
    /* Mutable shared geometry must not redefine a firmware LBA's real size. */
    block_shared.part[0].block_size=8192;
    assert(!request(0,2,1,BLOCK_OP_WRITE)&&writes==before);
    block_shared.part[0].block_size=0;block_shared.part[0].blocks=0;block_shared.parts=0;
    assert(request(0,3,1,BLOCK_OP_WRITE));before=writes;
    assert(!request(160,3,1,BLOCK_OP_WRITE)&&writes==before);
    assert(!request(0,UINT64_MAX,2,BLOCK_OP_WRITE)&&writes==before);
    native_in=1;assert(!request(0,3,1,BLOCK_OP_WRITE)&&writes==before);native_in=0;
    flush_error=1;uint64_t completed=block_writes;assert(!request(0,3,1,BLOCK_OP_WRITE));
    assert(block_writes==completed);flush_error=0;
    read_corrupt=1;assert(!request(0,3,1,BLOCK_OP_WRITE));read_corrupt=0;
    run_storage_bio=NULL;before=writes;assert(!request(0,3,1,BLOCK_OP_WRITE)&&writes==before);
    run_storage_bio=&io;assert(request(0,0,1,BLOCK_OP_READ));
    assert(!request(0,0,1,99));
    puts("PASS: real loader service write/readback, dynamic limits, real EFI geometry, duplicate requests, flush/corruption failures");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-loader-service-') as temporary:
    directory = Path(temporary)
    cfile, binary = directory / 'service.c', directory / 'service'
    cfile.write_text(prefix + structures + state + write_gate + poll + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', '-I', str(root / 'src/kernel/bsd/sys'),
                    str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=20)
