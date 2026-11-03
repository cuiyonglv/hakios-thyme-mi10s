#!/usr/bin/env python3
"""Verify real staging refuses mixed, missing, redirected or changed firmware."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage', root / 'scripts/stage-root-image.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
names = ('fw/a650_gmu.bin', 'fw/a650_sqe.fw', 'fw/a650_zap.mdt', 'fw/a650_zap.b02',
         'fw/qca6390/amss20.bin', 'fw/qca6390/m3.bin', 'fw/qca6390/bd_j2s.elf', 'fw/qca6390/regdb.bin')
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    firmware, template = work / 'firmware', work / 'template'
    template.mkdir()
    template.joinpath('keep').write_bytes(b'original template')
    files = {}
    for name in names:
        path = firmware / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(('fixture bytes ' + name).encode())
        files[name] = {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    record = {'schema': 1, 'target': 'thyme', 'stock_release': 'OS1.0.4.0.TGACNXM',
              'source_archive_md5': '778cda43017aacde0d7a04ff85b872f8', 'files': files}
    manifest = firmware / 'manifest.json'
    manifest.write_text(json.dumps(record))
    original = module.inventory(firmware)
    assert module.checked_firmware(firmware) == record
    module.stage_firmware(template, firmware, record)
    for name in names:
        assert (template / name).read_bytes() == (firmware / name).read_bytes()
        assert (template / name).stat().st_mode & 0o777 == 0o644
    assert (template / 'keep').read_bytes() == b'original template'
    assert module.inventory(firmware) == original

    def reject(operation):
        try:
            operation()
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError('invalid firmware staging accepted')

    reject(lambda: module.stage_firmware(template, firmware, record))
    path = firmware / names[0]
    saved = path.read_bytes()
    path.write_bytes(saved + b'changed')
    reject(lambda: module.checked_firmware(firmware))
    fresh = work / 'fresh'
    fresh.mkdir()
    reject(lambda: module.stage_firmware(fresh, firmware, record))
    assert not (fresh / 'fw').exists()
    path.write_bytes(saved)
    path.unlink()
    path.symlink_to(template / names[0])
    reject(lambda: module.checked_firmware(firmware))
    path.unlink()
    path.write_bytes(saved)
    outside = work / 'outside'
    outside.mkdir()
    (fresh / 'fw').symlink_to(outside, target_is_directory=True)
    reject(lambda: module.stage_firmware(fresh, firmware, record))
    assert not list(outside.iterdir())
    record['files']['fw/qca6390/bd_j1gl.elf'] = record['files'].pop('fw/qca6390/bd_j2s.elf')
    manifest.write_text(json.dumps(record))
    reject(lambda: module.checked_firmware(firmware))

print('Root firmware: eight files copied exactly; mixed, altered and symlink inputs refused')
