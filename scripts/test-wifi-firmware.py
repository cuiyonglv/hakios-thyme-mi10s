#!/usr/bin/env python3
"""Exercise the production firmware mapper, including absent board-data behavior."""
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
text = (root / 'src/kernel/bsd/dev/redwood_wifi.c').read_text()
start = text.index('const uint8_t *\nredwood_wifi_driver_file(')
end = text.index('\n/*', start)
function = text[start:end]
prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
static bool qca = true, missing;
static unsigned calls;
static const char *requested;
static uint8_t bytes[8];
static bool umi_wifi_is_board(void) { return qca; }
static const uint8_t *image_file(const char *name, uint64_t *size) {
    requested = name; ++calls;
    if (missing) return NULL;
    *size = sizeof(bytes); return bytes;
}
'''
suffix = r'''
static void expect(const char *input, const char *path) {
    uint64_t size = 0;
    calls = 0; requested = NULL;
    assert(redwood_wifi_driver_file(input, &size) == bytes);
    assert(calls == 1 && size == sizeof(bytes));
    assert(strcmp(requested, path) == 0);
}
int main(void) {
    expect("ath11k/QCA6390/hw2.0/board.bin", "qca6390/bd_j2s.elf");
    expect("amss.bin", "qca6390/amss20.bin");
    expect("m3.bin", "qca6390/m3.bin");
    expect("regdb.bin", "qca6390/regdb.bin");
    uint64_t size = 0;
    calls = 0;
    assert(redwood_wifi_driver_file("board-2.bin", &size) == NULL && calls == 0);
    missing = true;
    assert(redwood_wifi_driver_file("board.bin", &size) == NULL);
    assert(calls == 1 && strcmp(requested, "qca6390/bd_j2s.elf") == 0);
    missing = false; qca = false;
    expect("board.bin", "bd_m20gl.elf");
    expect("regdb.bin", "regdb_xiaomi.bin");
    return 0;
}
'''
compiler = shutil.which('clang') or shutil.which('gcc')
assert compiler, 'host C compiler required'
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    for name, candidate, success in [('current', function, True),
                                    ('old-umi-board', function.replace('qca6390/bd_j2s.elf', 'qca6390/bd_j1gl.elf'), False)]:
        source = work / (name + '.c')
        binary = work / name
        source.write_text(prefix + candidate + suffix)
        subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', str(source), '-o', str(binary)], check=True)
        result = subprocess.run([str(binary)], capture_output=True)
        assert (result.returncode == 0) == success, result.stderr.decode(errors='replace')
print('WiFi firmware mapper: thyme board data and absent-file refusal passed; old UMI mapping rejected')
