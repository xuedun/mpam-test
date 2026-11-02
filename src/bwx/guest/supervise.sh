#!/bin/bash
# bwx guest supervisor -- owns timing, the case budget and the result summary.
#
# Runs the action loop (run.sh) in its own process group so the budget can cut it
# off without losing the statistics of the steps that did complete. Pure bash +
# coreutils/awk: the converted images are not guaranteed to contain python3
# (3 of the 4 sample images have no interpreter at all).
#
# Serial contract (host side: src/bwx/serialio.py):
#   BWX_READY:<token>:<steps>:<crc>:<bytes>   inputs verified inside the guest
#   BWX_RESULT:<token>:<crc>:<json>           final summary, crc guards the json
# Everything else is emitted by run.sh.
set -u

TOKEN=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_token=//p' | head -1)
CASE_TIMEOUT=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_case_timeout=//p' | head -1)
BOOT_DEADLINE=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_boot_timeout=//p' | head -1)
ACTION_TIMEOUT=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_action_timeout=//p' | head -1)
WAIT_MODE=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_wait=//p' | head -1)
CASE_TIMEOUT=${CASE_TIMEOUT:-1800}
BOOT_DEADLINE=${BOOT_DEADLINE:-120}
ACTION_TIMEOUT=${ACTION_TIMEOUT:-600}
WAIT_MODE=${WAIT_MODE:-off}

OUT=/work/result
STEPS=/work/steps.txt
mkdir -p "$OUT/actions"

now_ns() { date +%s%N; }

bwx_error() { printf 'BWX_ERROR:%s:%s:%s\n' "$TOKEN" "$1" "$2"; }

# ------------------------------------------------------------------ integrity
# `cksum` is POSIX/coreutils and exists in every converted image, so it is the
# one digest the host and the guest can compute identically. Comparing it against
# the value the host injected proves the payload disk really reached this VM
# (debugfs/mkfs write failures are known to exit 0).
# No process substitution here: it needs /dev/fd, which devtmpfs does not provide.
CKSUM_LINE=$(cksum "$STEPS" 2>/dev/null || echo "-1 -1 -")
STEPS_CRC=$(printf '%s' "$CKSUM_LINE" | awk '{print $1}')
STEPS_BYTES=$(printf '%s' "$CKSUM_LINE" | awk '{print $2}')
if [ "$STEPS_CRC" = "-1" ] || [ -z "$STEPS_CRC" ]; then
    bwx_error missing_steps "$STEPS"
    printf '{"status":"initialization_failed","error":"steps.txt missing"}\n' > "$OUT/summary.json"
    exit 90
fi
TOTAL_STEPS=$(wc -l < "$STEPS" | tr -d ' ')
printf 'BWX_READY:%s:%s:%s:%s\n' "$TOKEN" "$TOTAL_STEPS" "$STEPS_CRC" "$STEPS_BYTES"

# --------------------------------------------------------------- action loop
# run.sh inherits this stdout (= ttyS0), so its markers reach the host directly.
# setsid gives it a private process group for budget enforcement.
LOOP_START=$(now_ns)
setsid /bin/bash /work/run.sh &
LOOP_PID=$!
LOOP_PGID=$(ps -o pgid= -p "$LOOP_PID" 2>/dev/null | tr -d ' ' || echo "$LOOP_PID")
[ -n "$LOOP_PGID" ] || LOOP_PGID=$LOOP_PID

kill_loop() {
    kill -TERM -- "-$LOOP_PGID" 2>/dev/null
    sleep 1
    kill -KILL -- "-$LOOP_PGID" 2>/dev/null
    kill -KILL "$LOOP_PID" 2>/dev/null
}

# Poll while the loop runs; enforce the initialization deadline before the first
# action starts and the case budget afterwards. 0.2s resolution costs nothing
# measurable next to actions that take seconds.
STAGE=init
BUDGET_NS=$(( CASE_TIMEOUT * 1000000000 ))
BOOT_NS=$(( BOOT_DEADLINE * 1000000000 ))
STARTED_NS=0
TIMED_OUT=0
while kill -0 "$LOOP_PID" 2>/dev/null; do
    sleep 0.2
    NOW=$(now_ns)
    if [ "$STAGE" = init ]; then
        if [ -f "$OUT/.case_start" ]; then
            STAGE=case
            STARTED_NS=$(cat "$OUT/.case_start")
        elif [ $(( NOW - LOOP_START )) -ge "$BOOT_NS" ]; then
            TIMED_OUT=1; STAGE=init_timeout; kill_loop; break
        fi
    else
        if [ $(( NOW - STARTED_NS )) -ge "$BUDGET_NS" ]; then
            TIMED_OUT=1; STAGE=budget; kill_loop; break
        fi
    fi
