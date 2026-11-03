#!/usr/bin/env python3
"""Run the production physical-watchdog trial policy against simulated registers."""
from pathlib import Path
import re, subprocess, tempfile
ROOT=Path(__file__).resolve().parents[1]
s=(ROOT/'src/loader/loader.c').read_text()
a=s.index('static int\nwdt_thyme_start(');b=s.index('\nstatic void ios_colour',a)
function=s[a:b]
defines='\n'.join(re.search(r'^#define '+n+r'\s+.*$',s,re.M)[0] for n in ('THYME_WDT_RESET','THYME_WDT_ENABLE','THYME_WDT_STATUS','THYME_WDT_BARK','THYME_WDT_BITE','THYME_WDT_MASK','THYME_WDT_HZ','THYME_WDT_TRIAL_SECONDS'))
harness=r"""
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include "efi.h"
static uint32_t regs[8];
static int profile,wdt_armed,wdt_trial_pending,writes,reject_enable,no_clock,bad_wait,wdt_physical;
static uint32_t wdt_physical_ms;
static uint64_t timer,frequency;
static void print(const char *fmt,...) {(void)fmt;}
static int wdt_thyme_profile(void) {return profile;}
static uint32_t wdt_thyme_read(uint32_t off) {assert(off<sizeof(regs));return regs[off/4];}
static void wdt_thyme_write(uint32_t off,uint32_t value) {
 assert(off==THYME_WDT_BITE || off==THYME_WDT_RESET || off==THYME_WDT_ENABLE);
 writes++;
 if(off==THYME_WDT_ENABLE && value==1 && reject_enable)return;
 regs[off/4]=value;
 if(off==THYME_WDT_RESET)regs[THYME_WDT_STATUS/4]=0;
}
enum {CNTFRQ_EL0,CNTVCT_EL0};
static uint64_t clock_read(int which) {return which==CNTFRQ_EL0?frequency:timer;}
#define READ_SYSREG(x) clock_read(x)
static efi_status wait_us(uint64_t us) {
 assert(us==20000);timer+=384000;
 if(!no_clock)regs[THYME_WDT_STATUS/4]=655*2;
 return bad_wait?EFI_ERROR_BIT:0;
}
static efi_boot_services services={.stall=wait_us};
static efi_boot_services *BS=&services;
static void reset(void) {
 for(unsigned i=0;i<8;i++)regs[i]=0;
 regs[THYME_WDT_BARK/4]=0x7ffff;regs[THYME_WDT_BITE/4]=0xfffff;
 wdt_physical=0;wdt_physical_ms=0;
 profile=1;wdt_armed=wdt_trial_pending=writes=reject_enable=no_clock=bad_wait=0;
 timer=1000;frequency=19200000;
}
static void restored(void) {
 assert(!wdt_trial_pending && regs[THYME_WDT_ENABLE/4]==0);
 assert(regs[THYME_WDT_BARK/4]==0x7ffff && regs[THYME_WDT_BITE/4]==0xfffff);
}
"""
harness+=function+r"""
int main(void) {
 reset();profile=0;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();wdt_armed=1;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();wdt_trial_pending=1;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();regs[THYME_WDT_ENABLE/4]=1;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();regs[THYME_WDT_BARK/4]=1;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();frequency=0;assert(!wdt_thyme_expiry_trial() && !writes);
 reset();reject_enable=1;assert(!wdt_thyme_expiry_trial());restored();
 reset();no_clock=1;assert(!wdt_thyme_expiry_trial());restored();
 reset();bad_wait=1;assert(!wdt_thyme_expiry_trial());restored();
 reset();assert(wdt_thyme_expiry_trial());assert(wdt_trial_pending && !wdt_armed);
 assert(regs[THYME_WDT_ENABLE/4]==1 && regs[THYME_WDT_BITE/4]==32765*8);
 assert(regs[THYME_WDT_BARK/4]==0x7ffff && writes==3);
 assert(!wdt_thyme_expiry_trial() && writes==3);
 reset();assert(wdt_thyme_start(12,1));assert(wdt_armed && wdt_physical && !wdt_trial_pending && wdt_physical_ms==12000);
 assert(wdt_physical_timeout(60000));assert(wdt_physical_ms==12000 && regs[THYME_WDT_BITE/4]==32765*12);
 assert(wdt_physical_timeout(0));assert(wdt_physical_ms==1000 && regs[THYME_WDT_BITE/4]==32765);
 int count=writes;wdt_physical_pet();assert(writes==count+1 && regs[THYME_WDT_ENABLE/4]==1);
 reset();assert(!wdt_thyme_start(31,1) && !writes);
 puts("PASS physical trial: eligibility, no existing-watchdog takeover, readback, clock, rollback and no BARK writes");
}
"""
with tempfile.TemporaryDirectory(prefix='thyme-physical-wdt-') as d:
 p=Path(d);(p/'test.c').write_text(defines+'\n'+harness)
 subprocess.run(['clang','-std=c11','-O1','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I',str(ROOT/'src/loader'),str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True,timeout=10)
