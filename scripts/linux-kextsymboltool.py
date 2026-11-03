#!/usr/bin/env python3
"""Give Apple's symbol checker a seekable discard output on Linux.

Linux /dev/null lseek returns zero, breaking the checker's required offset.
Use a private temporary regular file and preserve every check and exit status.
Set KEXTSYMBOLTOOL_REAL to the original compiled XNU SETUP tool.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    real = Path(os.environ['KEXTSYMBOLTOOL_REAL']).resolve(strict=True)
    if real == Path(__file__).resolve():
        raise SystemExit('symbol checker wrapper cannot invoke itself')
    arguments = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix='thyme-symbol-check-') as directory:
        for index in range(len(arguments) - 1):
            if arguments[index:index + 2] == ['-output', '/dev/null']:
                arguments[index + 1] = str(Path(directory) / 'discard.symbolset')
        return subprocess.run([str(real), *arguments]).returncode


if __name__ == '__main__':
    raise SystemExit(main())
