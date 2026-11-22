#!/bin/sh
# Storage diagnostics, round 2: the risks of an ext4 image on the Mac served
# as a loop-backed Docker local volume (method L3 of mac-storage.sh).
#
#   sh tests/diag/mac-storage-2.sh              # automatic tests only
#   sh tests/diag/mac-storage-2.sh --vm-restart # also stop and start OrbStack
#
# --vm-restart runs `orb stop` while a container is writing, which stops
# every container you have running in OrbStack; it asks before doing so.
# Touches only state/.diag-storage2-<time>/ and Docker objects named
# caged-diag2-*; all are removed at the end. Report: state/diag-storage2-<time>.txt
set -u

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
stamp=$(date +%Y%m%d-%H%M%S)
name=caged-diag2-$stamp
work=$root/state/.diag-storage2-$stamp
report=$root/state/diag-storage2-$stamp.txt
image=caged-diag-storage2:$stamp
claude_image=caged-claude-claude:local
vm_restart=
[ "${1:-}" = --vm-restart ] && vm_restart=1
out=$(mktemp) || exit 1
summary=$(mktemp) || exit 1
mkdir -p "$work" || exit 1
: >"$report"

say() { printf '%s\n' "$*" | tee -a "$report"; }
section() { say ''; say "=== $*"; }
note() { printf '%-60s %s\n' "$1" "$2" >>"$summary"; }
run() {  # run LABEL COMMAND...
    say "--- $1"; shift
    "$@" >"$out" 2>&1
    status=$?
    tee -a "$report" <"$out"
    say "[exit $status]"
    return $status
}
c() { docker run --rm --network none -v "$work:/w" "$@"; }
priv() { docker run --rm --network none --privileged -v "$work:/w" "$@"; }
mac_size() { du -k "$work/$1" | awk '{printf "%.0f MB", $1 / 1024}'; }
# attach FILE: print the new loop device and remember it for cleanup.
attach() { file=$1; shift; priv "$image" sh -c "d=\$(losetup -f --show $* /w/$file) && echo \"\$d\""; }
detach() { priv "$image" losetup -d "$1" >/dev/null 2>&1; }
# volume NAME DEVICE [MOUNT-OPTIONS]
volume() { docker volume create -d local -o type=ext4 -o "device=$2" ${3:+-o "o=$3"} "$name-$1" >/dev/null; }
vol() { printf 'type=volume,src=%s-%s,dst=/data' "$name" "$1"; }
mkimage() { c "$image" sh -c "truncate -s $2 /w/$1 && mkfs.ext4 -q -F -m 0 $3 /w/$1"; }
now() { date +%s; }

