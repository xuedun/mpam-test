import unittest

from bwx import serialio
from bwx.util import posix_cksum

TOKEN = 'a1b2c3d4e5f60718'


def feed(console, text):
    return console.feed(text.encode())


class BootArgsTest(unittest.TestCase):
    def test_markers_mode_enables_serial(self):
        args = serialio.boot_args('markers', TOKEN, case_timeout=60, boot_timeout=30,
                                  action_timeout=10, wait='off')
        self.assertIn('console=ttyS0', args)
        self.assertIn('quiet loglevel=0', args, 'kernel logging must be suppressed')
        self.assertIn('init=/bwx/init.sh', args)
        self.assertIn('bwx_token=%s' % TOKEN, args)
        self.assertIn('reboot=k', args, 'Firecracker must exit when the guest resets')

    def test_off_mode_has_no_console(self):
        args = serialio.boot_args('off', TOKEN, case_timeout=60, boot_timeout=30,
                                  action_timeout=10, wait='off')
        self.assertNotIn('console=', args, 'off mode must not create serial traffic')
        self.assertIn('quiet', args)

    def test_extra_cannot_override_contract(self):
        for bad in ('console=ttyAMA0', 'init=/bin/sh', 'root=/dev/vdb',
                    'bwx_token=deadbeef'):
            with self.assertRaises(Exception, msg=bad):
                serialio.boot_args('full', TOKEN, case_timeout=1, boot_timeout=1,
                                   action_timeout=1, wait='off', extra=bad)

    def test_extra_is_appended(self):
        args = serialio.boot_args('full', TOKEN, case_timeout=1, boot_timeout=1,
                                  action_timeout=1, wait='off', extra='nokaslr')
        self.assertTrue(args.endswith('nokaslr'))


class MarkerParsingTest(unittest.TestCase):
    def setUp(self):
        self.console = serialio.ConsoleFilter(TOKEN)

    def test_split_across_chunks(self):
        text = 'BWX_CASE_START:%s:1793631152630106120\nBWX_STEP:%s:1:0:4185020\n' % (TOKEN, TOKEN)
        mid = len(text) // 2
        first = feed(self.console, text[:mid])
        second = feed(self.console, text[mid:])
        # A marker must surface exactly once, however the line was split.
        kinds = [m.kind for m in first + second]
        self.assertEqual(kinds, ['CASE_START', 'STEP'])
        self.assertEqual([m.kind for m in self.console.markers], kinds)

    def test_foreign_token_rejected(self):
        feed(self.console, 'BWX_BOOT:ffffffffffffffff:\n')
        self.assertEqual(self.console.markers, [])
        self.assertEqual(self.console.foreign_markers, 1)

    def test_marker_glued_to_output(self):
        """An action printing without a trailing newline must not lose the marker."""
        feed(self.console, 'some action output without newlineBWX_STEP:%s:7:0:123\n' % TOKEN)
        self.assertEqual([m.kind for m in self.console.markers], ['STEP'])
        self.assertEqual(self.console.markers[0].fields, ['7', '0', '123'])

    def test_panic_detected(self):
        feed(self.console, 'Kernel panic - not syncing: Requested init /bwx/init.sh failed\n')
        self.assertIn('Kernel panic', self.console.panic_seen)

    def test_close_flushes_partial_line(self):
        feed(self.console, 'BWX_EXIT:%s:0' % TOKEN)
        self.assertEqual(self.console.markers, [])
        self.assertEqual([m.kind for m in self.console.close()], ['EXIT'])


