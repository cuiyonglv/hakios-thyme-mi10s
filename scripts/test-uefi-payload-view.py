#!/usr/bin/env python3
"""Exercise the actual UEFI XNUP selector at a protected page boundary."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
typedef uint8_t UINT8;
typedef uint32_t UINT32;
typedef uint64_t UINT64;
typedef uintptr_t UINTN;
typedef int BOOLEAN;
#define STATIC static
#define CONST const
#define TRUE 1
#define FALSE 0
#include "PayloadView.h"
static void u32(UINT8 *p, UINT32 n) { for (int i=0;i<4;i++) p[i]=n>>(i*8); }
static void fixture(UINT8 *p) {
 memset(p,0,72); memcpy(p,"XNUP",4); u32(p+4,64);
 memcpy(p+8,"loader.efi",10); u32(p+24,3); memcpy(p+32,"EFI",3);
}
static void refuse(UINT8 *p, UINTN n) {
 THYME_PAYLOAD_VIEW view={(const UINT8 *)1,123};
 assert(!ThymeFindPayloadLoader(p,n,&view));
 assert(view.Loader==NULL && view.LoaderSize==0);
}
int main(void) {
 size_t page=(size_t)sysconf(_SC_PAGESIZE);
 UINT8 *map=mmap(NULL,3*page,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 assert(map!=MAP_FAILED && mprotect(map+page,page,PROT_READ|PROT_WRITE)==0);
 UINT8 *p=map+2*page-72;
 THYME_PAYLOAD_VIEW view;
 fixture(p); assert(ThymeFindPayloadLoader(p,72,&view));
 assert(view.Loader==p+32 && view.LoaderSize==3 && memcmp(view.Loader,"EFI",3)==0);
 for (unsigned n=0;n<72;n++) refuse(p,n);
 refuse(NULL,72); refuse(p,(UINTN)-1);
 assert(!ThymeFindPayloadLoader(p,72,NULL));
 fixture(p); u32(p+4,65); refuse(p,72);
 fixture(p); u32(p+24,0xffffffffU); refuse(p,72);
 fixture(p); u32(p+24,0); refuse(p,72);
 fixture(p); u32(p+28,1); refuse(p,72);
 fixture(p); memset(p+8,'A',16); refuse(p,72);
 fixture(p); p[19]='x'; refuse(p,72); /* garbage after name terminator */
 fixture(p); p[71]=1; refuse(p,72); /* candidate found, later terminal invalid */
 fixture(p); u32(p+4,40); refuse(p,72); /* no terminating entry */
 UINT8 *large=map+2*page-112;
 memset(large,0,112); memcpy(large,"XNUP",4); u32(large+4,104);
 memcpy(large+8,"loader.efi",10); u32(large+24,1); large[32]=1;
 memcpy(large+48,"loader.efi",10); u32(large+64,1); large[72]=2;
 refuse(large,112); /* duplicate executable */
 fixture(p-1); assert(ThymeFindPayloadLoader(p-1,72,&view)); /* unaligned header */
 UINT32 state=0x12345678;
 for (unsigned trial=0;trial<2000;trial++) {
  fixture(p);
  state=state*1664525U+1013904223U;
  unsigned offset=state%72;
  p[offset]^=(UINT8)((state>>16)|1);
  if (!ThymeFindPayloadLoader(p,72,&view)) assert(!view.Loader && !view.LoaderSize);
  else assert(view.Loader>=p+8 && view.Loader+view.LoaderSize<=p+72);
 }
 assert(munmap(map,3*page)==0);
 puts("PASS UEFI selector: protected boundaries, malformed suffix, duplicates, unaligned input and mutations");
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='thyme-uefi-view-') as directory:
        path = Path(directory)
        source = path / 'test.c'
        source.write_text(HARNESS)
        output = path / 'test'
        subprocess.run([os.environ.get('CC', 'clang'), '-std=c11', '-D_GNU_SOURCE',
                        '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I', str(ROOT / 'src/uefi/Library/BootManagerLib'),
                        str(source), '-o', str(output)], check=True)
        subprocess.run([str(output)], check=True, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0',
                                                     'UBSAN_OPTIONS': 'halt_on_error=1'})


if __name__ == '__main__':
    main()
