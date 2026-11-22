"""Hash the actual Dockerfile COPY inputs recursively and build from one context."""
import hashlib
import shlex
import subprocess
from . import docker as engine
from .errors import CageError
from .paths import ROOT

IMAGES = {'network': 'caged-claude-network:local',
          'browser': 'caged-claude-browser:local',
          'claude': 'caged-claude-claude:local',
          # Helper for the image state backend; see caged/host/stateful.py.
          'storage': 'caged-claude-storage:local'}


def input_files(role):
    dockerfile = ROOT / 'containers' / role / 'Dockerfile'
    files = {dockerfile, ROOT / '.dockerignore'}
    source = dockerfile.read_text().replace('\\\n', ' ')
    for line in source.splitlines():
        fields = shlex.split(line, comments=True)
        if not fields or fields[0].upper() != 'COPY':
            continue
        if any(field.startswith('--from=') for field in fields):
            continue
        fields = [field for field in fields[1:] if not field.startswith('--')]
        if len(fields) < 2:
            raise CageError('Invalid local COPY instruction in ' + role)
        for name in fields[:-1]:
            path = ROOT / name
            if not path.exists():
                raise CageError('Missing image input: ' + name)
            relative = path.resolve().relative_to(ROOT.resolve())
            if relative.parts[0] not in ('caged', 'containers'):
                raise CageError('Image input is outside the public source directories: ' + name)
            candidates = path.rglob('*') if path.is_dir() else [path]
            for candidate in candidates:
                if candidate.is_file() and '__pycache__' not in candidate.parts and candidate.suffix != '.pyc':
                    files.add(candidate)
    return sorted(files)


def source_hash(role):
    digest = hashlib.sha256()
    for source in input_files(role):
        digest.update(str(source.relative_to(ROOT)).encode() + b'\0')
        digest.update(source.read_bytes())
    return digest.hexdigest()


def build(role='network', force=False):
    image = IMAGES[role]
    expected = source_hash(role)
    actual = engine.docker('image', 'inspect', image, '--format', '{{index .Config.Labels "io.caged-claude.source"}}', check=False)
    if force or actual.returncode or actual.stdout.strip() != expected:
        # --pull re-resolves base images for the daemon's platform instead of
        # trusting a local tag, which may hold another architecture's image.
        # Builds only run when the inputs change, so the extra lookup is rare.
        result = subprocess.run(['docker', 'build', '--pull', '--label', 'io.caged-claude.source=' + expected,
                                 '-t', image, '-f', str(ROOT / 'containers' / role / 'Dockerfile'), str(ROOT)])
        if result.returncode:
            raise CageError(role + ' image build failed')
