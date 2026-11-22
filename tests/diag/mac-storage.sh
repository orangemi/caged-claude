#!/bin/sh
# Storage diagnostics for macOS + OrbStack (docs/stateful-service.md §8).
#
# Question: can an instance's persistent environment live in an ext4 image
# file on the Mac (state/<instance>/disk.img), loop-mounted inside the Docker
# VM, so it survives Docker resets and moves between machines?
#
#   sh tests/diag/mac-storage.sh
#
# Touches only state/.diag-storage-<time>/ and throwaway Docker objects named
# caged-diag-*; all of them are removed at the end (also on Ctrl-C). The
# report is kept at state/diag-storage-<time>.txt. Needs Docker and network
# access once, to build a small Debian image with e2fsprogs.
# DIAG_WRITE_MB (default 1024) sets the sequential write size.
set -u

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
stamp=$(date +%Y%m%d-%H%M%S)
name=caged-diag-$stamp
work=$root/state/.diag-storage-$stamp
report=$root/state/diag-storage-$stamp.txt
image=caged-diag-storage:$stamp
volume=$name-volume
l3volume=$name-loop
vmdir=/var/lib/$name
mb=${DIAG_WRITE_MB:-1024}
out=$(mktemp) || exit 1
summary=$(mktemp) || exit 1
loop_benched=

mkdir -p "$work/bind" || exit 1
: >"$report"

say() { printf '%s\n' "$*" | tee -a "$report"; }
section() { say ''; say "=== $*"; }
note() { printf '%-58s %s\n' "$1" "$2" >>"$summary"; }
# run LABEL COMMAND...: record the output and exit code in the report.
run() {
    label=$1; shift
    say "--- $label"
    "$@" >"$out" 2>&1
    rc=$?
    tee -a "$report" <"$out"
    say "[exit $rc]"
    return $rc
}
c() { docker run --rm --network none -v "$work:/w" "$@"; }
priv() { docker run --rm --network none --privileged -v "$work:/w" "$@"; }
anchored() { priv --mount "type=bind,src=$vmdir,dst=/anchor,bind-propagation=rshared" "$@"; }

