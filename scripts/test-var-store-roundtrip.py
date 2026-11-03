#!/usr/bin/env python3
"""Production archive/save/load on a sparse file and real temporary directory.

Linux stat timestamps replace Darwin names; xattrs and file flags are omitted.
No phone, raw block device, kernel handshake or power-loss durability is tested.
"""
from pathlib import Path
import os
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'src/userland/var_store.c').read_text()
definitions = source[source.index('#define BLOCK '):source.index('/* ---- the kernel\'s side')]
definitions = definitions.replace('#define STORE_AT        2503475200LL', '#define STORE_AT        4096LL')
definitions = definitions.replace('#define SLOT_BYTES      0x20000000LL', '#define SLOT_BYTES      0x100000LL')
definitions = definitions.replace('#define BUFFER          (4u << 20)', '#define BUFFER          8192u')
definitions = definitions.replace('#define ROOT            "/private/var"', '#define ROOT            "tree"')
body = source[source.index('static uint32_t crc_table'):source.index('static void\nshow(void)')]
body = body.replace('st->st_flags', '0u').replace('st->st_mtimespec', 'st->st_mtim')
body = body.replace('#define NAME_MAX_NAMES  65536', '#define NAME_MAX_NAMES  8')
prefix = r'''
#define _DEFAULT_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/time.h>
#define XATTR_NOFOLLOW 1
static ssize_t listxattr(const char *p,char *n,size_t s,int o) { return 0; }
static ssize_t getxattr(const char *p,const char *n,void *v,size_t s,int pos,int o) { return -1; }
static int setxattr(const char *p,const char *n,const void *v,size_t s,int pos,int o) { return 0; }
static int chflags(const char *p,int f) { return 0; }
/* Fail before a chosen block write to emulate interrupted saves. */
static long writes_left=-1;
static ssize_t host_pwrite(int f,const void *p,size_t n,off_t at) {
    if(writes_left==0){errno=EIO;return -1;}
    if(writes_left>0)writes_left--;
    return pwrite(f,p,n,at);
}
#define pwrite host_pwrite
static int enumeration_error;
static ssize_t host_getdirentries(int f,char *p,size_t n,off_t *at) {
    if(enumeration_error){errno=EIO;return -1;}
    return getdirentries(f,p,n,at);
}
#define getdirentries host_getdirentries
'''
suffix = r'''
static unsigned char content[40001], restored[40001];
static void write_content(unsigned seed) {
    for(unsigned i=0;i<sizeof(content);i++)content[i]=(i*37+seed)%251;
    int f=open(ROOT "/db/data",O_WRONLY|O_CREAT|O_TRUNC,0600);assert(f>=0);
    assert(write(f,content,sizeof(content))==sizeof(content));close(f);
}
static void clear_tree(void) {
    unlink(ROOT "/db/data");unlink(ROOT "/db/hard");unlink(ROOT "/shortcut");
    assert(rmdir(ROOT "/db")==0);
}
static void check_content(unsigned seed) {
    int f=open(ROOT "/db/data",O_RDONLY);assert(f>=0);
    assert(read(f,restored,sizeof(restored))==sizeof(restored));close(f);
    for(unsigned i=0;i<sizeof(restored);i++)assert(restored[i]==(i*37+seed)%251);
    struct stat a,b;assert(stat(ROOT "/db/data",&a)==0&&stat(ROOT "/db/hard",&b)==0);
    assert(a.st_ino==b.st_ino);
    char target[32]={0};assert(readlink(ROOT "/shortcut",target,sizeof(target))==7);
    assert(memcmp(target,"db/data",7)==0);
}
int main(void) {
    fd=open("store.sparse",O_RDWR|O_CREAT|O_EXCL,0600);assert(fd>=0);
    assert(ftruncate(fd,STORE_AT+SLOTS*SLOT_BYTES)==0);
    buffer=malloc(BUFFER);already=malloc(BUFFER);links=calloc(LINKS,sizeof(*links));
    assert(buffer&&already&&links);crc_init();
    assert(mkdir(ROOT,0700)==0&&mkdir(ROOT "/db",0700)==0);
    assert(mkdir(ROOT "/tmp",0700)==0);
    int runtime=open(ROOT "/tmp/transient",O_WRONLY|O_CREAT,0600);assert(runtime>=0);close(runtime);
    write_content(1);assert(link(ROOT "/db/data",ROOT "/db/hard")==0);
    assert(symlink("db/data",ROOT "/shortcut")==0);
    assert(save());struct head h;assert(newest_verified(&h)==0&&h.generation==1);
    write_content(2);assert(save());assert(newest_verified(&h)==1&&h.generation==2);
    clear_tree();assert(load());check_content(2);
    /* Damage only the newer archive: CRC must choose the older real archive. */
    unsigned char value;off_t corrupt=slot_at(1)+BLOCK+1024;
    assert(pread(fd,&value,1,corrupt)==1);value^=0x80;
    assert(host_pwrite(fd,&value,1,corrupt)==1);
    assert(newest_verified(&h)==0&&h.generation==1);
    clear_tree();assert(load());check_content(1);
    /* Every write cut during save must preserve recoverable generation 1. */
    for(long cut=0;cut<7;cut++) {
        /* Reset only the disposable destination so dedup cannot skip injected cuts. */
        unsigned char zero[8192]={0};
        for(off_t where=slot_at(1);where<slot_at(1)+SLOT_BYTES;where+=sizeof(zero))
            assert(host_pwrite(fd,zero,sizeof(zero),where)==sizeof(zero));
        write_content(3);writes_left=cut;assert(!save());writes_left=-1;
        assert(newest_verified(&h)==0&&h.generation==1);
        clear_tree();assert(load());check_content(1);
    }
    write_content(4);assert(save());assert(newest_verified(&h)==1&&h.generation==2);
    clear_tree();assert(load());check_content(4);
    /* An unchanged save remains a valid archive and retains skip semantics. */
    assert(save());assert(newest_verified(&h)==0&&h.generation==3);
    clear_tree();assert(load());check_content(4);
    /* Enumeration errors and full name arenas must not commit incomplete snapshots. */
    enumeration_error=1;assert(!save());enumeration_error=0;
    assert(newest_verified(&h)==0&&h.generation==3);
    for(unsigned i=0;i<10;i++) {
        char name[64];snprintf(name,sizeof(name),ROOT "/extra%u",i);
        int f=open(name,O_WRONLY|O_CREAT,0600);assert(f>=0);close(f);
    }
    assert(!save());assert(newest_verified(&h)==0&&h.generation==3);
    for(unsigned i=0;i<10;i++) {
        char name[64];snprintf(name,sizeof(name),ROOT "/extra%u",i);assert(unlink(name)==0);
    }
    clear_tree();assert(load());check_content(4);
    free(buffer);free(already);free(links);close(fd);
    puts("PASS: real pack/save/CRC/load, binary bytes and links, CRC fallback, seven interrupted saves, enumeration errors/limits preserve old generation");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-roundtrip-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'roundtrip.c'
    cfile.write_text(prefix + definitions + body + suffix)
    binary = directory / 'roundtrip'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], cwd=directory, check=True, timeout=30)
