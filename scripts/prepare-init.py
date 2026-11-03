#!/usr/bin/env python3
"""Generate guarded init from a pinned external source; never modify the input."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile

SOURCE_SHA256 = '3018a31fb5b6a864b17085ab7921e1b0929317a899ffc44a9d11c06ec6f26062'
REPOSITORY = Path(__file__).resolve().parents[1]


def prepare(source, guard):
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise ValueError('unrecognized init source; review upstream changes before integrating')
    begin = source.index('static void\nrestore_var(void)')
    end = source.index('static int\ncount_entries(', begin)
    source = source[:begin] + guard + '\n\n' + source[end:]
    call = '\trestore_var();'
    if source.count(call) != 1:
        raise ValueError('expected exactly one restore call')
    return source.replace(call, '''\tif (!restore_var()) {
\t\tprintf("init: refusing to start services with incomplete restored data\\n");
\t\treboot(RB_HALT);
\t\t/* A failed halt must not fall through into launchd or the autosaver. */
\t\tfor (;;) {
\t\t\tusleep(1000000);
\t\t}
\t}''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--init', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    temporary = None
    try:
        if args.init.resolve() == args.out.resolve():
            raise ValueError('output must not replace the external input')
        source = args.init.read_text()
        guard = (REPOSITORY / 'src/userland/restore_guard.inc').read_text()
        result = prepare(source, guard)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=args.out.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.out)
        temporary = None
    except (OSError, ValueError) as error:
        parser.exit(1, f'prepare-init: {error}\n')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print('Generated guarded init; compile and device acceptance remain required')


if __name__ == '__main__':
    main()
