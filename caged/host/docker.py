"""Docker process calls and project/session ownership checks."""
import json
import subprocess
import time
from .errors import CageError
from .paths import ROOT, LABEL


def call(args, *, check=True, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1800:]
        raise CageError(detail or f'{args[0]} failed')
    return result


def docker(*args, **kwargs):
    return call(['docker', *args], **kwargs)


def inspect(name):
    result = docker('inspect', name, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def stop_managed(name, session_id=None):
    current = inspect(name)
    if current is None:
        return
    if current.get('Config', {}).get('Labels', {}).get(LABEL) != str(ROOT):
        raise CageError(f'Refusing to stop an unmanaged container named {name}')
    if session_id and current['Config']['Labels'].get('io.caged-browser.session') != session_id:
        return
    docker('stop', '-t', '12', current['Id'], check=False, timeout=20)
    # --rm removal can lag behind stop. Return only when the old name is free;
    # never wait on or remove a replacement container from another launcher.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        remaining = inspect(name)
        if remaining is None or remaining['Id'] != current['Id']:
            return
        time.sleep(0.1)
    raise CageError('Container did not finish removal: ' + name)


def wait_running(name, timeout, on_poll=None, interval=0.2):
    """Return the container state once it runs, or None after the timeout.

    on_poll runs after each unsuccessful check and may raise to abort early."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = inspect(name)
        if state and state['State']['Running']:
            return state
        if on_poll is not None:
            on_poll()
        time.sleep(interval)
    return None


# Atomically writes stdin to argv[1] with owner-only permissions, so secrets
# reach a private tmpfs without passing through Docker env or arguments.
PRIVATE_WRITER = ("import os,sys; os.umask(0o077); p=sys.argv[1]; f=open(p+'.tmp','w'); "
                  "f.write(sys.stdin.read()); f.close(); os.rename(p+'.tmp',p)")


def write_private(container, path, payload, timeout):
    return subprocess.run(['docker', 'exec', '-i', container, 'python3', '-c', PRIVATE_WRITER, path],
                          input=payload, text=True, capture_output=True, timeout=timeout)


def ensure_names_available(names):
    for name in names:
        if inspect(name) is not None:
            raise CageError(f'Container {name} already exists. Use ./status to inspect it or ./stop to stop this instance.')
