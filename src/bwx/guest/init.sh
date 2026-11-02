#!/bin/sh
# bwx PID 1 -- generic, identical for every run of every case.
#
# Injected into the rootfs once at convert time. Everything that varies per run
# (token, timeouts, wait mode) is passed on the kernel command line, and everything
# case-specific (actions, framework) lives on the payload disk. The delivery image
# is therefore never modified after conversion, which is what makes repeated
# measurements of the same case comparable.
#
# Host-side contract: src/bwx/serialio.py
#   BWX_BOOT:<token>                    PID 1 up, /work mounted
#   BWX_ERROR:<token>:<stage>:<detail>  fatal problem, the run cannot proceed
#
# POSIX sh on purpose: converted images are not guaranteed to have bash at a
# predictable path, but /bin/sh is verified at convert time.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

bwx_cmdline() {
    tr ' ' '\n' < /proc/cmdline | sed -n "s/^bwx_$1=//p" | head -1
}
TOKEN=
[ -n "$TOKEN" ] || TOKEN=notoken

bwx_fail() {
    printf 'BWX_ERROR:%s:%s:%s\n' "$TOKEN" "$1" "$2"
    printf '{"status":"initialization_failed","error":"%s: %s"}\n' "$1" "$2" > /work/result/summary.json 2>/dev/null
    sync
    reboot -f 2>/dev/null
    # Images without a reboot binary still need to end the VM. `reboot=k panic=1`
    # on the kernel line makes Firecracker exit on the sysrq reset too.
    echo b > /proc/sysrq-trigger 2>/dev/null
    while :; do sleep 1; done
}

mkdir -p /proc /sys /dev /dev/pts /dev/shm /run /tmp /work /opt
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mount -t devpts devpts /dev/pts 2>/dev/null
mount -t tmpfs -o mode=1777,size=512m tmpfs /dev/shm 2>/dev/null
mount -t tmpfs tmpfs /run 2>/dev/null
chmod 1777 /tmp

# /proc must be mounted before the kernel command line can be read.
TOKEN=$(bwx_cmdline token)
[ -n "$TOKEN" ] || TOKEN=notoken

printf 'BWX_BOOT:%s\n' "$TOKEN"

# Payload disk: framework, actions, and the result area. Nothing runs without it.
# The rootfs stub may already have mounted it to find this file; only mount when
# /work does not hold the payload yet (stacking a mount would just shadow it).
if [ ! -f /work/supervise.sh ]; then
    mount /dev/vdb /work || bwx_fail mount_payload /dev/vdb
fi
mkdir -p /work/result

# Tools disk (optional, read-only, shared by every VM on the host): supplies a
# python3 for images that have none. Never shadows an image-provided interpreter.
# The mount point must exist first -- `mount` does not create it, and the images
# have no /opt/bwx-tools.
mkdir -p /opt/bwx-tools
if ! mount -o ro /dev/vdc /opt/bwx-tools 2>/dev/null; then
    printf 'BWX_TOOLS_MOUNT_FAILED\n'
fi

# Image environment (PATH/HOME/language toolchain) captured at convert time.
# Sourced BEFORE the tools dir is prepended: image-env.sh assigns PATH outright,
# so prepending first would be silently discarded and every python step would
# exit 127.
[ -f /bwx/image-env.sh ] && . /bwx/image-env.sh 2>/dev/null
export HOME=${HOME:-/root}
mkdir -p /work/result
if ! command -v python3 >/dev/null 2>&1 && [ -x /opt/bwx-tools/bin/python3 ]; then
    PATH=/opt/bwx-tools/bin:$PATH
    export PATH
    printf 'BWX_PYTHON_FROM_TOOLS\n'
fi

# Loopback: the VM has no NIC, but guests that bind 127.0.0.1 need lo up. The
# images carry neither `ip` nor `ifconfig`, hence the static helper.
if [ -x /work/tools/loopback ]; then
    /work/tools/loopback || printf 'BWX_ERROR:%s:loopback:nonzero\n' "$TOKEN"
elif [ -x /opt/bwx-tools/bin/loopback ]; then
    /opt/bwx-tools/bin/loopback || printf 'BWX_ERROR:%s:loopback:nonzero\n' "$TOKEN"
fi

if [ ! -f /work/supervise.sh ]; then
    bwx_fail missing_supervisor /work/supervise.sh
fi

/bin/bash /work/supervise.sh
rc=$?

printf 'BWX_EXIT:%s:%s\n' "$TOKEN" "$rc"
sync
reboot -f 2>/dev/null
echo b > /proc/sysrq-trigger 2>/dev/null
while :; do sleep 1; done
