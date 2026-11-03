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
