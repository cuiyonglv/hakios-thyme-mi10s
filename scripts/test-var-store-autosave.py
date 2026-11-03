#!/usr/bin/env python3
"""Exercise actual saver main/boot-arg parser; storage and allocations are instrumented."""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/userland/var_store.c').read_text()
parser = source[source.index('static int\nboot_arg_is('):source.index('static long\nparse_number(')]
main = source[source.index('int\nmain(int argc,'):].replace('int\nmain(int argc,', 'int\nsaver_main(int argc,', 1)
prefix = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#define BUFFER 4096
#define LINKS 8
#define DEVICE "fake-device"
static const char *boot="";
static int query_error,layout_ok=1,layouts,allocations,opens,watches,saves,claims;
static uint8_t *buffer,*already;
static struct {uint64_t ino;char path[256];} *links;
static void *allocated[16];static int fd;
static int sysctlbyname(const char *name,void *out,size_t *size,void *in,size_t in_size){
    assert(!strcmp(name,"kern.bootargs"));
    if(query_error||strlen(boot)+1>*size)return -1;
    memcpy(out,boot,strlen(boot)+1);*size=strlen(boot);return 0;
}
static void *fake_mmap(void *a,size_t n,int prot,int flags,int f,long offset){
    void *p=malloc(n);assert(p&&allocations<16);allocated[allocations++]=p;return p;
}
static int fake_open(const char *name,int flags,int mode){assert(!strcmp(name,DEVICE));opens++;return 20;}
static int fake_close(int f){assert(f==20);return 0;}
static int storage_layout_init(void){layouts++;return layout_ok;}
static void crc_init(void){}
static int stamp(const char *p){return 1;}
static int watch(int seconds){watches++;return 1;}
static long parse_number(const char *s){return strtol(s,NULL,10);}
static int claim(void){claims++;return 1;}
static void release(void){}
static int save(void){saves++;return 1;}
static int load(void){return 1;}
static int erase(void){return 1;}
static void show(void){}
#define mmap fake_mmap
#define open fake_open
#define close fake_close
'''
suffix = r'''
static void reset(const char *args){
    for(int i=0;i<allocations;i++)free(allocated[i]);
    boot=args;query_error=layouts=allocations=opens=watches=saves=claims=0;layout_ok=1;
}
int main(void){
    char *argv[]={"var_store","watch",NULL};
    const char *disabled[]={"","var_store=0","var_store=10","xvar_store=1","var_store=1x",
                            "var_store=0 var_store=1","var_store=01"};
    for(unsigned i=0;i<sizeof(disabled)/sizeof(disabled[0]);i++){
        reset(disabled[i]);assert(saver_main(2,argv,NULL)==0);
        assert(!layouts&&!allocations&&!opens&&!watches&&!claims&&!saves);
    }
    reset("var_store=1");query_error=1;assert(saver_main(2,argv,NULL)==0&&!layouts&&!allocations);
    reset("a=1 var_store=1 b=2");assert(saver_main(2,argv,NULL)==0);
    assert(layouts==1&&allocations==3&&watches==1&&!opens&&!claims&&!saves);
    reset("var_store=1 var_store=0");assert(saver_main(1,argv,NULL)==0&&watches==1);
    reset("var_store=1");layout_ok=0;assert(saver_main(2,argv,NULL)==1&&!allocations&&!watches);
    reset("var_store=0");argv[1]="save";assert(saver_main(2,argv,NULL)==0);
    assert(layouts==1&&opens==1&&claims==1&&saves==1&&!watches);
    reset("");puts("PASS: autosaver disabled before layout/allocation/I/O, first duplicate matches init, manual save remains explicit");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-autosave-') as temporary:
    directory = Path(temporary)
    cfile, binary = directory / 'autosave.c', directory / 'autosave'
    cfile.write_text(prefix + parser + main + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=20)