done
wait "$LOOP_PID" 2>/dev/null
LOOP_RC=$?
STOP_NS=$(now_ns)
rm -f "$OUT/.case_start"

# ------------------------------------------------------------------ summary
# events.jsonl is appended by run.sh after every step, so a budget cut still
# leaves complete per-action timings behind. awk keeps this dependency-free.
SUMMARY=$(awk -v total="$TOTAL_STEPS" -v rc="$LOOP_RC" -v timed_out="$TIMED_OUT" \
              -v stage="$STAGE" -v start="$STARTED_NS" -v stop="$STOP_NS" \
              -v wait_mode="$WAIT_MODE" -v case_timeout="$CASE_TIMEOUT" '
BEGIN {
    completed = 0; actions_ns = 0; nonzero = 0; wait_ns = 0; seen_done = 0
    interrupted = 0; max_step = 0; last_start = 0
}
{
    kind = ""; idx = ""; val = 0; el = 0
    n = split($0, f, ",")
    for (i = 1; i <= n; i++) {
        split(f[i], kv, "=")
        if (kv[1] == "kind") kind = kv[2]
        else if (kv[1] == "step") idx = kv[2] + 0
        else if (kv[1] == "exit_code") val = kv[2] + 0
        else if (kv[1] == "elapsed_ns") el = kv[2] + 0
        else if (kv[1] == "at_ns") at = kv[2] + 0
    }
    if (kind == "action_start") { last_start = at; started[idx] = 1 }
    else if (kind == "action_end") {
        completed++; actions_ns += el; delete started[idx]
        if (val != 0) nonzero++
        if (el > max_step) max_step = el
        ends[idx] = 1
    }
    else if (kind == "wait_end") wait_ns += el
    else if (kind == "case_done") seen_done = 1
}
END {
    for (i in started) { interrupted = i; break }
    partial = 0
    if (interrupted && last_start && stop > last_start) partial = stop - last_start
    wall = (start && stop > start) ? stop - start : 0
    if (seen_done && completed == total && rc == 0) status = "completed"
    else if (timed_out && stage == "budget") status = "budget_exhausted"
    else if (timed_out) status = "initialization_timeout"
    else if (rc != 0) status = "action_failed"
    else status = "incomplete"
    # %.0f, never %d: mawk clamps %d at INT32_MAX, which silently corrupts
    # nanosecond timestamps (measured case_wall_ns = 2147483647). Double gives
    # ~256 ns resolution at 1.8e18, far below anything this experiment measures.
    printf "{\"status\":\"%s\",\"completed_steps\":%.0f,\"total_steps\":%.0f," \
           "\"nonzero_actions\":%.0f,\"interrupted_action\":%.0f," \
           "\"case_wall_ns\":%.0f,\"actions_ns\":%.0f,\"partial_action_ns\":%.0f," \
           "\"max_step_ns\":%.0f,\"wait_ns\":%.0f,\"wait\":\"%s\"," \
           "\"case_timeout_s\":%.0f,\"action_exit\":%.0f}",
           status, completed, total, nonzero, interrupted, wall, actions_ns,
           partial, max_step, wait_ns, wait_mode, case_timeout, rc
}' "$OUT/events.jsonl" 2>/dev/null)

if [ -z "$SUMMARY" ]; then
    SUMMARY=$(printf '{"status":"initialization_failed","completed_steps":0,"total_steps":%s,' \
                     '"nonzero_actions":0,"interrupted_action":0,"case_wall_ns":0,' \
                     '"actions_ns":0,"partial_action_ns":0,"max_step_ns":0,"wait_ns":0,' \
                     '"wait":"%s","case_timeout_s":%s,"action_exit":%s}' \
                     "$TOTAL_STEPS" "$WAIT_MODE" "$CASE_TIMEOUT" "$LOOP_RC")
fi

# One exact byte string goes to both the disk and the serial line, so the host can
# (a) recompute the crc over what arrived and (b) diff the two copies.
printf '%s\n' "$SUMMARY" > "$OUT/summary.json"
printf '%s\n' "$TOKEN" > "$OUT/token.txt"
printf '%s\n' "$STAGE" > "$OUT/stage.txt"
sync
CRC=$(printf '%s' "$SUMMARY" | cksum | awk '{print $1}')
printf 'BWX_RESULT:%s:%s:%s\n' "$TOKEN" "$CRC" "$SUMMARY"

STATUS=$(printf '%s' "$SUMMARY" | sed -n 's/.*"status":"\([a-z_]*\)".*/\1/p')
case "$STATUS" in
    completed|budget_exhausted) exit 0 ;;
esac
exit 1
