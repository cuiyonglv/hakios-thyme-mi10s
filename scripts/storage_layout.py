"""Read the literal legacy constants from the canonical C layout contract."""
from pathlib import Path
import re

HEADER = Path(__file__).resolve().parents[1] / 'src/kernel/bsd/sys/thyme_storage_layout.h'
NAMES = ('THYME_LAYOUT_VERSION', 'THYME_STORAGE_BLOCK_SIZE', 'THYME_STORE_FIRST_BLOCK',
         'THYME_STORE_SLOT_BYTES', 'THYME_STORE_SLOTS')


def read_legacy_layout(path=HEADER):
    source = Path(path).read_text()
    values = {}
    for name in NAMES:
        matches = re.findall(r'^#define\s+' + name + r'\s+(0x[0-9a-fA-F]+|[0-9]+)[uUlL]*\s*$',
                             source, re.MULTILINE)
        definitions = re.findall(r'^#define\s+' + name + r'\b', source, re.MULTILINE)
        if len(matches) != 1 or len(definitions) != 1:
            raise ValueError(f'expected one literal definition of {name}')
        values[name] = int(matches[0], 0)
    version, block, first, slot, count = (values[name] for name in NAMES)
    if (version != 1 or block != 4096 or first < 1 or count != 2 or
            slot <= block or slot % block or first * block + count * slot >= 1 << 63):
        raise ValueError('unsupported or invalid legacy storage layout')
    return {'version': version, 'block_bytes': block, 'store_at': first * block,
            'slot_bytes': slot, 'slots': count, 'store_end': first * block + count * slot}


LEGACY_LAYOUT = read_legacy_layout()


def valid_v2_layout(layout, image_bytes, partition_bytes):
    fields = ('store_at', 'slot_bytes', 'slots', 'block_bytes', 'flags')
    if any(type(layout.get(name)) is not int or not 0 <= layout[name] < 1 << 64 for name in fields):
        return False
    if (type(image_bytes) is not int or type(partition_bytes) is not int or
            not 4096 <= image_bytes <= layout['store_at'] <= partition_bytes < 1 << 63 or
            layout['flags'] != 0 or layout['slots'] != 2 or layout['block_bytes'] != 4096 or
            layout['store_at'] % 4096 or layout['slot_bytes'] < 8192 or layout['slot_bytes'] % 4096):
        return False
    return layout['slot_bytes'] <= (partition_bytes - layout['store_at']) // layout['slots']


def plan_v2_layout(image_bytes, partition_bytes, slot_bytes=LEGACY_LAYOUT['slot_bytes']):
    if type(image_bytes) is not int or image_bytes < 4096:
        raise ValueError('invalid v2 image length')
    layout = {'store_at': (image_bytes + 4095) // 4096 * 4096,
              'slot_bytes': slot_bytes, 'slots': 2, 'block_bytes': 4096, 'flags': 0}
    if not valid_v2_layout(layout, image_bytes, partition_bytes):
        raise ValueError('v2 image and persistence slots do not fit the explicit partition capacity')
    return layout
