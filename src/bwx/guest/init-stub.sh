#!/bin/sh
# bwx rootfs stub -- the ONLY guest code baked into the delivery image.
#
# Kept deliberately minimal and stable: it mounts what is needed to reach the
# payload disk, then hands over to /work/init.sh, which carries all the real
# logic. Iterating on the guest framework therefore never requires rebuilding a
# multi-gigabyte rootfs (a conversion takes minutes; the payload disk takes
# milliseconds).
#
# If /work/init.sh is missing the run cannot proceed; announce it on the serial
# line and reset so the host sees a failure instead of a silent 0-exit.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mkdir -p /proc /sys /dev /work
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sysfs /sys 2>/dev/null
mount -t devtmpfs devtmpfs /dev 2>/dev/null

BWX_TOKEN=$(tr ' ' '\n' < /proc/cmdline 2>/dev/null | sed -n 's/^bwx_token=//p' | head -1)
[ -n "$BWX_TOKEN" ] || BWX_TOKEN=notoken

if [ ! -f /work/init.sh ]; then
    # The payload disk is not mounted yet here; /work is on /dev/vdb.
    mount /dev/vdb /work 2>/dev/null
fi
if [ ! -f /work/init.sh ]; then
    printf 'BWX_ERROR:%s:stub:no_payload_init\n' "$BWX_TOKEN"
    printf 'BWX_EXIT:%s:90\n' "$BWX_TOKEN"
    sync
    reboot -f 2>/dev/null
    echo b > /proc/sysrq-trigger 2>/dev/null
    while :; do sleep 1; done
fi

# Everything from here on lives on the payload disk and is versioned with the
# experiment code, not with the image.
exec /bin/sh /work/init.sh
