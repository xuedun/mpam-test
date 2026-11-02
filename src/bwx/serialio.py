"""Serial protocol between the guest replay framework and the host driver.

Why serial
----------
These VMs boot with no NIC and no Firecracker API socket, so the serial console
(guest `ttyS0` -> Firecracker stdout) is the only channel out of a running guest.
The final result summary therefore travels over serial as one CRC-guarded line,
which is what makes a run self-describing without a second disk read.

Console modes (the switchable part)
-----------------------------------
Action stdout/stderr always go to per-action log files on the payload disk, never
to serial, so the serial stream stays bounded to a handful of protocol lines no
matter how chatty a trajectory is. The mode only decides what the *host* keeps:

    off      Firecracker stdout -> /dev/null. No host persistence, no host parse.
             Cheapest: zero host-side serial I/O. Verification falls back to the
             on-disk summary extracted from the payload disk after exit; serial
             integrity cannot be checked, so the result is reported with
             `serial_verification=disabled`.
    markers  (default) stdout -> console.raw, parsed incrementally; only protocol
             lines are persisted to console-markers.log and console.raw is
             deleted. Full serial verification. Best cost/correctness balance.
    full     stdout -> console.log, kept in full (kernel boot log included).
             Debugging only.

In `markers` and `full` the kernel line carries `console=ttyS0 quiet loglevel=0`,
which maps /dev/console to the serial device (needed for PID1's stdout) while
suppressing the bulk of kernel logging. `off` drops `console=ttyS0` entirely.

Correctness (serialio.verify)
-----------------------------
A VM whose init failed panics and, with `reboot=k panic=1`, Firecracker exits 0.
Treating exit 0 as success is the single most dangerous mistake here, so success
requires positive evidence: BOOT + READY + CASE_START + every STEP + a CRC-valid
RESULT + a matching on-disk summary. Markers are matched unanchored because an
action that prints without a trailing newline can glue a marker onto its output.
"""
import json
import re

from .util import Error, posix_cksum

_KERNEL_BASE = 'reboot=k panic=1 pci=off root=/dev/vda rw init=/bwx/init.sh'
_QUIET = 'quiet loglevel=0'

_MARKER = re.compile(
    r'BWX_(BOOT|READY|CASE_START|STEP|CASE_DONE|RESULT|EXIT|ERROR|LO_READY)'
    r':?([0-9a-f]{8,64})?(?::([^\r\n]*))?')

_PANIC_PATTERNS = (
    'Kernel panic - not syncing',
    'Requested init ',
    'No filesystem could mount root',
    'Attempted to kill init',
    'Unable to mount root fs',
    'VFS: Cannot open root device',
)

VALID_MODES = ('off', 'markers', 'full')


def boot_args(console_mode, token, *, case_timeout, boot_timeout, action_timeout,
              wait, extra=''):
    """Build the kernel command line for a run.

    Every per-run parameter is passed here rather than patched into the image, so
    the delivery rootfs is never modified after conversion. The guest reads them
    from /proc/cmdline (see guest/init.sh and guest/supervise.sh).
    """
    parts = []
    if console_mode == 'off':
        # No console= -> the host never sees serial; timing is unpolluted.
        parts.append(_QUIET)
    else:
        # console=ttyS0 is required so PID1's stdout (and /dev/console) reach the
        # serial device; quiet+loglevel=0 keeps kernel logging minimal.
        parts.append('console=ttyS0 %s' % _QUIET)
    parts.append(_KERNEL_BASE)
    parts.append('bwx_token=%s' % token)
    parts.append('bwx_case_timeout=%s' % case_timeout)
    parts.append('bwx_boot_timeout=%s' % boot_timeout)
    parts.append('bwx_action_timeout=%s' % action_timeout)
    parts.append('bwx_wait=%s' % wait)
    if extra:
        for word in extra.split():
            key = word.split('=', 1)[0]
            if key in ('init', 'root', 'console') or key.startswith('bwx_'):
                raise Error('boot-args-extra may not override %r' % word)
        parts.append(extra.strip())
    return ' '.join(parts)


