#!/usr/bin/env python3
"""Exercise production watchdog negotiation and refusal to hang without it."""
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'src/loader/loader.c').read_text()


def body_at(start):
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[opening + 1:end - 1]


defines = '\n'.join(re.search(r'^#define ' + name + r'\s+.*$', source, re.M)[0]
                    for name in ('VIRT_WDT_CONTROL', 'VIRT_WDT_STATUS',
                                 'VIRT_WDT_PET', 'VIRT_WDT_SET_TIME', 'WDT_BITE_MS',
                                 'QC_HYP_CALL_UID', 'QC_HYP_UID0', 'QC_HYP_UID1',
                                 'QC_HYP_UID2', 'QC_HYP_UID3'))
arm = body_at(source.index('static void\nwdt_arm(void)'))
hang = body_at(source.index('} else if (starts_with(cmd, "oem hang"))'))
harness = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "efi.h"
static int wdt_armed, protocol, statuses, enables, disables, pets, times, replies;
static uint64_t first_ctl, final_ctl, status_ret, time_ret, enable_ret, pet_ret, final_ret;
static uint64_t smc_result_x3;
static int uid_calls, uid_matches;
static void print(const char *format, ...) { (void)format; }
static efi_status locate(const efi_guid *guid, void *registration, void **iface) {
 static const efi_guid expected=QCOM_SCM_GUID;
 assert(!memcmp(guid,&expected,sizeof(expected)) && !registration);
 *iface=protocol==2?(void *)1:NULL;
 return protocol==0?EFI_ERROR_BIT:0;
}
static efi_boot_services services={.locate_protocol=locate};
static efi_boot_services *BS=&services;
static uint64_t smc_call(uint64_t id, uint64_t a1, uint64_t a2, uint64_t *r1, uint64_t *r2) {
 if (id==QC_HYP_CALL_UID) {
  assert(!a1 && !a2); uid_calls++;
  *r1=QC_HYP_UID1; *r2=QC_HYP_UID2; smc_result_x3=QC_HYP_UID3;
  return uid_matches?QC_HYP_UID0:UINT64_MAX;
 }
 if (id==VIRT_WDT_STATUS) {
  assert(!a1 && !a2); statuses++;
  if (r1) *r1=statuses==1?first_ctl:final_ctl;
  if (r2) *r2=17;
  return statuses==1?status_ret:final_ret;
 }
 if (id==VIRT_WDT_SET_TIME) { assert(a1==30000 && a2==30000); times++; return time_ret; }
 if (id==VIRT_WDT_CONTROL) {
  assert(!a2);
  if(a1==3) { enables++; return enable_ret; }
  assert(a1==2); disables++; return 0;
 }
 assert(id==VIRT_WDT_PET && !a1 && !a2); pets++; return pet_ret;
}
static void fb_reply(const char *code, const char *message) {
 assert(!strcmp(code,"FAIL") && strstr(message,"not armed")); replies++;
}
static void reset(void) {
 protocol=2; first_ctl=0; final_ctl=1;
 status_ret=time_ret=enable_ret=pet_ret=final_ret=0;
 statuses=enables=disables=pets=times=replies=0;
 uid_calls=0; uid_matches=1;
 wdt_armed=1; /* A rejected negotiation must clear any stale acceptance. */
}
'''
harness += '\nstatic void wdt_arm(void) {\n' + arm + '\n}\n'
harness += '\nstatic void hang_command(void) {\n' + hang + '\n}\n'
harness += r'''
int main(void) {
 reset(); protocol=0; wdt_arm(); assert(!wdt_armed && !uid_calls && !statuses && !times);
 reset(); protocol=1; wdt_arm(); assert(!wdt_armed && !uid_calls && !statuses);
 reset(); uid_matches=0; wdt_arm(); assert(!wdt_armed && uid_calls==1 && !statuses && !times && !enables && !pets);
 reset(); status_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && statuses==1 && !times && !enables && !pets);
 reset(); time_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && times==1 && !enables && !pets);
 reset(); enable_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && enables==1 && !pets);
 reset(); wdt_arm(); assert(wdt_armed && enables==1 && pets==1 && statuses==2 && !disables);
 reset(); first_ctl=1; wdt_arm(); assert(wdt_armed && !enables && pets==1 && !disables);
 reset(); pet_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && enables==1 && disables==1);
 reset(); final_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && disables==1);
 reset(); final_ctl=0; wdt_arm(); assert(!wdt_armed && disables==1);
 reset(); first_ctl=1; pet_ret=UINT64_MAX; wdt_arm(); assert(!wdt_armed && !disables); /* preserve preexisting service */
 wdt_armed=0; hang_command(); assert(replies==1);
 puts("PASS watchdog: protocol discovery, rejected SMCs, existing enable state, readback, rollback and hang refusal");
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix='thyme-watchdog-') as directory:
        path = Path(directory)
        test = path / 'test.c'
        test.write_text(defines + '\n' + harness)
        exe = path / 'test'
        subprocess.run(['clang', '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-I', str(ROOT / 'src/loader'),
                        str(test), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=10)


if __name__ == '__main__':
    main()
