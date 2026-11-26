#!/usr/bin/env python3
"""Active filesystem-boundary probe, run as the Claude user.

The launcher writes a random token into the Claude state directory (shared) and
another file into a host-private directory. The cage must read and answer the
first and must not be able to open the second at its host path."""
import json
import os
from pathlib import Path
import re
import sys


def challenge(name, token, private_path):
    if not re.fullmatch(r'\.caged-audit-[0-9a-f]{32}', name) or not re.fullmatch(r'[0-9a-f]{64}', token):
        raise RuntimeError('Invalid challenge')
    shared = Path(os.environ['CLAUDE_CONFIG_DIR']) / name
    shared_read = shared.read_text() == token
    shared.with_name(name + '.reply').write_text(token[::-1])
    try:
        with open(private_path, 'rb'):
            private_readable = True
    except OSError:
        private_readable = False
    return {'shared_read': shared_read, 'shared_reply_written': True, 'private_readable': private_readable}


if __name__ == '__main__':
    try:
        print(json.dumps(challenge(*sys.argv[1:4])))
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        raise SystemExit(1)
