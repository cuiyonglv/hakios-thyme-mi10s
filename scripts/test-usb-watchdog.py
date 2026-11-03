#!/usr/bin/env python3
"""Prevent watchdog starvation during continuous completed USB transfers."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]
s=(ROOT/'src/loader/loader.c').read_text();a=s.index('static int64_t\nusb_wait(');b=s.index('\nstatic int usb_rx_queued',a)
code=s[a:b]
harness=r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "efi.h"
enum {CNTFRQ_EL0,CNTVCT_EL0};
static uint64_t ticks;
#define READ_SYSREG(x) ((x)==CNTFRQ_EL0?1000:ticks)
static int pets,mode,usb_connected;
static void *running_live;
static void wdt_pet(void) {pets++;}
static efi_status event(uint32_t *msg,uint64_t *size,usb_device_event_data *data) {
 ticks+=500;*size=sizeof(*data);
 if(mode==2) {*msg=UsbDeviceEventDeviceStateChange;data->device_state=UsbDeviceStateDisconnected;return 0;}
 *msg=UsbDeviceEventTransferNotification;
 data->transfer.endpoint_index=mode?2:1;
 data->transfer.bytes_completed=1024;data->transfer.status=UsbDeviceTransferStatusCompleteOK;
 return 0;
}
static usb_device_protocol device={.handle_event=event};
static usb_device_protocol *USB=&device;
"""
harness+=code+r"""
int main(void) {
 for(int i=0;i<100;i++) assert(usb_wait(1)==1024);
 assert(pets>20); /* Every completion used to return before the pet check. */
 int previous=pets;mode=1;running_live=(void*)1;
 assert(usb_wait(1)==-1 && pets>previous);
 mode=2;running_live=NULL;assert(usb_wait(1)==-1 && !usb_connected);
 puts("PASS USB watchdog: continuous completions, busy-session timeout and disconnect");
}
"""
with tempfile.TemporaryDirectory(prefix='thyme-usb-pet-') as d:
 p=Path(d);(p/'test.c').write_text(harness)
 subprocess.run(['clang','-std=c11','-O1','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I',str(ROOT/'src/loader'),str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True,timeout=10)
