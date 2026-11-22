#!/bin/sh
# Shared entry for ./start, ./stop, ./status, ./claude and ./shell.
#
#   run.sh <python module> [arguments...]
#
# The host needs only a POSIX shell and the Docker CLI: the Python launcher
# runs in a small container that talks to the host Docker daemon. Set
# CAGED_NATIVE=1 to run it with the host's python3 (needs PyYAML) instead.
set -eu

module=$1
shift
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)

if [ "${CAGED_NATIVE:-}" = 1 ] || [ "${CAGED_LAUNCHER:-}" = 1 ]; then
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
    # Put the project first on sys.path so a `caged` package in the caller's
    # directory cannot shadow it, while relative arguments keep the caller's cwd.
    exec python3 -c "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); module=sys.argv.pop(1); sys.argv[0]=module; runpy.run_module(module,run_name='__main__')" "$root" "$module" "$@"
fi

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail 'Docker CLI not found. Install OrbStack, Docker Desktop or Docker Engine.'

case $root in
    *,*) fail "Project path cannot contain a comma: $root" ;;
esac

dockerfile=$root/containers/launcher/Dockerfile
image=caged-browser-launcher:$(cksum < "$dockerfile" | cut -d ' ' -f 1)
if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker info >/dev/null 2>&1 || fail 'Docker daemon is not reachable. Start OrbStack/Docker and try again.'
    printf 'Building launcher image %s (first run only)...\n' "$image" >&2
    docker build -q -t "$image" - < "$dockerfile" >/dev/null || fail 'Launcher image build failed.'
fi

# The daemon socket as seen by the daemon's host. OrbStack and Docker Desktop
# expose it at /var/run/docker.sock inside containers whatever DOCKER_HOST
# says on macOS; on Linux a unix DOCKER_HOST (e.g. rootless) is a real path.
sock=${CAGED_DOCKER_SOCK:-}
if [ -z "$sock" ]; then
    case ${DOCKER_HOST:-} in
        unix://*) [ "$(uname -s)" = Linux ] && sock=${DOCKER_HOST#unix://} ;;
        '') ;;
        *) fail "DOCKER_HOST=$DOCKER_HOST is not a local socket; set CAGED_DOCKER_SOCK, or CAGED_NATIVE=1 to run on the host." ;;
    esac
    sock=${sock:-/var/run/docker.sock}
fi

under() {  # under <path> <directory>: path is the directory or inside it
    case $1/ in "$2"/*) return 0 ;; esac
    return 1
}

# Append launcher options after the caller's arguments, then rotate the
# caller's arguments back to the end; POSIX sh has only one array.
count=$#
set -- "$@" run --rm -i --init \
    --mount "type=bind,src=$sock,dst=/var/run/docker.sock" \
    --mount "type=bind,src=$root,dst=$root" \
    -e HOME -e TERM -e "CAGED_HOST_UID=$(id -u)" -e "CAGED_HOST_GID=$(id -g)" \
    -w "$PWD"

# Settings, node files and workspaces are validated inside the launcher, so
# it needs to see them at their host paths. Read-only: it never edits them.
home=${HOME:-}
if [ -n "$home" ] && [ "$home" != / ]; then
    set -- "$@" --mount "type=bind,src=$home,dst=$home,readonly"
fi
if ! under "$PWD" "$root" && { [ -z "$home" ] || [ "$home" = / ] || ! under "$PWD" "$home"; }; then
    set -- "$@" --mount "type=bind,src=$PWD,dst=$PWD,readonly"
fi
# Extra host directories, colon-separated, e.g. workspaces outside $HOME.
old_ifs=$IFS
IFS=:
for extra in ${CAGED_MOUNTS:-}; do
    [ -n "$extra" ] && set -- "$@" --mount "type=bind,src=$extra,dst=$extra,readonly"
done
IFS=$old_ifs

for name in $(env | sed -n 's/^\(CAGED_BROWSER_[A-Za-z0-9_]*\)=.*/\1/p'); do
    set -- "$@" -e "$name"
done

if [ "$(uname -s)" = Linux ]; then
    # On macOS the socket path is resolved inside the Docker VM, so only a
    # Linux host can check it. Files the launcher creates (config/.claude)
    # stay owned by the caller.
    [ -S "$sock" ] || fail "Docker socket not found at $sock; set CAGED_DOCKER_SOCK."
    socket_group=$(stat -c %g "$sock")
    set -- "$@" --user "$(id -u):$(id -g)" --group-add "$socket_group"
fi
if [ -t 0 ] && [ -t 1 ]; then
    set -- "$@" -t
fi
set -- "$@" "$image" sh "$root/containers/launcher/run.sh" "$module"

while [ "$count" -gt 0 ]; do
    argument=$1
    shift
    set -- "$@" "$argument"
    count=$((count - 1))
done
exec docker "$@"
