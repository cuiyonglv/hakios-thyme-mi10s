#!/usr/bin/env python3
"""Run production XNUP/Mach-O parsing and USB receive on guarded host buffers."""
import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / 'src/loader/loader.c').read_text()
start = text.index('struct payload_info {')
parser_source = text[start:text.index('\n/* ------------------------------------------------------------------------ */\n/* NVRAM image', start)]
start = text.index('static int\nreceive_download(')
receiver = text[start:text.index('\nstatic int\nstarts_with(', start)]
start = text.index('static void\nios_storage_try_boot(')
installation = text[start:text.index('\nefi_status EFIAPI\nefi_main(', start)]
install_header = text[text.index('struct ios_storage_header {'):text.index('#define CHECKPOINT_SELFTEST')]
constants = '\n'.join(re.search(r'^#define ' + name + r'\s+.*$', text, re.M)[0] for name in (
    'PAYLOAD_MAGIC', 'PAYLOAD_MAX_SIZE', 'KERNEL_VIRT_BASE', 'KERNEL_LINK_OFFSET',
    'BASE_ALIGN', 'MAX_MEM_SIZE', 'RESIDENT_RD_OFFSET', 'RESIDENT_RD_RESERVE',
    'PAGE_SIZE_MAX', 'BOOT_LINE_LENGTH', 'MH_MAGIC_64', 'LC_SEGMENT_64', 'LC_UNIXTHREAD'))