class Marker:
    """One parsed protocol line."""

    __slots__ = ('kind', 'token', 'fields', 'raw')

    def __init__(self, kind, token, fields, raw):
        self.kind, self.token, self.fields, self.raw = kind, token, fields, raw

    def __repr__(self):
        return 'Marker(%s%s)' % (self.kind, (':' + ':'.join(self.fields)) if self.fields else '')


class ConsoleFilter:
    """Incremental byte-stream parser for the guest serial protocol.

    Feed arbitrary chunks (a read may split a line anywhere); completed markers
    come out of :meth:`feed`. Non-marker bytes are counted but never stored, so
    host memory stays bounded no matter how much the guest prints.
    """

    def __init__(self, token, keep_full_lines=False):
        self.token = token
        self.keep_full_lines = keep_full_lines
        self.markers = []
        self.full_lines = [] if keep_full_lines else None
        self.bytes_total = 0
        self.lines_total = 0
        self.marker_lines = 0
        self.foreign_markers = 0
        self.panic_seen = None
        self._pending = b''

    def feed(self, data):
        if not data:
            return []
        self.bytes_total += len(data)
        self._pending += data
        found = []
        while b'\n' in self._pending:
            line, self._pending = self._pending.split(b'\n', 1)
            found.extend(self._feed_line(line))
        return found

    def close(self):
        rest, self._pending = self._pending, b''
        return self._feed_line(rest) if rest.strip() else []

    def _feed_line(self, raw):
        text = raw.decode('utf-8', 'replace').rstrip('\r')
        self.lines_total += 1
        if self.full_lines is not None:
            self.full_lines.append(text)
        if self.panic_seen is None:
            for pattern in _PANIC_PATTERNS:
                if pattern in text:
                    self.panic_seen = text.strip()[:400]
                    break
        out = []
        for match in _MARKER.finditer(text):
            kind = match.group(1)
            token = match.group(2)
            rest = match.group(3) or ''
            # BWX_LO_READY carries no token; accept it structurally.
            if kind == 'LO_READY':
                self.marker_lines += 1
                marker = Marker(kind, token or self.token, [], text)
                self.markers.append(marker)
                out.append(marker)
                continue
            if token != self.token:
                self.foreign_markers += 1
                continue
            self.marker_lines += 1
            marker = Marker(kind, token, rest.split(':') if rest else [], text)
            self.markers.append(marker)
            out.append(marker)
        return out

    def first(self, kind):
        return next((m for m in self.markers if m.kind == kind), None)

    def all(self, kind):
        return [m for m in self.markers if m.kind == kind]

    def result_payload(self):
        """Return (json_text, transmitted_crc) from BWX_RESULT, or (None, None)."""
        marker = self.first('RESULT')
        if marker is None or len(marker.fields) < 2:
            return None, None
        try:
            crc = int(marker.fields[0])
        except ValueError:
            return None, None
        return ':'.join(marker.fields[1:]), crc

    def result_json(self):
        """Parse BWX_RESULT and recompute the CRC over the bytes that arrived."""
        payload, transmitted = self.result_payload()
        if payload is None:
            marker = self.first('RESULT')
            if marker is None:
                return None, 'no BWX_RESULT marker on the serial console'
            return None, 'malformed BWX_RESULT (need crc:json)'
        actual, _ = posix_cksum(payload.encode('utf-8'))
        if actual != transmitted:
            return None, ('BWX_RESULT crc mismatch (serial corruption): '
                          'transmitted=%d recomputed=%d' % (transmitted, actual))
        try:
            return json.loads(payload), None
        except ValueError as exc:
            return None, 'BWX_RESULT payload is not valid JSON: %s' % exc

    def stats(self):
        return {'bytes_total': self.bytes_total, 'lines_total': self.lines_total,
                'marker_lines': self.marker_lines, 'foreign_markers': self.foreign_markers,
                'panic': self.panic_seen}


