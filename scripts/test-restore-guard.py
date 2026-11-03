#!/usr/bin/env python3
"""Bounded child supervision with mocked syscalls; no device or wall-clock sleeps."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
guard = (root / 'src/userland/restore_guard.inc').read_text()
spec = importlib.util.spec_from_file_location('prepare_init', root / 'scripts/prepare-init.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
fixture = '''static void
restore_var(void) { /* original external function */ }
static int
count_entries(void) { return 0; }
int main(void) {
\trestore_var();
\trun_rc();
}
'''
try:
    module.prepare(fixture, guard)
except ValueError:
    pass
else:
    raise AssertionError('unrecognized upstream input was accepted')
with patch.object(module, 'SOURCE_SHA256', hashlib.sha256(fixture.encode()).hexdigest()):
    prepared = module.prepare(fixture, guard)
    assert prepared.count('static int\nrestore_var(void)') == 1
    assert prepared.index('if (!restore_var())') < prepared.index('\trun_rc();')
    gate = prepared[prepared.index('\tif (!restore_var())'):prepared.index('\trun_rc();')]

prefix = r'''
#include <assert.h>
#include <errno.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#define SYS_kill 37
#define RB_HALT 8
static int enabled=1, fork_error, mode, waited, slept, killed, launched, halt_returns;
static jmp_buf stop;
static char *environ_storage[]={NULL};
#define environ environ_storage
static const char *boot_arg(const char *name){assert(!strcmp(name,"var_store"));return enabled?"1":NULL;}
static const char *in_image(const char *name){return name;}
static pid_t fake_fork(void){return fork_error?-1:42;}
static int fake_exec(const char *p,char *const a[],char *const e[]){abort();}
static void fake_exit(int status){abort();}
static pid_t fake_wait(pid_t pid,int *status,int options,void *usage){
    assert(pid==42&&options==1&&usage==NULL);waited++;
    if(killed){if(mode==6)return 0;*status=9;return pid;}
    if(mode==3||mode==6)return 0;
    if(mode==4){errno=5;return -1;}
    if(mode==5&&waited==1){errno=4;return -1;}
    if(mode==0&&waited==1)return 0;
    *status=mode==1?(127<<8):mode==2?9:0;return pid;
}
static int fake_sleep(unsigned usec){
    if(usec==1000000){assert(halt_returns);longjmp(stop,2);}
    assert(usec==100000);slept++;return 0;
}
static long mini_syscall(long number,long pid,long signal,long c,long d,long e,long f){
    assert(number==37&&pid==42&&signal==9);killed++;return 0;
}
static int fake_reboot(int flags){assert(flags==RB_HALT);if(!halt_returns)longjmp(stop,1);return -1;}
#define fork fake_fork
#define execve fake_exec
#define _exit fake_exit
#define wait4 fake_wait
#define usleep fake_sleep
#define reboot fake_reboot
'''
suffix = r'''
static void reset(int scenario){mode=scenario;enabled=1;fork_error=waited=slept=killed=launched=halt_returns=0;}
static void boot_services(void) {
GATE
    launched++;
}
int main(void){
    reset(0);enabled=0;assert(restore_var()==1&&waited==0);
    reset(0);fork_error=1;assert(restore_var()==0&&waited==0&&killed==0);
    reset(0);assert(restore_var()==1&&waited==2&&killed==0);
    reset(1);assert(restore_var()==0&&waited==1);
    reset(2);assert(restore_var()==0&&waited==1);
    reset(3);assert(restore_var()==0&&waited==601&&slept==600&&killed==1);
    reset(4);assert(restore_var()==0&&killed==1);
    reset(5);assert(restore_var()==1&&waited==2&&killed==0);
    reset(6);assert(restore_var()==0&&waited==620&&slept==620&&killed==1);
    reset(0);boot_services();assert(launched==1);
    reset(1);int halted=setjmp(stop);if(!halted)boot_services();assert(halted==1&&launched==0);
    reset(1);halt_returns=1;halted=setjmp(stop);if(!halted)boot_services();assert(halted==2&&launched==0);
    puts("PASS: init restore success/failure/signals/EINTR/timeout, bounded kill/reap, failure blocks services even if halt returns");
}
'''.replace('GATE', gate)
with tempfile.TemporaryDirectory(prefix='thyme-restore-guard-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'guard.c'
    cfile.write_text(prefix + guard + suffix)
    binary = directory / 'guard'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', str(cfile), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=20)
    out = directory / 'existing.c'
    out.write_text('preserve existing output')
    bad = directory / 'unknown.c'
    bad.write_text(fixture)
    result = subprocess.run(['python3', str(root / 'scripts/prepare-init.py'),
                             '--init', str(bad), '--out', str(out)], capture_output=True)
    assert result.returncode != 0 and out.read_text() == 'preserve existing output'
    result = subprocess.run(['python3', str(root / 'scripts/prepare-init.py'),
                             '--init', str(bad), '--out', str(bad)], capture_output=True)
    assert result.returncode != 0 and bad.read_text() == fixture
print('PASS: source pin rejects unknown revisions; invalid input preserves output and cannot overwrite input')