structures = text[text.index('struct payload_header {'):text.index('#define KERNEL_VIRT_BASE')]
structures += text[text.index('struct mach_header_64 {'):text.index('/* Globals and small helpers')].rsplit('/* ------------------------------------------------------------------------ */', 1)[0]
prefix = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
static uint8_t boot_buffer[4096];
#define PAYLOAD_BASE ((uintptr_t)boot_buffer)
static void print(const char *format, ...) { (void)format; }
static int str_eq(const char *a, const char *b) { return strcmp(a,b)==0; }
#define USB_CHUNK 64
static uint8_t usb_buffer[USB_CHUNK], *usb_rx_buf=usb_buffer;
static const uint8_t *rx_source;
static uint64_t rx_at, rx_limit, resident_rd_size;
static int rx_oversize;
static int64_t usb_rx(uint8_t *buffer, uint64_t bytes) {
    if (rx_oversize) return (int64_t)bytes+1;
    if (rx_at==rx_limit) return -1;
    if (bytes>rx_limit-rx_at) bytes=rx_limit-rx_at;
    if (bytes>13) bytes=13;
    memcpy(buffer,rx_source+rx_at,(size_t)bytes); rx_at+=bytes; return (int64_t)bytes;
}
'''
tests = r'''
static uint8_t blob[4096], macho[2048];
static uint64_t used;
static void begin(void) {
    memset(blob,0,sizeof(blob)); used=8;
    ((struct payload_header*)blob)->magic=PAYLOAD_MAGIC;
}
static void add(const char *name, const void *bytes, uint32_t size) {
    struct payload_entry e={0};
    assert(strlen(name)<sizeof(e.name)); strcpy(e.name,name); e.size=size;
    uint64_t padded=((uint64_t)size+15)&~15ULL;
    assert(used+sizeof(e)+padded<=sizeof(blob));
    memcpy(blob+used,&e,sizeof(e));
    if (size) memcpy(blob+used+sizeof(e),bytes,size);
    used+=sizeof(e)+padded;
    ((struct payload_header*)blob)->size=(uint32_t)(used-8);
}
static void *guarded(const uint8_t *bytes, size_t count, void **mapping, size_t *map_size) {
    size_t page=(size_t)sysconf(_SC_PAGESIZE);
    size_t middle=((count+page-1)/page+1)*page;
    *map_size=middle+2*page;
    uint8_t *base=mmap(NULL,*map_size,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    assert(base!=MAP_FAILED && mprotect(base+page,middle,PROT_READ|PROT_WRITE)==0);
    *mapping=base;
    uint8_t *data=base+page+middle-count;
    memcpy(data,bytes,count);
    return data;
}
static int parse_guarded(size_t count) {
    void *mapping; size_t map_size;
    uint8_t *data=guarded(blob,count,&mapping,&map_size);
    struct payload_info info; memset(&info,0xa5,sizeof(info));
    int okay=parse_payload_bytes(&info,data,count,0);
    if (!okay) assert(info.kernel==NULL && info.ramdisk==NULL && info.cmdline[0]==0);
    else {
        if (info.kernel) assert((uintptr_t)info.kernel>=(uintptr_t)data &&
            (uintptr_t)info.kernel+info.kernel_size<=(uintptr_t)data+count);
        if (info.ramdisk) assert((uintptr_t)info.ramdisk>=(uintptr_t)data &&
            (uintptr_t)info.ramdisk+info.ramdisk_size<=(uintptr_t)data+count);
    }
    assert(munmap(mapping,map_size)==0);
    return okay;
}
static int kernel_guarded(size_t count) {
    void *mapping; size_t map_size;
    uint8_t *data=guarded(macho,count,&mapping,&map_size);
    int okay=kernel_image_valid(data,count,NULL);
    assert(munmap(mapping,map_size)==0);
    return okay;
}
static void make_macho(void) {
    memset(macho,0,sizeof(macho));
    struct mach_header_64 *h=(void*)macho;
    *h=(struct mach_header_64){MH_MAGIC_64,0x100000c,0,2,2,72+288,1,0};
    struct segment_command_64 *s=(void*)(macho+32);
    *s=(struct segment_command_64){.cmd=LC_SEGMENT_64,.cmdsize=72,
        .vmaddr=KERNEL_VIRT_BASE+KERNEL_LINK_OFFSET,.vmsize=0x4000,
        .fileoff=512,.filesize=16,.maxprot=7,.initprot=5};
    uint32_t *t=(void*)(macho+32+72);
    t[0]=LC_UNIXTHREAD; t[1]=288; t[2]=6; t[3]=68;
    memcpy((uint8_t*)t+16+32*8,&s->vmaddr,8);
}
static void container_cases(void) {
    make_macho(); begin(); add("kernel",macho,sizeof(macho));
    add("ramdisk","RAM",3); add("cmdline","ufs_write=0 var_store=0",sizeof("ufs_write=0 var_store=0"));
    add("physbase","0x80000000",11); add("rdres","100000",7);
    add("checkpoint","999",4); add("usb",NULL,0); add("future","xyz",3);
    assert(parse_guarded((size_t)used));
    struct payload_info pi;
    assert(parse_payload_bytes(&pi,blob,used,1));
    assert(pi.kernel_size==sizeof(macho) && pi.ramdisk_size==3 && pi.usb_mode==1);
    assert(pi.checkpoint==999 && pi.resident_rd==0x100000 && pi.forced_base==0x80000000);
    for (size_t n=0;n<used;n++) assert(!parse_guarded(n));
    uint8_t saved[4096]; memcpy(saved,blob,sizeof(blob)); uint64_t length=used;
    ((struct payload_header*)blob)->size=UINT32_MAX; assert(!parse_guarded(length));
    memcpy(blob,saved,sizeof(blob)); ((struct payload_entry*)(blob+8))->size=UINT32_MAX;
    assert(!parse_guarded(length));
    memcpy(blob,saved,sizeof(blob)); memset(blob+8,'a',16); assert(!parse_guarded(length));
    memcpy(blob,saved,sizeof(blob)); ((struct payload_entry*)(blob+8))->reserved=1;
    assert(!parse_guarded(length));
    begin(); add("cmdline","bad",3); assert(!parse_guarded(used));
    begin(); add("checkpoint","18446744073709551616",21); assert(!parse_guarded(used));
    begin(); add("checkpoint","1x",3); assert(!parse_guarded(used));
    begin(); add("physbase","80000001",9); assert(!parse_guarded(used));
    begin(); add("rdres","ffffffffffffffff",17); assert(!parse_guarded(used));
    begin(); add("kernel",macho,sizeof(macho)); add("cmdline","ok",3); add("cmdline","later",6);
    assert(!parse_guarded(used));
    begin(); add("kernel",macho,sizeof(macho));
    used+=24; ((struct payload_header*)blob)->size=(uint32_t)(used-8); assert(parse_guarded(used));
    blob[used-1]=1; assert(!parse_guarded(used));
    uint32_t random=17;
    for (unsigned i=0;i<3000;i++) {
        memcpy(blob,saved,sizeof(blob));
        random=random*1664525+1013904223;
        size_t at=random%length; blob[at]^=(uint8_t)(1+(random>>24));
        (void)parse_guarded(length);
    }
    assert(parse_number("ffffffffffffffff",16)==UINT64_MAX);
    assert(parse_number("10000000000000001",16)==0);
}
static void macho_cases(void) {
    make_macho(); assert(kernel_guarded(sizeof(macho)));
    for (size_t n=0;n<392;n++) assert(!kernel_guarded(n));
    struct mach_header_64 *h=(void*)macho;
    struct segment_command_64 *s=(void*)(macho+32);
    uint32_t *t=(void*)(macho+104);
#define BAD(statement) do { make_macho(); statement; assert(!kernel_guarded(sizeof(macho))); } while(0)
    BAD(h->cputype=0x1000007); BAD(h->cpusubtype=2); BAD(h->filetype=6);
    BAD(h->sizeofcmds=UINT32_MAX); BAD(h->ncmds=UINT32_MAX); BAD(h->ncmds=1);
    BAD(s->cmdsize=0); BAD(s->cmdsize=7); BAD(s->cmdsize=UINT32_MAX); BAD(s->nsects=1);
    BAD(s->fileoff=UINT64_MAX); BAD(s->filesize=sizeof(macho)); BAD(s->vmsize=1);
    BAD(s->vmaddr=0); BAD(s->vmsize=UINT64_MAX); BAD(s->vmaddr=UINT64_MAX-8);
    BAD(s->vmsize=MAX_MEM_SIZE); BAD(s->initprot=3); BAD(t[1]=280); BAD(t[2]=5); BAD(t[3]=67);
    BAD(t[4+32*2]+=1); BAD(t[4+32*2]+=32);
#undef BAD
    make_macho();
    uint8_t saved[2048]; memcpy(saved,macho,sizeof(macho));
    for (unsigned i=0;i<1000;i++) {
        memcpy(macho,saved,sizeof(macho));
        macho[(i*73)%392]^=(uint8_t)(1+i%255);
        (void)kernel_guarded(sizeof(macho));
    }
    make_macho();
    memmove(macho+176,macho+104,288); memcpy(macho+104,macho+32,72);
    h->ncmds=3; h->sizeofcmds=432; assert(!kernel_guarded(sizeof(macho)));
}
static void download_cases(void) {
    begin(); add("kernel",macho,sizeof(macho));
    uint8_t target[4096]; memset(target,0xa5,sizeof(target));
    memcpy(boot_buffer,blob,(size_t)used); payload_accept(used);
    rx_source=blob; rx_at=0; rx_limit=used; rx_oversize=0;
    assert(receive_download(boot_buffer,used,0));
    struct payload_info info; assert(parse_payload(&info,0) && payload_bytes==used);
    rx_at=0; rx_limit=8; assert(!receive_download(boot_buffer,used,0));
    assert(payload_bytes==0 && !parse_payload(&info,0));
    memcpy(boot_buffer,blob,(size_t)used); payload_accept(used);
    resident_rd_size=100; rx_at=0; rx_limit=used;
    assert(receive_download(target,used,1) && resident_rd_size==used && payload_bytes==used);
    rx_at=0; rx_limit=8; assert(!receive_download(target,used,1));
    assert(resident_rd_size==0 && payload_bytes==used);
    rx_at=0; rx_limit=used; rx_oversize=1;
    memset(target,0xa5,sizeof(target)); assert(!receive_download(target,used,0));
    assert(payload_bytes==0 && target[0]==0xa5 && target[sizeof(target)-1]==0xa5);
    rx_oversize=0;
    assert(!receive_download(target,0,0) && payload_bytes==0);
}
static void memory_cases(void) {
    make_macho(); struct kernel_layout layout; struct kernel_memory plan;
    assert(kernel_image_valid(macho,sizeof(macho),&layout));
    uint64_t base=0x80000000, bytes=MAX_MEM_SIZE;
    assert(kernel_memory_plan(base,bytes,&layout,8*1024*1024,0x90000000,&plan));
    assert(plan.high<=plan.ramdisk && plan.ramdisk+plan.ramdisk_size<=plan.resident);
    assert(plan.resident==base+RESIDENT_RD_OFFSET && plan.top<base+bytes);
    assert(kernel_memory_plan(base,bytes,&layout,0,0x90000000,&plan));
    assert(plan.ramdisk_resident && plan.resident_size==0 && plan.ramdisk==base+RESIDENT_RD_OFFSET);
    assert(kernel_memory_plan(base,bytes,&layout,1,0,&plan));
    assert(!kernel_memory_plan(base,0,&layout,0,0,&plan));
    assert(!kernel_memory_plan(UINT64_MAX-7,bytes,&layout,0,0,&plan));
    assert(!kernel_memory_plan(base+1,bytes,&layout,0,0,&plan));
    assert(!kernel_memory_plan(base,bytes,&layout,UINT64_MAX,0,&plan));
    assert(!kernel_memory_plan(base,bytes,&layout,0,UINT64_MAX,&plan));
    assert(!kernel_memory_plan(base,0x1000000,&layout,0,0,&plan));
    layout.high=KERNEL_VIRT_BASE+RESIDENT_RD_OFFSET;
    assert(!kernel_memory_plan(base,bytes,&layout,1,1,&plan));
    layout.high=UINT64_MAX;
    assert(!kernel_memory_plan(base,bytes,&layout,0,0,&plan));
}
static uint8_t *read_file(const char *name, uint64_t *size) {
    FILE *f=fopen(name,"rb"); assert(f);
    assert(fseek(f,0,SEEK_END)==0); long count=ftell(f); assert(count>0);
    rewind(f); uint8_t *data=malloc((size_t)count); assert(data);
    assert(fread(data,1,(size_t)count,f)==(size_t)count); fclose(f); *size=(uint64_t)count; return data;
}
int main(int argc, char **argv) {
    container_cases(); macho_cases(); download_cases(); memory_cases(); transaction_cases();
    if (argc>1) { uint64_t bytes; uint8_t *data=read_file(argv[1],&bytes);
        assert(kernel_image_valid(data,bytes,NULL)); free(data); }
    if (argc>2) { uint64_t bytes; uint8_t *data=read_file(argv[2],&bytes); struct payload_info pi;
        assert(parse_payload_bytes(&pi,data,bytes,0)); assert(pi.ramdisk_size>0);
        assert(kernel_image_valid(pi.kernel,pi.kernel_size,NULL)); free(data); }
    puts("PASS: bounded XNUP/Mach-O, physical planning, guarded mutations, USB and install publication");
    return 0;
}
'''
install_mocks = r'''
#include "thyme_storage_layout.h"
struct mock_media { uint64_t last_block; uint32_t block_size; };
typedef struct { struct mock_media *media; } efi_block_io;
static struct mock_media media={15,4096};
static efi_block_io io={&media}, *pending_storage_bio;
static struct thyme_store_info pending_storage;
static struct ios_storage_header rom_header;
static uint8_t rom_payload[4096], rom_system[4096], system_buffer[4096];
static uint64_t win_base, win_size;
static int read_count, fail_read, header_good, digest_good, starts, attempts, resets;
static void ios_colour(uint32_t color) { (void)color; }
static void flush_log(void) {}
static void reboot(void) { resets++; }
static efi_block_io *find_partition_bio(const uint16_t *name) { (void)name; return &io; }
static int read_partition_bytes(efi_block_io *bio, uint64_t at, uint64_t bytes, uint8_t *data) {
    (void)bio; read_count++;
    if (read_count==fail_read) {
        if (at==IOS_HEADER_BYTES && bytes>=8) memcpy(data,rom_payload,8);
        return 0;
    }
    if (at==0) { assert(bytes==sizeof(rom_header)); memcpy(data,&rom_header,(size_t)bytes); }
    else if (at==IOS_HEADER_BYTES) { assert(bytes<=sizeof(rom_payload)); memcpy(data,rom_payload,(size_t)bytes); }
    else { assert(bytes==sizeof(rom_system)); memcpy(data,rom_system,(size_t)bytes); }
    return 1;
}
static int ios_header_valid(const struct ios_storage_header *h, uint64_t bytes) {
    (void)h; (void)bytes; return header_good;
}
static int ios_image_digest_valid(const struct ios_storage_header *h, const uint8_t *p, const uint8_t *s) {
    (void)h; (void)p; (void)s; return digest_good;
}
static int ios_storage_info(const struct ios_storage_header *h, uint64_t bytes, struct thyme_store_info *info) {
    (void)h; (void)bytes; (void)info; return 0;
}
static int choose_window(const struct payload_info *pi) { (void)pi; return 1; }
static int run_kernel(int have_checkpoint, uint64_t checkpoint) {
    (void)have_checkpoint; (void)checkpoint; attempts++;
    struct payload_info pi;
    if (!parse_payload(&pi,0) || !kernel_image_valid(pi.kernel,pi.kernel_size,NULL)) return 0;
    starts++; return 1;
}
'''
install_tests = r'''
static void install_reset(void) {
    make_macho(); begin(); add("kernel",macho,sizeof(macho)); add("cmdline","ufs_write=0",12);
    memcpy(rom_payload,blob,(size_t)used); memcpy(boot_buffer,blob,(size_t)used);
    memset(&rom_header,0,sizeof(rom_header));
    rom_header.version=1; rom_header.payload_len=used; rom_header.system_len=sizeof(rom_system);
    memset(rom_system,0,sizeof(rom_system)); memcpy(rom_system+32,"NXSB",4);
    payload_accept(used); resident_rd_size=0;
    win_base=(uintptr_t)system_buffer-RESIDENT_RD_OFFSET;
    win_size=RESIDENT_RD_OFFSET+RESIDENT_RD_RESERVE+sizeof(rom_system)+0x4000000;
    read_count=fail_read=starts=attempts=resets=0; header_good=digest_good=1;
}
static void transaction_cases(void) {
    install_reset(); digest_good=0;
    ios_storage_try_boot(); assert(payload_bytes==0 && starts==0 && attempts==0 && resets==0);
    /* efi_main's earlier have_payload flag must not run the overwritten RAM. */
    assert(!run_kernel(0,0) && starts==0);
    install_reset(); fail_read=2;
    ios_storage_try_boot(); assert(payload_bytes==0 && starts==0);
    install_reset(); fail_read=3;
    ios_storage_try_boot(); assert(payload_bytes==0 && starts==0);
    install_reset(); rom_system[32]=0;
    ios_storage_try_boot(); assert(payload_bytes==0 && starts==0);
    install_reset(); ((struct payload_header*)rom_payload)->size=UINT32_MAX;
    ios_storage_try_boot(); assert(payload_bytes==0 && starts==0);
    install_reset(); rom_header.boot_mode=1;
    ios_storage_try_boot(); assert(payload_bytes==used && starts==0 && parse_payload(&(struct payload_info){0},0));
    install_reset(); header_good=0;
    ios_storage_try_boot(); assert(payload_bytes==used && starts==0);
    install_reset(); ios_storage_try_boot();
    assert(payload_bytes==used && starts==1 && resets==1 && resident_rd_size==sizeof(rom_system));
}
'''


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--kernel', type=Path)
    cli.add_argument('--root', type=Path)
    args = cli.parse_args()
    compiler = shutil.which('clang') or shutil.which('gcc')
    assert compiler
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        source, executable = work / 'test.c', work / 'test'
        assembled = (prefix + constants + '\n' + structures + parser_source + receiver +
                     '\nstatic void transaction_cases(void);\n' + tests +
                     '\n#define IOS_HEADER_BYTES 4096ULL\n' + install_header + install_mocks)
        flags = [compiler, '-std=c11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                 '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                 '-I' + str(root / 'src/kernel/bsd/sys')]
        source.write_text(assembled + installation + install_tests)
        subprocess.run([*flags, str(source), '-o', str(executable)], check=True)
        command = [str(executable)]
        if args.kernel:
            command.append(str(args.kernel))
            if args.root:
                sys.path.insert(0, str(root / 'scripts'))
                spec = importlib.util.spec_from_file_location('packer', root / 'scripts/build-image.py')
                builder = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(builder)
                body = (builder.entry('kernel', args.kernel.read_bytes()) +
                        builder.entry('ramdisk', args.root.read_bytes()) +
                        builder.entry('cmdline', builder.DEFAULT_CMDLINE.encode() + b'\0'))
                payload = work / 'actual-payload.bin'
                payload.write_bytes(struct.pack('<II', 0x50554e58, len(body)) + body)
                command.append(str(payload))
        subprocess.run(command, check=True)
        assert installation.count('\tpayload_forget();') == 1
        source.write_text(assembled + installation.replace('\tpayload_forget();', '') + install_tests)
        subprocess.run([*flags, str(source), '-o', str(executable)], check=True)
        regression = subprocess.run([str(executable)], capture_output=True)
        assert regression.returncode != 0 and b'transaction_cases' in regression.stderr, regression.stderr
        print('PASS: old install-publication behavior fails the actual control-flow regression')


if __name__ == '__main__':
    main()