class Verification:
    """Outcome of the host-side correctness checks for one VM run."""

    def __init__(self):
        self.checks = {}
        self.ok = False
        self.status = 'unverified'
        self.detail = ''
        self.serial_verification = 'enabled'

    def add(self, name, passed, detail=''):
        self.checks[name] = passed if detail == '' else '%s (%s)' % (passed, detail)
        return passed

    def finish(self, ok, status, detail=''):
        self.ok, self.status, self.detail = ok, status, detail
        return self

    def as_dict(self):
        return {'ok': self.ok, 'status': self.status, 'detail': self.detail,
                'serial_verification': self.serial_verification, 'checks': self.checks}


def verify(console, *, expected_steps, expected_steps_crc, disk_summary=None,
           vmm_exit_code=None, strict=True, serial_available=True):
    """Run every available correctness check and classify the run.

    `strict` is the `console_verify` config knob. When serial was not captured
    (console mode `off`) the run is verified against the on-disk summary only and
    flagged `serial_verification=disabled`.
    """
    v = Verification()

    if console.panic_seen:
        v.add('no_kernel_panic', False, console.panic_seen)
        v.finish(False, 'guest_failed', 'kernel panic: %s' % console.panic_seen)
        return v

    # Serial unavailable (mode off, or the guest printed nothing): disk-only path.
    if not serial_available or console.marker_lines == 0:
        v.serial_verification = 'disabled'
        if not strict:
            v.finish(False, 'unverified', 'verification disabled and no serial captured')
            return v
        if disk_summary is None:
            v.finish(False, 'guest_failed',
                     'no serial markers and no on-disk summary (vmm_exit_code=%s)'
                     % vmm_exit_code)
            return v
        return _verify_disk_only(v, disk_summary, expected_steps, vmm_exit_code)

    boot = console.first('BOOT')
    ready = console.first('READY')
    done = console.first('CASE_DONE')
    error = console.first('ERROR')
    steps = console.all('STEP')

    v.add('serial_channel_seen', True,
          '%d bytes / %d markers' % (console.bytes_total, console.marker_lines))
    v.add('boot_marker', boot is not None)
    if boot is None:
        v.finish(False, 'guest_failed',
                 'no BWX_BOOT marker (init did not run; vmm_exit_code=%s)' % vmm_exit_code)
        return v
    if error is not None:
        detail = ':'.join(error.fields)
        v.add('no_guest_error', False, detail)
        v.finish(False, 'guest_error', 'guest reported %s' % detail)
        return v

    # Injected inputs reached the guest intact (guards silent debugfs failures).
    if ready is not None and expected_steps_crc is not None and len(ready.fields) >= 3:
        try:
            guest_crc, guest_bytes = int(ready.fields[1]), int(ready.fields[2])
        except ValueError:
            guest_crc, guest_bytes = -1, -1
        v.add('steps_crc_matches_injection', guest_crc == expected_steps_crc,
              'guest=%d host=%d' % (guest_crc, expected_steps_crc))
        v.add('steps_bytes_nonzero', guest_bytes > 0, 'bytes=%d' % guest_bytes)
        if guest_crc != expected_steps_crc:
            v.finish(False, 'injection_corrupt',
                     'steps.txt crc mismatch: guest read %d, host injected %d '
                     '(debugfs/mkfs write did not land?)' % (guest_crc, expected_steps_crc))
            return v
        try:
            guest_steps = int(ready.fields[0])
            v.add('step_count_matches', guest_steps == expected_steps,
                  'guest=%d host=%d' % (guest_steps, expected_steps))
        except ValueError:
            pass
    else:
        v.add('steps_crc_matches_injection', None, 'BWX_READY unavailable')

    # Step sequence complete, ordered, no gaps/dupes. STEP lines are
    # `<token>:<idx>:<rc>:<elapsed_ns>`; the start marker has fields[1]=='start'.
    ended = []
    for marker in steps:
        if len(marker.fields) >= 2 and marker.fields[1] != 'start':
            try:
                ended.append(int(marker.fields[0]))
            except ValueError:
                continue
    expected = list(range(1, expected_steps + 1))
    ordered_ok = ended == [i for i in expected if i in set(ended)]
    v.add('step_end_count', len(ended) == expected_steps, '%d/%d' % (len(ended), expected_steps))
    v.add('step_sequence_ordered_unique', ordered_ok)
    if not ordered_ok:
        v.finish(False, 'action_failed', 'step sequence unordered or duplicated: %s' % ended[:20])
        return v
    # An incomplete sequence is only fatal when the guest claims completion; a
    # budget_exhausted sample is truncated by design and stays valid.
    if len(ended) != expected_steps:
        result_summary, _ = console.result_json()
        status = (result_summary or {}).get('status')
        if status != 'budget_exhausted':
            missing = [i for i in expected if i not in set(ended)][:20]
            v.finish(False, 'action_failed',
                     'step sequence incomplete: got %d/%d, missing %s'
                     % (len(ended), expected_steps, ','.join(map(str, missing)) or '-'))
            return v

    # The summary survived the serial link intact.
    summary, problem = console.result_json()
    v.add('result_crc_valid', summary is not None, problem or 'crc ok')
    if summary is None:
        v.finish(False, 'result_corrupt', problem)
        return v

    # Cross-check the serial copy against the on-disk copy when available.
    if disk_summary is not None:
        same = _same_summary(summary, disk_summary)
        v.add('serial_matches_disk', same)
        if not same:
            v.finish(False, 'result_mismatch',
                     'serial summary disagrees with on-disk summary.json')
            return v
    else:
        v.add('serial_matches_disk', None, 'no on-disk summary extracted')

    v.add('case_done_marker', done is not None)
    v.add('vmm_exit_zero', vmm_exit_code == 0, 'code=%s' % vmm_exit_code)

    status = summary.get('status', 'unknown')
    if status in ('completed', 'budget_exhausted'):
        if vmm_exit_code not in (0, None):
            v.finish(False, 'vmm_failed', 'guest ok but firecracker exited %s' % vmm_exit_code)
        elif done is None and status == 'completed':
            v.finish(False, 'result_mismatch', 'status=completed without BWX_CASE_DONE')
        else:
            v.finish(True, status, '')
    else:
        v.finish(False, status, summary.get('error', ''))
    return v


