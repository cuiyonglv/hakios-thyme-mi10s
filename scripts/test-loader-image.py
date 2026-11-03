#!/usr/bin/env python3
"""Host/ASan tests for the production installation header and partition reader."""
from pathlib import Path
import os
import importlib.util
import struct
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'src/loader/loader.c').read_text()
header = source[source.index('struct ios_storage_header {'):
                source.index('#define CHECKPOINT_SELFTEST')]
reader = source[source.index('static int\nread_partition_bytes('):
                source.index('\n/*\n * Boot the iOS installation', source.index('read_partition_bytes('))]
prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sha256.h"
#include "thyme_storage_layout.h"
#define IOS_HEADER_BYTES 4096ULL
#define IOS_STORAGE_MAGIC "IOSSTOR1"
#define PAYLOAD_MAX_SIZE 0x04000000ULL
#define BLOCK_WRITE_FIRST THYME_STORE_FIRST_BLOCK
#define BLOCK_BUFFER_SIZE (4u << 20)
typedef uint64_t efi_status;
#define EFI_ERROR(s) ((s) != 0)
typedef struct efi_block_io efi_block_io;
struct media { uint32_t block_size; uint64_t last_block; uint32_t media_id; } media;
struct efi_block_io {
    struct media *media;
    efi_status (*read_blocks)(efi_block_io *, uint32_t, uint64_t, uint64_t, void *);
};
static uint8_t block_buffer[BLOCK_BUFFER_SIZE], disk[65536];
static int calls, wdt_armed;
static void print(const char *format, ...) { (void)format; }
static void wdt_pet(void) {}
static efi_status read_blocks(efi_block_io *bio, uint32_t id, uint64_t lba,
    uint64_t bytes, void *out) {
    (void)id; ++calls;
    assert(bytes % bio->media->block_size == 0);
    assert(lba * bio->media->block_size + bytes <= sizeof(disk));
    memcpy(out, disk + lba * bio->media->block_size, bytes);
    return 0;
}
'''
suffix = r'''
int main(int argc, char **argv) {
    media.block_size = 4096; media.last_block = 15;
    efi_block_io io = {&media, read_blocks};
    struct { struct ios_storage_header header; uint8_t canary[4096]; } dest;
    memset(&dest, 0xa5, sizeof(dest));
    for (unsigned i = 0; i < sizeof(disk); ++i) disk[i] = (i ^ (i >> 8)) & 255;
    assert(read_partition_bytes(&io, 0, IOS_HEADER_BYTES, (uint8_t *)&dest.header));
    for (unsigned i = 0; i < sizeof(dest.canary); ++i) assert(dest.canary[i] == 0xa5);
    uint8_t data[9000];
    assert(read_partition_bytes(&io, 13, sizeof(data), data));
    assert(memcmp(data, disk + 13, sizeof(data)) == 0);
    assert(read_partition_bytes(&io, sizeof(disk) - 1, 1, data));
    assert(data[0] == disk[sizeof(disk) - 1]);
    int before = calls;
    assert(read_partition_bytes(&io, sizeof(disk), 0, NULL));
    assert(read_partition_bytes(&io, 1, 0, NULL));
    assert(!read_partition_bytes(&io, sizeof(disk), 1, data));
    assert(!read_partition_bytes(&io, UINT64_MAX, 2, data));
    assert(!read_partition_bytes(NULL, 0, 1, data));
    assert(!read_partition_bytes(&io, 0, 1, NULL));
    io.media = NULL;
    assert(!read_partition_bytes(&io, 0, 1, data));
    io.media = &media; io.read_blocks = NULL;
    assert(!read_partition_bytes(&io, 0, 1, data));
    io.read_blocks = read_blocks;
    media.block_size = 0;
    assert(!read_partition_bytes(&io, 0, 1, data));
    media.block_size = 3;
    assert(!read_partition_bytes(&io, 0, 1, data));
    media.block_size = 4096; media.last_block = UINT64_MAX;
    assert(!read_partition_bytes(&io, 0, 1, data));
    assert(calls == before);
    media.last_block = 15;
    struct ios_storage_header h = {0};
    memcpy(h.magic, IOS_STORAGE_MAGIC, 8);
    h.version = 1; h.payload_len = 64; h.system_len = h.part1_len = 4096;
    assert(ios_header_valid(&h, sizeof(disk)));
    assert(!ios_header_valid(&h, IOS_HEADER_BYTES + 64 + 4096 - 1));
#define REJECT(field, value) do { uint64_t old = h.field; h.field = value; \
    assert(!ios_header_valid(&h, UINT64_MAX)); h.field = old; } while (0)
    REJECT(version, 2); REJECT(boot_mode, 2); REJECT(flags, 1);
    REJECT(payload_len, 0); REJECT(payload_len, PAYLOAD_MAX_SIZE + 1);
    REJECT(system_len, 0); REJECT(part1_len, 1); REJECT(part2_len, 1);
    REJECT(part3_len, 1);
    h.part1_len = h.system_len = BLOCK_WRITE_FIRST * 4096 - IOS_HEADER_BYTES - h.payload_len;
    assert(ios_header_valid(&h, UINT64_MAX));
    ++h.system_len; ++h.part1_len;
    assert(!ios_header_valid(&h, UINT64_MAX));
    h.system_len = h.part1_len = UINT64_MAX;
    assert(!ios_header_valid(&h, UINT64_MAX));
    memcpy(h.magic, "IOSSTOR2", 8); h.version = 2;
    assert(!ios_header_valid(&h, UINT64_MAX)); /* overflow/invalid descriptor still rejected */
    memcpy(h.magic, IOS_STORAGE_MAGIC, 8); h.version = 1;
    uint8_t payload[64] = {1}, system[128] = {2};
    SHA256_CTX hash;
    h.payload_len = sizeof(payload); h.system_len = sizeof(system);
    sha256_init(&hash);
    sha256_update(&hash, payload, sizeof(payload));
    sha256_update(&hash, system, sizeof(system));
    sha256_final(&hash, h.sha256);
    assert(ios_image_digest_valid(&h, payload, system));
    payload[0] ^= 1;
    assert(!ios_image_digest_valid(&h, payload, system));
    payload[0] ^= 1; system[127] ^= 1;
    assert(!ios_image_digest_valid(&h, payload, system));
    system[127] ^= 1; h.sha256[0] ^= 1;
    assert(!ios_image_digest_valid(&h, payload, system));
    puts("PASS: 4096-byte header canary, partition bounds, invalid media, overflow, layout limits");
    puts("PASS: payload, system and digest corruption are rejected");
    memset(&h,0,sizeof(h));memcpy(h.magic,"IOSSTOR2",8);h.version=2;
    h.payload_len=sizeof(payload);h.system_len=h.part1_len=sizeof(system);
    struct thyme_store_layout layout={12288,65536,2,4096,0};
    memcpy(h.reserved,&layout,sizeof(layout));
    assert(ios_header_valid(&h,4u<<20));
    assert(!ios_header_valid(&h,layout.store_at+2*layout.slot_bytes-1));
    sha256_init(&hash);sha256_update(&hash,(const uint8_t *)&h,96);
    sha256_update(&hash,payload,sizeof(payload));sha256_update(&hash,system,sizeof(system));
    sha256_final(&hash,h.sha256);assert(ios_image_digest_valid(&h,payload,system));
    h.reserved[0]^=1;assert(!ios_image_digest_valid(&h,payload,system));h.reserved[0]^=1;
    h.boot_mode=1;assert(!ios_image_digest_valid(&h,payload,system));h.boot_mode=0;
    h.padding[0]=1;assert(!ios_header_valid(&h,4u<<20));h.padding[0]=0;
    struct thyme_store_info info;assert(ios_storage_info(&h,4u<<20,&info));
    assert(info.version==2&&info.layout.store_at==12288);
    puts("PASS: v2 descriptor/capacity, header-bound digest and nonzero padding rejection");
    assert(argc==2);FILE *file=fopen(argv[1],"rb");assert(file);
    uint8_t *packed=malloc(65536);assert(packed);size_t length=fread(packed,1,65536,file);fclose(file);
    assert(length>=4096);struct ios_storage_header *from_python=(void *)packed;
    assert(ios_header_valid(from_python,1u<<20));
    assert(4096+from_python->payload_len+from_python->system_len==length);
    assert(ios_image_digest_valid(from_python,packed+4096,packed+4096+from_python->payload_len));
    packed[64]^=1;assert(!ios_image_digest_valid(from_python,packed+4096,packed+4096+from_python->payload_len));
    free(packed);puts("PASS: Python-packed v2 image decoded and hashed by production C loader");
}
'''
with tempfile.TemporaryDirectory(prefix='thyme-image-reader-') as temporary:
    directory = Path(temporary)
    cfile = directory / 'reader.c'
    binary = directory / 'reader'
    cfile.write_text(prefix + header + reader + suffix)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O1',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    '-I', str(root / 'src/loader'), str(cfile),
                    '-I', str(root / 'src/kernel/bsd/sys'),
                    str(root / 'src/loader/sha256.c'), '-o', str(binary)], check=True)
    spec = importlib.util.spec_from_file_location('pack_fixture', root / 'scripts/build-image.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    kernel_path, apfs, image = directory / 'kernel', directory / 'test.apfs', directory / 'image'
    kernel_path.write_bytes(struct.pack('<8I', 0xfeedfacf, 0x100000c, 0, 2, 0, 0, 0, 0))
    container = bytearray(4096)
    container[32:36] = b'NXSB'
    struct.pack_into('<IQ', container, 36, 4096, 1)
    apfs.write_bytes(container)
    builder.build_image(kernel_path, apfs, apfs, image, layout_version=2,
                        partition_bytes=1 << 20, slot_bytes=65536)
    subprocess.run([str(binary), str(image)], check=True, timeout=20)