cleanup() {
    trap - EXIT INT TERM
    section cleanup
    docker ps -aq --filter "name=$name" | xargs docker rm -f >/dev/null 2>&1
    docker volume ls -q --filter "name=$name" | xargs docker volume rm -f >/dev/null 2>&1
    priv "$image" sh -c 'for f in /w/*.img; do losetup -j "$f" | cut -d: -f1; done | while read -r d; do losetup -d "$d"; done; true' >/dev/null 2>&1
    docker run --rm --network none -v "$root/state:/s" "$image" rm -rf "/s/.diag-storage2-$stamp" >/dev/null 2>&1
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

section environment
run 'versions' sh -c "sw_vers -productVersion; uname -m; orb version | head -1; docker version --format 'docker {{.Server.Version}}'"

section 'diagnostic image'
if ! run 'build Debian + e2fsprogs + util-linux' sh -c "docker build -q -t '$image' - <<'EOF'
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends e2fsprogs util-linux mount coreutils tar \
 && rm -rf /var/lib/apt/lists/*
EOF"; then
    say 'cannot build the diagnostic image; stopping'
    exit 1
fi

# ---------------------------------------------------------------------------
section 'G. seed a real Claude environment into the image'
mkimage seed.img 64G ''
dev=$(attach seed.img)
volume seed "$dev"
if docker image inspect "$claude_image" >/dev/null 2>&1; then
    run 'size of the Claude image content' docker run --rm --network none --entrypoint /bin/sh "$claude_image" -c \
        'du -sh /etc /usr /var /opt /root /home/claude 2>/dev/null; find /etc /usr /var /opt /root /home/claude | wc -l'
    start=$(now)
    if run 'seed: tar the image content into the volume (as cage up does)' docker run --rm --network none --user 0:0 \
        --mount "$(vol seed)" --entrypoint /bin/sh "$claude_image" -c \
        'mkdir -p /data/system /data/home && cd / && tar --numeric-owner --xattrs --exclude=./etc/hosts --exclude=./etc/hostname --exclude=./etc/resolv.conf -cpf - ./etc ./usr ./var ./opt ./srv ./root | tar -C /data/system --numeric-owner --xattrs -xpf - && cd /home/claude && tar --numeric-owner --xattrs -cpf - . | tar -C /data/home --numeric-owner --xattrs -xpf - && sync'; then
        say "seed took $(( $(now) - start )) s"
        note "G. seed the real Claude image ($(( $(now) - start )) s)" yes
    else
        note 'G. seed the real Claude image' NO
    fi
    run 'start a shell on the seeded system (volume-subpath mounts)' docker run --rm --network none --entrypoint /bin/bash \
        --mount "type=volume,src=$name-seed,dst=/usr,volume-subpath=system/usr" \
        --mount "type=volume,src=$name-seed,dst=/etc,volume-subpath=system/etc" \
        "$claude_image" -c 'node --version && python3 --version && ls -l /usr/bin/sudo && stat -c "%U %a" /usr/bin/sudo'
else
    say "$claude_image not found; run ./cage build first to include this test"
    note 'G. seed the real Claude image' skipped
fi
docker volume rm -f "$name-seed" >/dev/null
detach "$dev"
say "image on the Mac: $(mac_size seed.img)"
rm -f "$work/seed.img" 2>/dev/null || c "$image" rm -f /w/seed.img

# ---------------------------------------------------------------------------
section 'E. loop direct I/O'
mkimage dio.img 8G ''
for mode in off on; do
    dev=$(attach dio.img "--direct-io=$mode")
    run "direct-io=$mode: loop status" priv "$image" losetup -l "$dev"
    volume "dio-$mode" "$dev"
    run "direct-io=$mode: 1 GB write + fsync, 500 sync writes, copy /usr" c --mount "$(vol "dio-$mode")" "$image" sh -c '
        s=$(date +%s.%N); dd if=/dev/zero of=/data/big bs=1M count=1024 conv=fsync status=none; e=$(date +%s.%N)
        awk -v a=$s -v b=$e "BEGIN { printf \"1 GB write + fsync: %.2f s\n\", b - a }"; rm /data/big
        s=$(date +%s.%N); dd if=/dev/zero of=/data/s bs=4k count=500 oflag=dsync status=none; e=$(date +%s.%N)
        awk -v a=$s -v b=$e "BEGIN { printf \"500 x 4 KB sync writes: %.2f s\n\", b - a }"; rm /data/s
        s=$(date +%s.%N); tar -C / -cf - usr | tar -C /data -xpf -; sync; e=$(date +%s.%N)
        awk -v a=$s -v b=$e "BEGIN { printf \"copy /usr + sync: %.2f s\n\", b - a }"; rm -rf /data/usr'
    docker volume rm -f "$name-dio-$mode" >/dev/null
    detach "$dev"
done
note 'E. direct-io comparison' 'see report'
c "$image" rm -f /w/dio.img

# ---------------------------------------------------------------------------
section 'D. returning deleted space to the Mac'
mkimage trim.img 8G ''
dev=$(attach trim.img)
volume trim-online "$dev" discard
run 'online discard: write 1 GB, delete, sync' c --mount "$(vol trim-online)" "$image" sh -c \
    'dd if=/dev/urandom of=/data/big bs=1M count=1024 conv=fsync status=none && echo written'
say "after writing: $(mac_size trim.img)"
c --mount "$(vol trim-online)" "$image" sh -c 'rm /data/big && sync' >/dev/null 2>&1
sleep 3
after_discard=$(mac_size trim.img)
say "after deleting with o=discard: $after_discard"
docker volume rm -f "$name-trim-online" >/dev/null
volume trim-batch "$dev"
c --mount "$(vol trim-batch)" "$image" sh -c 'dd if=/dev/urandom of=/data/big bs=1M count=1024 conv=fsync status=none && rm /data/big && sync' >/dev/null 2>&1
say "after writing and deleting without discard: $(mac_size trim.img)"
run 'fstrim from a privileged container' priv --mount "$(vol trim-batch)" "$image" fstrim -v /data
sleep 3
say "after fstrim: $(mac_size trim.img)"
note "D. online discard (after delete: $after_discard)" 'see report'
note "D. fstrim (after: $(mac_size trim.img))" 'see report'
docker volume rm -f "$name-trim-batch" >/dev/null
detach "$dev"
c "$image" rm -f /w/trim.img

# ---------------------------------------------------------------------------
section 'F. growing the image while it is mounted'
mkimage grow.img 4G ''
dev=$(attach grow.img)
volume grow "$dev"
docker run -d --network none --name "$name-grow-hold" --mount "$(vol grow)" "$image" sleep 300 >/dev/null
run 'size before' docker exec "$name-grow-hold" df -h /data
if run 'truncate to 8G, refresh the loop device, resize2fs online' priv "$image" sh -c \
    "truncate -s 8G /w/grow.img && losetup -c $dev && resize2fs $dev"; then
    run 'size after (same running container)' docker exec "$name-grow-hold" df -h /data
    note 'F. online grow' yes
else
    note 'F. online grow' NO
fi
docker rm -f "$name-grow-hold" >/dev/null
docker volume rm -f "$name-grow" >/dev/null
detach "$dev"
c "$image" rm -f /w/grow.img

# ---------------------------------------------------------------------------
section 'A. refusing a second mount of the same image (ext4 multi-mount protection)'
mkimage mmp.img 8G '-O mmp'
run 'MMP settings' c "$image" sh -c 'dumpe2fs -h /w/mmp.img 2>/dev/null | grep -i mmp'
first=$(attach mmp.img)
volume mmp-a "$first"
start=$(now)
docker run -d --network none --name "$name-mmp-hold" --mount "$(vol mmp-a)" "$image" sleep 300 >/dev/null
say "first mount of a clean MMP filesystem took $(( $(now) - start )) s"
second=$(attach mmp.img)
say "second loop device on the same file: ${second:-none (losetup refused)}"
if [ -n "$second" ]; then
    volume mmp-b "$second"
    start=$(now)
    if run 'mount the same image through the second device' c --mount "$(vol mmp-b)" "$image" ls /data; then
        note 'A. MMP refuses a second mount' NO
    else
        note "A. MMP refuses a second mount (after $(( $(now) - start )) s)" yes
    fi
    docker volume rm -f "$name-mmp-b" >/dev/null
    detach "$second"
fi
run 'kernel log about MMP' priv "$image" sh -c 'dmesg 2>/dev/null | grep -i mmp | tail -5'
docker kill "$name-mmp-hold" >/dev/null 2>&1; docker rm -f "$name-mmp-hold" >/dev/null 2>&1
start=$(now)
run 'remount after the holder was killed' c --mount "$(vol mmp-a)" "$image" ls /data
say "remount took $(( $(now) - start )) s"
note "A. MMP remount after a kill ($(( $(now) - start )) s)" 'see report'
docker volume rm -f "$name-mmp-a" >/dev/null
detach "$first"
c "$image" rm -f /w/mmp.img

# ---------------------------------------------------------------------------
section 'B. docker kill during writes'
cat >"$work/writer.sh" <<'EOF'
# Writes numbered files, each fsynced, and prints the number once it is durable;
# a large unsynced file is written alongside to keep dirty pages in flight.
mkdir -p /data/w
dd if=/dev/urandom of=/data/w/bulk bs=1M count=4096 status=none &
i=0
while :; do
    i=$((i + 1))
    head -c 65536 /dev/urandom >/data/w/$i && dd if=/data/w/$i of=/data/w/$i conv=notrunc,fsync status=none 2>/dev/null
    echo "$i"
done
EOF
mkimage kill.img 16G ''
dev=$(attach kill.img)
volume kill "$dev"
docker run -d --network none --name "$name-writer" --mount "$(vol kill)" -v "$work:/w" "$image" sh /w/writer.sh >/dev/null
sleep 8
docker kill "$name-writer" >/dev/null
durable=$(docker logs "$name-writer" 2>/dev/null | tail -1)
docker rm -f "$name-writer" >/dev/null
say "last file reported durable before the kill: $durable"
if run 'every durable file survived' c --mount "$(vol kill)" "$image" sh -c \
    "missing=0; i=1; while [ \$i -le $durable ]; do [ -s /data/w/\$i ] || missing=\$((missing + 1)); i=\$((i + 1)); done; echo \"missing: \$missing\"; [ \$missing -eq 0 ]"; then
    note 'B. fsynced files survive docker kill' yes
else
    note 'B. fsynced files survive docker kill' NO
fi
docker volume rm -f "$name-kill" >/dev/null
detach "$dev"
if run 'fsck after the kill and a clean unmount' c "$image" e2fsck -fn /w/kill.img; then
    note 'B. filesystem clean after docker kill' yes
else
    note 'B. filesystem clean after docker kill' NO
fi

# ---------------------------------------------------------------------------
section 'C. OrbStack stopped during writes (--vm-restart)'
if [ -z "$vm_restart" ]; then
    say 'skipped; rerun with --vm-restart to include it'
    note 'C. OrbStack stop during writes' skipped
else
    printf 'This stops OrbStack and every container in it, then starts it again. Continue? [y/N] '
    read -r answer
    if [ "$answer" != y ] && [ "$answer" != Y ]; then
        note 'C. OrbStack stop during writes' skipped
    else
        dev=$(attach kill.img)
        volume vm "$dev"
        docker run -d --network none --name "$name-vmwriter" --mount "$(vol vm)" -v "$work:/w" "$image" sh -c 'rm -rf /data/w; sh /w/writer.sh' >/dev/null
        sleep 8
        durable=$(docker logs "$name-vmwriter" 2>/dev/null | tail -1)
        say "durable before the stop (lower bound): $durable"
        run 'orb stop' orb stop
        run 'orb start' orb start
        start=$(now)
        while ! docker info >/dev/null 2>&1 && [ $(( $(now) - start )) -lt 120 ]; do sleep 2; done
        say "Docker back after $(( $(now) - start )) s"
        run 'loop devices after the restart' priv "$image" sh -c 'losetup -l; echo "(end of list)"'
        run 'the old volume after the restart' docker volume inspect --format '{{.Name}} {{.Options}}' "$name-vm"
        docker rm -f "$name-vmwriter" >/dev/null 2>&1
        docker volume rm -f "$name-vm" >/dev/null 2>&1
        if run 'fsck before the journal replay (read-only, expected to want recovery)' c "$image" e2fsck -fn /w/kill.img; then :; fi
        dev=$(attach kill.img)
        volume vm2 "$dev"
        if run 'mount replays the journal; durable files present' c --mount "$(vol vm2)" "$image" sh -c \
            "missing=0; i=1; while [ \$i -le $durable ]; do [ -s /data/w/\$i ] || missing=\$((missing + 1)); i=\$((i + 1)); done; echo \"missing: \$missing\"; [ \$missing -eq 0 ]"; then
            note 'C. fsynced files survive an OrbStack stop' yes
        else
            note 'C. fsynced files survive an OrbStack stop' NO
        fi
        docker volume rm -f "$name-vm2" >/dev/null
        detach "$dev"
        if run 'fsck after the replay' c "$image" e2fsck -fn /w/kill.img; then
            note 'C. filesystem clean after an OrbStack stop' yes
        else
            note 'C. filesystem clean after an OrbStack stop' NO
        fi
    fi
fi

# ---------------------------------------------------------------------------
section 'H. copying the image on the Mac (moving to another machine)'
say "original: apparent $(ls -lh "$work/kill.img" | awk '{print $5}'), allocated $(mac_size kill.img)"
for tool in 'cp' 'cp -c' 'ditto'; do
    rm -f "$work/copy.img"
    start=$(now)
    $tool "$work/kill.img" "$work/copy.img" 2>>"$report"
    say "$tool: $(( $(now) - start )) s, allocated $(mac_size copy.img)"
done
if run 'the copy is a valid filesystem' c "$image" e2fsck -fn /w/copy.img; then note 'H. a copied image is valid' yes; else note 'H. a copied image is valid' NO; fi
rm -f "$work/copy.img" "$work/kill.img" 2>/dev/null