def _verify_disk_only(v, disk_summary, expected_steps, vmm_exit_code):
    """Verification when serial was not captured (console mode off)."""
    status = disk_summary.get('status', 'unknown')
    completed = disk_summary.get('completed_steps')
    total = disk_summary.get('total_steps', expected_steps)
    v.add('disk_summary_present', True)
    v.add('step_count', completed == total, '%s/%s' % (completed, total))
    v.add('vmm_exit_zero', vmm_exit_code == 0, 'code=%s' % vmm_exit_code)
    if status in ('completed', 'budget_exhausted') and vmm_exit_code in (0, None) \
            and completed == total:
        v.finish(True, status, 'verified from on-disk summary only')
    else:
        v.finish(False, status, disk_summary.get('error', 'disk summary not conclusive'))
    return v


def _same_summary(serial, disk):
    keys = ('status', 'completed_steps', 'total_steps', 'nonzero_actions',
            'case_wall_ns', 'actions_ns')
    return all(serial.get(k) == disk.get(k) for k in keys if k in disk or k in serial)


def marker_table(console, limit=4096):
    """Bounded per-marker record for result.json (protocol lines only)."""
    return [{'kind': m.kind, 'fields': m.fields} for m in console.markers[:limit]]


def summary_bytes(fields):
    """Canonical serialization shared by the guest and host-side tests."""
    return json.dumps(fields, separators=(',', ':'), sort_keys=True).encode('utf-8')


def crc_of_summary(fields):
    data = summary_bytes(fields)
    crc, length = posix_cksum(data)
    return crc, length, data
