#!/usr/bin/env python3
"""Exercise production unpacking on temporary files; Darwin xattrs/flags are mocked."""
from pathlib import Path
import os
import subprocess
import tempfile

source = (Path(__file__).resolve().parents[1] / 'src/userland/var_store.c').read_text()
structures = source[source.index('struct head {'):source.index('/* ---- the kernel\'s side')]
functions = source[source.index('static int\nwhole_path('):source.index('static int\npass(')]
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/time.h>
#define ROOT "restore"
#define MAX_PATH 1024
#define MAX_XATTR (256u << 10)
#define XATTR_NOFOLLOW 1
enum { ENT_DIR=1, ENT_FILE=2, ENT_SYMLINK=3, ENT_HARDLINK=4, ENT_DIRTIME=5 };
static char path[MAX_PATH];
static uint64_t made_dirs,made_files,made_links,made_bytes,short_files,lost_bytes,skipped_big;
static uint64_t retry_ms_left=0;
static unsigned char archive[8192];
static size_t used, cursor;
static int in(void *p, uint64_t n) {
    if (n > used-cursor) return 0;
    memcpy(p,archive+cursor,n);cursor+=n;return 1;
}
static int in_skip(uint64_t n) { if(n>used-cursor)return 0;cursor+=n;return 1; }
static int in_pad(void) { return in_skip((8-cursor%8)%8); }
static int setxattr(const char *p,const char *n,const void *v,size_t s,int pos,int opts) { return 0; }
static int chflags(const char *p,int f) { return 0; }
'''
suffix = r'''
static void entry(unsigned kind,const char *name,unsigned namelen,const char *data,unsigned size) {
    struct entry e={0};e.kind=kind;e.mode=0700;e.uid=getuid();e.gid=getgid();e.pathlen=namelen;e.size=size;
    used=cursor=0;memcpy(archive,&e,sizeof(e));used=sizeof(e);
    memcpy(archive+used,name,namelen);used+=namelen;
    if(size){memcpy(archive+used,data,size);used+=size;}
    while(used%8)archive[used++]=0;
}
static void restore(unsigned kind,const char *name,const char *data,unsigned size) {
    entry(kind,name,strlen(name),data,size);
    assert(unpack_one(0));assert(cursor==used);cursor=0;
    assert(unpack_one(1));assert(cursor==used);
}
int main(void) {
    assert(mkdir(ROOT,0700)==0);
    restore(ENT_DIR,"db",NULL,0);
    restore(ENT_FILE,"db/settings","abc\0xyz",7);
    char value[32]={0};int fd=open(ROOT "/db/settings",O_RDONLY);
    assert(fd>=0&&read(fd,value,sizeof(value))==7);close(fd);
    assert(memcmp(value,"abc\0xyz",7)==0);
    restore(ENT_SYMLINK,"shortcut","db/settings",11);
    memset(value,0,sizeof(value));assert(readlink(ROOT "/shortcut",value,sizeof(value))==11);
    assert(memcmp(value,"db/settings",11)==0);
    restore(ENT_HARDLINK,"copy","db/settings",11);
    struct stat a,b;assert(stat(ROOT "/copy",&a)==0&&stat(ROOT "/db/settings",&b)==0);
    assert(a.st_ino==b.st_ino);
    entry(ENT_DIR,"db/settings",11,NULL,0);assert(!unpack_one(1));
    /* Root metadata emitted by pack_tree must remain a valid no-op entry. */
    entry(ENT_DIR,"",0,NULL,0);assert(unpack_one(0));
    entry(ENT_DIRTIME,"",0,NULL,0);assert(unpack_one(0));
    const char *bad[]={"../outside","db/../../outside","/outside","db/.."};
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        entry(ENT_FILE,bad[i],strlen(bad[i]),"x",1);assert(!unpack_one(0));
    }
    entry(ENT_FILE,"db/settings\0hidden",18,"x",1);assert(!unpack_one(0));
    entry(ENT_FILE,"",0,"x",1);assert(!unpack_one(0));
    entry(99,"unknown",7,NULL,0);assert(!unpack_one(0));
    entry(ENT_DIR,"bad-dir",7,"x",1);assert(!unpack_one(0));
    entry(ENT_FILE,"truncated",9,"abc",3);used=sizeof(struct entry)+9+2;assert(!unpack_one(0));
    assert(access("outside",F_OK)!=0&&access(ROOT "/unknown",F_OK)!=0);
    puts("PASS: real binary file, directory, symlink/hardlink restore; invalid paths/kinds/truncation rejected in preflight");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-unpack-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'unpack.c'
    cfile.write_text(prefix + structures + functions + suffix)
    binary = directory / 'unpack'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], cwd=directory, check=True, timeout=20)
