#!/bin/bash
# bwx action loop -- executes the trajectory step by step.
#
# Generic: everything case-specific comes from /work/default.sh, /work/steps.txt
# and /work/actions/<idx>.sh, which the host injects into the payload disk.
#
# Actions are *sourced* into this shell (not run as child processes) so that cwd,
# exported variables and shell functions defined by an earlier step still apply to
# later ones -- the trajectories were recorded that way. Each action's stdout and
# stderr are redirected into its own log file, which keeps the serial stream down
# to the protocol markers and makes per-action output inspectable afterwards.
#
# events line format (parsed by supervise.sh with awk):
#   kind=<k>,step=<i>[,exit_code=<rc>][,elapsed_ns=<n>][,at_ns=<n>][,nominal_s=<f>]
set -u

TOKEN=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_token=//p' | head -1)
WAIT_MODE=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_wait=//p' | head -1)
ACTION_TIMEOUT=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^bwx_action_timeout=//p' | head -1)
ACTION_TIMEOUT=${ACTION_TIMEOUT:-600}
WAIT_MODE=${WAIT_MODE:-off}

OUT=/work/result
REPLAY=/work
mkdir -p "$OUT/actions"

now_ns() { date +%s%N; }
event() { printf '%s\n' "$*" >> "$OUT/events.jsonl"; }
bwx_error() { printf 'BWX_ERROR:%s:%s:%s\n' "$TOKEN" "$1" "$2"; }

[ -f "$REPLAY/default.sh" ] && . "$REPLAY/default.sh"
BWX_CWD=${BWX_CWD:-/testbed}
[ -d "$BWX_CWD" ] || BWX_CWD=/

cd "$BWX_CWD" || { bwx_error chdir "$BWX_CWD"; exit 81; }
export GIT_OPTIONAL_LOCKS=0

# Trajectories end with a bare `submit` action, which is a shell function rather
# than a binary. Defining it here keeps the final step from exiting 127.
if ! command -v submit >/dev/null 2>&1; then
    submit() {
        if command -v git >/dev/null 2>&1; then
            git diff --binary > "$OUT/submitted.patch" 2>/dev/null
            git status --porcelain -uall > "$OUT/submitted-status.txt" 2>/dev/null
        else
            printf 'submit: git unavailable in this image\n' > "$OUT/submit-unavailable.txt"
        fi
        return 0
    }
fi

# Baseline capture is diagnostic only. `git` may be absent; never fail on it.
if command -v git >/dev/null 2>&1; then
    git rev-parse HEAD > "$OUT/start-commit.txt" 2>/dev/null || true
    git status --porcelain -uall > "$OUT/start-status.txt" 2>/dev/null || true
fi

printf 'BWX_CASE_START:%s:%s\n' "$TOKEN" "$(now_ns)"
echo "$(now_ns)" > "$OUT/.case_start"

# Per-action watchdog: an action that hangs must not consume the whole budget
# silently. It TERM/KILLs this shell's process group, which supervise.sh detects
# as a nonzero exit.
watchdog() {
    ( sleep "$1"; kill -TERM 0 2>/dev/null; sleep 2; kill -KILL 0 2>/dev/null ) &
    WATCHDOG_PID=$!
}
stop_watchdog() {
    [ -n "${WATCHDOG_PID:-}" ] || return 0
    kill -KILL "$WATCHDOG_PID" 2>/dev/null
    wait "$WATCHDOG_PID" 2>/dev/null
    WATCHDOG_PID=
}

while read -r idx delay _rest; do
    [ -n "$idx" ] || continue
    event "kind=step,step=$idx,at_ns=$(now_ns)"

    if [ "$WAIT_MODE" = on ] && [ -n "$delay" ] && [ "$delay" != 0 ]; then
        wstart=$(now_ns)
        event "kind=wait_start,step=$idx,nominal_s=$delay,at_ns=$wstart"
        sleep "$delay" 2>/dev/null || { bwx_error wait "step=$idx"; exit 83; }
        wend=$(now_ns)
        event "kind=wait_end,step=$idx,elapsed_ns=$(( wend - wstart )),at_ns=$wend"
    fi

    script="$REPLAY/actions/$idx.sh"
    if [ ! -f "$script" ]; then
        bwx_error missing_action "$script"
        event "kind=action_end,step=$idx,exit_code=127,elapsed_ns=0,at_ns=$(now_ns)"
        exit 86
    fi

    astart=$(now_ns)
    event "kind=action_start,step=$idx,at_ns=$astart"
    printf 'BWX_STEP:%s:%s:start\n' "$TOKEN" "$idx"

    watchdog "$ACTION_TIMEOUT"
    # Sourced so shell state persists across steps; output isolated per action.
    . "$script" > "$OUT/actions/$idx.log" 2>&1 </dev/null
    rc=$?
    stop_watchdog

    aend=$(now_ns)
    event "kind=action_end,step=$idx,exit_code=$rc,elapsed_ns=$(( aend - astart )),at_ns=$aend"
    printf 'BWX_STEP:%s:%s:%s:%s\n' "$TOKEN" "$idx" "$rc" "$(( aend - astart ))"

    # 126/127 mean the action could not be executed at all (missing interpreter,
    # non-executable file). Continuing would only produce meaningless timings.
    if [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ]; then
        bwx_error unsupported_action "step=$idx rc=$rc"
        exit 84
    fi
done < "$REPLAY/steps.txt"

event "kind=case_done,at_ns=$(now_ns)"
printf 'BWX_CASE_DONE:%s:%s\n' "$TOKEN" "$(now_ns)"
sync
exit 0