cleanup() {
    trap - EXIT INT TERM
    say ''
    say '=== cleanup'
    anchored "$image" sh -c 'for m in /anchor/l1 /anchor/l2; do mountpoint -q "$m" && umount "$m"; done; true' >/dev/null 2>&1
    docker run --rm --network none --privileged --pid=host "$image" \
        nsenter -t 1 -m -- sh -c "umount '$vmdir/l1' '$vmdir/l2' 2>/dev/null; true" >/dev/null 2>&1
    docker volume rm -f "$volume" "$l3volume" >/dev/null 2>&1
    if [ -s "$work/loops" ]; then
        priv "$image" sh -c 'for d in $(cat /w/loops); do losetup -d "$d" 2>/dev/null; done; true' >/dev/null 2>&1
    fi
    docker run --rm --network none -v /var/lib:/h "$image" rm -rf "/h/$name" >/dev/null 2>&1
    docker run --rm --network none -v "$root/state:/s" "$image" rm -rf "/s/.diag-storage-$stamp" >/dev/null 2>&1
    docker image rm "$image" >/dev/null 2>&1
    [ -e "$work" ] && rm -rf "$work" 2>/dev/null
    [ -e "$work" ] && say "could not remove $work; delete it by hand" || say 'removed the temporary files, volumes and image'
    section summary
    tee -a "$report" <"$summary"
    rm -f "$out" "$summary"
    say ''
    say "Report: $report"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Probe: the same checks as caged/host/stateful.py _probe, one at a time.
cat >"$work/probe.sh" <<'EOF'
t=$1/.probe-$$
mkdir -p "$t" && cd "$t" || exit 9
echo "filesystem: $(stat -f -c %T .)"
touch f
echo "new file owner (as root): $(stat -c %u:%g f)"
chown 12345:23456 f; echo "owner after chown:   $(stat -c %u:%g f)  (want 12345:23456)"
chmod 4755 f;        echo "mode after chmod:    $(stat -c %a f)  (want 4755)"
ln f hard;           echo "hard link count:     $(stat -c %h f)  (want 2)"
ln -s f soft;        echo "symlink target:      $(readlink soft)  (want f)"
mkfifo p && echo "fifo: ok"
result="$(stat -c %u:%g:%a:%h f) $(readlink soft)"
cd / && rm -rf "$t"
if [ "$result" = "12345:23456:4755:2 f" ]; then echo 'PROBE PASS'; else echo "PROBE FAIL ($result)"; exit 3; fi
EOF

cat >"$work/bench.sh" <<'EOF'
d=$1/.bench-$$
mb=$2
mkdir -p "$d" || exit 9
now() { date +%s.%N; }
span() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f s", b - a }'; }
s=$(now); dd if=/dev/zero of="$d/big" bs=1M count="$mb" conv=fsync status=none; e=$(now)
echo "sequential write ${mb} MB + fsync:   $(span "$s" "$e")"
s=$(now); cat "$d/big" >/dev/null; e=$(now)
echo "sequential read ${mb} MB (maybe cached): $(span "$s" "$e")"
rm -f "$d/big"
s=$(now); dd if=/dev/zero of="$d/sync" bs=4k count=500 oflag=dsync status=none; e=$(now)
echo "500 x 4 KB synchronous writes:     $(span "$s" "$e")"
echo "source /usr: $(find /usr | wc -l) entries, $(du -sh /usr | cut -f1)"
s=$(now); tar -C / -cf - usr | tar -C "$d" -xpf -; sync; e=$(now)
echo "copy /usr in (seed-like) + sync:   $(span "$s" "$e")"
s=$(now); ls -lR "$d/usr" >/dev/null; e=$(now)
echo "metadata walk of the copy:         $(span "$s" "$e")"
s=$(now); tar -C "$d" -cf - usr | wc -c >/dev/null; e=$(now)
echo "read the copy back as a tar:       $(span "$s" "$e")"
s=$(now); rm -rf "$d"; e=$(now)
echo "delete the copy:                   $(span "$s" "$e")"
EOF

# verify LABEL MOUNT-OPTION...: check, from an ordinary unprivileged
# container, that the mount is the ext4 image and keeps Linux metadata.
verify() {
    label=$1; shift
    if run "$label: unprivileged container sees the ext4 image" c "$@" "$image" sh -c \
        'stat -f -c "filesystem: %T" /data; ls -a /data; [ -d /data/lost+found ]'; then
        note "$label: ext4 visible to an ordinary container" yes
    else
        note "$label: ext4 visible to an ordinary container" NO
        return 1
    fi
    run "$label: markers written by earlier methods" c "$@" "$image" sh -c 'cat /data/markers 2>/dev/null || echo "(none yet)"'
    if run "$label: probe" c "$@" "$image" sh /w/probe.sh /data; then
        note "$label: ownership/setuid/links probe" PASS
    else
        note "$label: ownership/setuid/links probe" FAIL
    fi
    c "$@" "$image" sh -c "echo '$label' >>/data/markers" >/dev/null 2>&1
    if [ -z "$loop_benched" ]; then
        loop_benched=1
        run "$label: benchmark (ext4 image)" c "$@" "$image" sh /w/bench.sh /data "$mb"
    fi
    return 0
}

section environment
run 'macOS' sw_vers
run 'architecture' uname -m
run 'OrbStack' orb version
run 'Docker' docker version --format 'client {{.Client.Version}}, server {{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}}), API {{.Server.APIVersion}}'
run 'Docker host' docker info --format '{{.OperatingSystem}} | kernel {{.KernelVersion}} | storage {{.Driver}} | {{.NCPU}} CPUs | {{.MemTotal}} bytes'
say "work dir: $work"

section 'diagnostic image'
if ! run 'build Debian + e2fsprogs + util-linux' sh -c "docker build -q -t '$image' - <<'EOF'
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends e2fsprogs util-linux mount coreutils tar \
 && rm -rf /var/lib/apt/lists/*
EOF"; then
    say 'cannot build the diagnostic image; stopping'
    exit 1
fi

section '1. bind mount of a Mac directory (what state/ uses today)'
if run 'probe' c "$image" sh /w/probe.sh /w/bind; then note '1. Mac bind mount probe' PASS; else note '1. Mac bind mount probe' FAIL; fi
run 'benchmark (Mac bind mount)' c "$image" sh /w/bench.sh /w/bind "$mb"

section '2. Docker named volume (inside the VM, for comparison)'
docker volume create "$volume" >/dev/null
if run 'probe' c -v "$volume:/data" "$image" sh /w/probe.sh /data; then note '2. named volume probe' PASS; else note '2. named volume probe' FAIL; fi
run 'benchmark (named volume)' c -v "$volume:/data" "$image" sh /w/bench.sh /data "$mb"
docker volume rm -f "$volume" >/dev/null 2>&1

section '3. ext4 image file on the Mac'
if run 'create a sparse 8 GB ext4 image' c "$image" sh -c 'truncate -s 8G /w/disk.img && mkfs.ext4 -q -F -m 0 /w/disk.img && echo created'; then
    note '3. create ext4 image on the Mac' yes
else
    note '3. create ext4 image on the Mac' NO
    exit 1
fi
run 'size on the Mac (apparent / allocated)' sh -c "ls -lh '$work/disk.img' | awk '{print \"apparent \" \$5}'; du -h '$work/disk.img' | awk '{print \"allocated \" \$1}'"

section '3a. loop device inside the VM'
if run 'attach a loop device (privileged)' priv "$image" sh -c 'd=$(losetup -f --show /w/disk.img) && echo "$d" | tee -a /w/loops && losetup -l "$d"'; then
    note '3a. privileged container can attach a loop device' yes
    priv "$image" sh -c 'for d in $(cat /w/loops); do losetup -d "$d"; done; : >/w/loops' >/dev/null 2>&1
else
    note '3a. privileged container can attach a loop device' NO
fi

section 'L1. mount in the Docker host mount namespace (nsenter -t 1 -m)'
run 'host namespace: OS, tools, visibility of the Mac path' docker run --rm --network none --privileged --pid=host "$image" \
    nsenter -t 1 -m -- sh -c "head -2 /etc/os-release 2>/dev/null; uname -a; command -v mount losetup; ls -l '$work/disk.img'"
if run 'mount the image there' docker run --rm --network none --privileged --pid=host "$image" \
    nsenter -t 1 -m -- sh -c "mkdir -p '$vmdir/l1' && mount -o loop '$work/disk.img' '$vmdir/l1' && grep ' $vmdir/l1 ' /proc/self/mountinfo"; then
    note 'L1. nsenter mount' yes
    verify L1 --mount "type=bind,src=$vmdir/l1,dst=/data"
    run 'unmount' docker run --rm --network none --privileged --pid=host "$image" nsenter -t 1 -m -- umount "$vmdir/l1"
else
    note 'L1. nsenter mount' NO
fi

section 'L2. mount from a privileged helper, propagated through an rshared bind mount'
run 'create the anchor directory in the VM' c -v "$vmdir:/a" "$image" sh -c 'ls -ld /a'
if run 'helper mounts the image under the anchor, then exits' anchored "$image" sh -c \
    'mkdir -p /anchor/l2 && mount -o loop /w/disk.img /anchor/l2 && losetup -j /w/disk.img'; then
    note 'L2. helper mount' yes
    if verify L2 --mount "type=bind,src=$vmdir/l2,dst=/data"; then
        note 'L2. mount survives the helper exiting' yes
    else
        note 'L2. mount survives the helper exiting' NO
    fi
    run 'unmount' anchored "$image" sh -c 'umount /anchor/l2 && echo unmounted'
else
    note 'L2. helper mount' NO
fi

section 'L3. Docker local volume backed by a loop device'
loop=$(priv "$image" sh -c 'd=$(losetup -f --show /w/disk.img) && echo "$d" >>/w/loops && echo "$d"' 2>>"$report")
say "loop device: ${loop:-none}"
if [ -n "$loop" ] && run 'create the volume' docker volume create -d local -o type=ext4 -o "device=$loop" "$l3volume"; then
    if verify L3 --mount "type=volume,src=$l3volume,dst=/data"; then
        note 'L3. loop-backed local volume' yes
        c --mount "type=volume,src=$l3volume,dst=/data" "$image" sh -c 'mkdir -p /data/system/etc && echo sub >/data/system/etc/x' >/dev/null 2>&1
        if run 'volume-subpath mount of system/etc' c --mount "type=volume,src=$l3volume,dst=/data,volume-subpath=system/etc" "$image" cat /data/x; then
            note 'L3. volume-subpath (Docker 26+)' yes
        else
            note 'L3. volume-subpath (Docker 26+)' NO
        fi
        run 'two containers mount the volume at once' sh -c "docker run -d --rm --network none --name '$name-hold' --mount 'type=volume,src=$l3volume,dst=/data' '$image' sleep 30 >/dev/null && docker run --rm --network none --mount 'type=volume,src=$l3volume,dst=/data' '$image' ls /data; docker rm -f '$name-hold' >/dev/null"
    else
        note 'L3. loop-backed local volume' NO
    fi
    docker volume rm -f "$l3volume" >/dev/null 2>&1
else
    note 'L3. loop-backed local volume' NO
fi
priv "$image" sh -c 'for d in $(cat /w/loops 2>/dev/null); do losetup -d "$d" 2>/dev/null; done; : >/w/loops' >/dev/null 2>&1

section 'L4. Docker local volume given the Mac file directly (o=loop)'
if run 'create the volume' docker volume create -d local -o type=ext4 -o o=loop -o "device=$work/disk.img" "$l3volume" \
    && verify L4 --mount "type=volume,src=$l3volume,dst=/data"; then
    note 'L4. local volume with o=loop on the Mac file' yes
else
    note 'L4. local volume with o=loop on the Mac file' NO
fi
docker volume rm -f "$l3volume" >/dev/null 2>&1

section '3b. image file after the tests'
run 'loop devices still attached to the image' priv "$image" sh -c 'losetup -j /w/disk.img; echo "(end of list)"'
if run 'fsck (read-only)' c "$image" e2fsck -fn /w/disk.img; then note '3b. image is clean after unmounting' yes; else note '3b. image is clean after unmounting' NO; fi
run 'size on the Mac after the tests' sh -c "du -h '$work/disk.img'"