class ResultCrcTest(unittest.TestCase):
    def setUp(self):
        self.console = serialio.ConsoleFilter(TOKEN)

    def _result_line(self, payload):
        crc, _ = posix_cksum(payload.encode())
        return 'BWX_RESULT:%s:%d:%s\n' % (TOKEN, crc, payload)

    def test_valid_crc_parses(self):
        payload = '{"status":"completed","completed_steps":3,"total_steps":3}'
        feed(self.console, self._result_line(payload))
        summary, problem = self.console.result_json()
        self.assertIsNone(problem)
        self.assertEqual(summary['status'], 'completed')

    def test_truncated_payload_detected(self):
        payload = '{"status":"completed","completed_steps":3,"total_steps":3}'
        line = self._result_line(payload)
        feed(self.console, line[:len(line) - 12] + '\n')   # lose the tail
        summary, problem = self.console.result_json()
        self.assertIsNone(summary)
        self.assertIn('crc mismatch', problem)

    def test_json_with_colons_survives(self):
        """Fields are split on ':' so JSON colons must be re-joined."""
        payload = '{"error":"a: b: c","status":"action_failed"}'
        feed(self.console, self._result_line(payload))
        summary, problem = self.console.result_json()
        self.assertIsNone(problem)
        self.assertEqual(summary['error'], 'a: b: c')

    def test_missing_result_reported(self):
        summary, problem = self.console.result_json()
        self.assertIsNone(summary)
        self.assertIn('no BWX_RESULT', problem)


def good_console(steps=3):
    console = serialio.ConsoleFilter(TOKEN)
    lines = ['BWX_BOOT:%s' % TOKEN,
             'BWX_READY:%s:%d:12345:909' % (TOKEN, steps),
             'BWX_CASE_START:%s:1000' % TOKEN]
    for index in range(1, steps + 1):
        lines.append('BWX_STEP:%s:%d:start' % (TOKEN, index))
        lines.append('BWX_STEP:%s:%d:0:1000' % (TOKEN, index))
    lines.append('BWX_CASE_DONE:%s:2000' % TOKEN)
    summary = '{"status":"completed","completed_steps":%d,"total_steps":%d,' \
              '"nonzero_actions":0,"case_wall_ns":1000,"actions_ns":900}' % (steps, steps)
    crc, _ = posix_cksum(summary.encode())
    lines.append('BWX_RESULT:%s:%d:%s' % (TOKEN, crc, summary))
    lines.append('BWX_EXIT:%s:0' % TOKEN)
    console.feed(('\n'.join(lines) + '\n').encode())
    return console, summary


class VerifyTest(unittest.TestCase):
    """The dangerous failure mode: Firecracker exits 0 after a panic'd init."""

    def test_happy_path(self):
        console, summary = good_console()
        import json
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=12345,
                            disk_summary=json.loads(summary), vmm_exit_code=0)
        self.assertTrue(v.ok, v.detail)
        self.assertEqual(v.status, 'completed')
        self.assertTrue(v.checks['serial_matches_disk'])

    def test_panic_with_zero_exit_is_a_failure(self):
        console = serialio.ConsoleFilter(TOKEN)
        console.feed(b'Kernel panic - not syncing: Attempted to kill init!\n')
        v = serialio.verify(console, expected_steps=1, expected_steps_crc=1,
                            disk_summary=None, vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'guest_failed')

    def test_no_boot_marker_with_zero_exit_is_a_failure(self):
        console = serialio.ConsoleFilter(TOKEN)
        v = serialio.verify(console, expected_steps=1, expected_steps_crc=1,
                            disk_summary=None, vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'guest_failed')

    def test_injection_crc_mismatch(self):
        console, _ = good_console()
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=99999,
                            disk_summary=None, vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'injection_corrupt')

    def test_missing_step_detected(self):
        console = serialio.ConsoleFilter(TOKEN)
        console.feed(('BWX_BOOT:%s\nBWX_READY:%s:2:777:10\nBWX_CASE_START:%s:1\n'
                      'BWX_STEP:%s:1:0:5\n' % (TOKEN, TOKEN, TOKEN, TOKEN)).encode())
        v = serialio.verify(console, expected_steps=2, expected_steps_crc=777,
                            disk_summary=None, vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'action_failed')
        self.assertIn('missing 2', v.detail)

    def test_serial_disk_disagreement(self):
        console, _ = good_console()
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=12345,
                            disk_summary={'status': 'completed', 'completed_steps': 99,
                                          'total_steps': 3},
                            vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'result_mismatch')

    def test_nonzero_vmm_exit_fails_a_good_guest(self):
        console, summary = good_console()
        import json
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=12345,
                            disk_summary=json.loads(summary), vmm_exit_code=1)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'vmm_failed')

    def test_budget_exhausted_is_a_valid_sample(self):
        console = serialio.ConsoleFilter(TOKEN)
        summary = ('{"status":"budget_exhausted","completed_steps":2,"total_steps":5,'
                   '"nonzero_actions":0,"case_wall_ns":60000000000,"actions_ns":500}')
        crc, _ = posix_cksum(summary.encode())
        console.feed(('BWX_BOOT:%s\nBWX_READY:%s:5:1:1\nBWX_CASE_START:%s:1\n'
                      'BWX_STEP:%s:1:0:5\nBWX_STEP:%s:2:0:5\n'
                      'BWX_RESULT:%s:%d:%s\nBWX_EXIT:%s:0\n'
                      % (TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, TOKEN, crc, summary,
                         TOKEN)).encode())
        import json
        v = serialio.verify(console, expected_steps=5, expected_steps_crc=1,
                            disk_summary=json.loads(summary), vmm_exit_code=0)
        # budget_exhausted is a truncated-by-design sample: the step sequence is
        # legitimately incomplete and must not be classified as action_failed.
        self.assertTrue(v.ok, v.detail)
        self.assertEqual(v.status, 'budget_exhausted')

    def test_serial_unavailable_routes_to_disk_only(self):
        """console=off: no markers at all, but the on-disk summary can still verify."""
        console = serialio.ConsoleFilter(TOKEN)
        disk = {'status': 'completed', 'completed_steps': 3, 'total_steps': 3,
                'nonzero_actions': 0, 'case_wall_ns': 10, 'actions_ns': 9}
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=1,
                            disk_summary=disk, vmm_exit_code=0, serial_available=False)
        self.assertTrue(v.ok, v.detail)
        self.assertEqual(v.serial_verification, 'disabled')

    def test_serial_unavailable_without_disk_is_a_failure(self):
        console = serialio.ConsoleFilter(TOKEN)
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=1,
                            disk_summary=None, vmm_exit_code=0, serial_available=False)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'guest_failed')

    def test_strict_false_marks_unverified(self):
        # console_verify=False must flag the run as unverified rather than silently
        # trusting the serial stream.
        console, summary = good_console()
        import json
        v = serialio.verify(console, expected_steps=3, expected_steps_crc=12345,
                            disk_summary=json.loads(summary), vmm_exit_code=0,
                            strict=False, serial_available=False)
        self.assertEqual(v.status, 'unverified')
        self.assertFalse(v.ok, 'unverified must not be reported as a success')

    def test_guest_error_marker(self):
        console = serialio.ConsoleFilter(TOKEN)
        console.feed(('BWX_BOOT:%s\nBWX_ERROR:%s:unsupported_action:step=4 rc=127\n'
                      % (TOKEN, TOKEN)).encode())
        v = serialio.verify(console, expected_steps=5, expected_steps_crc=1,
                            disk_summary=None, vmm_exit_code=0)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, 'guest_error')


class ConsoleStatsTest(unittest.TestCase):
    def test_non_marker_bytes_are_counted_not_stored(self):
        console = serialio.ConsoleFilter(TOKEN)
        console.feed(b'x' * 100000 + b'\n')
        self.assertGreaterEqual(console.bytes_total, 100000)
        self.assertEqual(console.lines_total, 1)
        self.assertIsNone(console.full_lines, 'markers mode must not retain raw lines')
        stats = console.stats()
        self.assertEqual(stats['bytes_total'], console.bytes_total)


if __name__ == '__main__':
    unittest.main()
