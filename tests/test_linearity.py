import argparse
import json
import tempfile
import unittest
from pathlib import Path

from bwx.commands.linearity import (
    _aggregate, _chunks, _collect_samples, _load_or_create_run,
    _parse_levels, _replay_cmd, _seed_for, _shuffled, _write_seed,
    _write_wide)


def _fake_record(case, slot, wall, status='completed', verified=True):
    record = {'kind': 'real', 'slot': slot, 'case': case, 'status': status,
              'case_wall_s': wall, 'label': '%s-slot%d' % (case, slot)}
    if verified is not None:
        record['verification'] = {'ok': verified}
    return record


class ParseLevelsTest(unittest.TestCase):
    def test_default(self):
        self.assertEqual(_parse_levels(None, default='1,2,4'),
                         [1, 2, 4])

    def test_sorted_and_deduped(self):
        self.assertEqual(_parse_levels('8,2,4,2'), [2, 4, 8])

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('')

    def test_non_positive_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('2,0')

    def test_garbage_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('a,b')

    # -- inclusive slices (bash {1..6} style) -----------------------------
    def test_slice_two_part(self):
        self.assertEqual(_parse_levels('1:6'), [1, 2, 3, 4, 5, 6])

    def test_slice_three_part(self):
        self.assertEqual(_parse_levels('1:6:2'), [1, 3, 5])

    def test_slice_stop_on_the_grid(self):
        self.assertEqual(_parse_levels('2:12:5'), [2, 7, 12])

    def test_slice_single_value(self):
        self.assertEqual(_parse_levels('3:3'), [3])

    def test_slice_whitespaces_tolerated(self):
        self.assertEqual(_parse_levels(' 1 : 6 , 8 '), [1, 2, 3, 4, 5, 6, 8])

    def test_mixed_integers_and_slices(self):
        self.assertEqual(_parse_levels('1,2,4:8,16'),
                         [1, 2, 4, 5, 6, 7, 8, 16])

    def test_union_across_segments(self):
        self.assertEqual(_parse_levels('1:4,2,3:5'), [1, 2, 3, 4, 5])

    def test_big_stride_to_48(self):
        self.assertEqual(_parse_levels('24:48:8'), [24, 32, 40, 48])

    def test_slice_empty_range_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('5:3')

    def test_slice_zero_step_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('1:6:0')

    def test_slice_negative_step_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('6:1:-2')

    def test_slice_zero_start_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('0:6')

    def test_slice_four_parts_rejected(self):
        with self.assertRaises(Exception):
            _parse_levels('1:6:2:3')

    def test_slice_hollow_part_rejected(self):
        for bad in ('1::2', '1:6:', ':6'):
            with self.assertRaises(Exception, msg=bad):
                _parse_levels(bad)

    def test_slice_non_integer_rejected(self):
        for bad in ('x:y', '1.5:6'):
            with self.assertRaises(Exception, msg=bad):
                _parse_levels(bad)


class ShuffleTest(unittest.TestCase):
    def test_same_seed_same_order(self):
        base = ['c%d' % i for i in range(20)]
        self.assertEqual(_shuffled(base, 42), _shuffled(base, 42))
        self.assertEqual(_shuffled(base, 42), _shuffled(base, 42))
        self.assertEqual(sorted(_shuffled(base, 42)), sorted(base))

    def test_seed_is_stable_across_calls_and_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'run.json').write_text(json.dumps({'run_id': 'r1'}))
            first = _seed_for(out, 4)
            self.assertEqual(_seed_for(out, 4), first)
            self.assertNotEqual(_seed_for(out, 8), first)

    def test_recorded_seed_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'n8'
            out.mkdir(parents=True)
            (out / 'seed.txt').write_text('0xdead\n')
            self.assertEqual(_seed_for(out.parent, 8), 0xdead)


class ShuffleSwitchTest(unittest.TestCase):
    def _parser(self):
        from bwx.cli import build_parser
        return build_parser()

    def test_shuffle_is_on_by_default(self):
        ns = self._parser().parse_args(['linearity', '--cases', 'a'])
        self.assertTrue(ns.shuffle)

    def test_no_shuffle_flag(self):
        ns = self._parser().parse_args(
            ['linearity', '--cases', 'a', '--no-shuffle'])
        self.assertFalse(ns.shuffle)

    def test_seed_file_records_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            level_dir = Path(tmp) / 'n4'
            _write_seed(level_dir, 42, ['a', 'b'], [['a', 'b']],
                        shuffle=False)
            self.assertEqual((level_dir / 'seed.txt').read_text(), 'off\n')
            self.assertEqual((level_dir / 'plan.txt').read_text(),
                             'g00: a,b\n')

    def test_run_manifest_records_shuffle(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            args = argparse.Namespace(cpu_start=None, cpu_step=None,
                                      shuffle=False)
            doc = _load_or_create_run(out, ['a'], [1, 2], args)
            self.assertFalse(doc['shuffle'])
            self.assertEqual(doc['cases'], ['a'])
            # resume keeps the recorded setting even if the flag flips
            other = argparse.Namespace(cpu_start=None, cpu_step=None,
                                       shuffle=True)
            reloaded = _load_or_create_run(out, ['a'], [1, 2], other)
            self.assertFalse(reloaded['shuffle'])


class ChunkTest(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(_chunks(['a', 'b', 'c', 'd'], 2),
                         [['a', 'b'], ['c', 'd']])

    def test_tail_group_is_short(self):
        self.assertEqual(_chunks(['a', 'b', 'c'], 2),
                         [['a', 'b'], ['c']])

    def test_level_above_case_count_is_one_group(self):
        self.assertEqual(_chunks(['a', 'b'], 8), [['a', 'b']])


class ReplayCmdTest(unittest.TestCase):
    def _args(self, **kwargs):
        base = dict(case=[], cases=None, case_file=None, levels='1,2',
                    out=None, group_timeout=99, summarize_only=False,
                    shuffle=True, cpu_start=None, cpu_step=None, vcpus=None,
                    mem_mib=None, numa_node=None, case_timeout=None,
                    boot_timeout=None, action_timeout=None,
                    bandwidth_interval=None, console=None,
                    console_verify=True, no_tools_disk=False,
                    discard_work=False, config=None)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_fill_and_group_are_always_on(self):
        cmd = _replay_cmd(self._args(), ['a', 'b'], 4, Path('/tmp/g'))
        self.assertIn('--fill', cmd)
        self.assertIn('--concurrency', cmd)
        self.assertEqual(cmd[cmd.index('--concurrency') + 1], '4')
        self.assertEqual(cmd[cmd.index('--cases') + 1], 'a,b')

    def test_console_is_forwarded(self):
        cmd = _replay_cmd(self._args(console='full'), ['a'], 1, Path('/tmp/g'))
        self.assertEqual(cmd[cmd.index('--console') + 1], 'full')
        cmd = _replay_cmd(self._args(console='off'), ['a'], 1, Path('/tmp/g'))
        self.assertEqual(cmd[cmd.index('--console') + 1], 'off')

    def test_console_verify_optout_is_forwarded(self):
        cmd = _replay_cmd(self._args(console_verify=False), ['a'], 1,
                          Path('/tmp/g'))
        self.assertIn('--no-console-verify', cmd)

    def test_cpu_and_tuning_forwarded(self):
        args = self._args(cpu_start=38, cpu_step=2, vcpus=2, mem_mib=2048,
                          case_timeout=60, no_tools_disk=True,
                          discard_work=True)
        cmd = _replay_cmd(args, ['a'], 1, Path('/tmp/g'))
        for flag, value in (('--cpu-start', '38'), ('--cpu-step', '2'),
                            ('--vcpus', '2'), ('--mem-mib', '2048'),
                            ('--case-timeout', '60')):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn('--no-tools-disk', cmd)
        self.assertIn('--discard-work', cmd)

    def test_defaults_forward_nothing(self):
        cmd = _replay_cmd(self._args(), ['a'], 1, Path('/tmp/g'))
        for flag in ('--cpu-start', '--vcpus', '--mem-mib', '--case-timeout',
                     '--no-tools-disk', '--discard-work', '--config',
                     '--console', '--no-console-verify'):
            self.assertNotIn(flag, cmd)


class CollectAndSummarizeTest(unittest.TestCase):
    def _sweep_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def _group(self, out, level, group, records):
        gdir = out / ('n%d' % level) / group
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / 'result.json').write_text(
            json.dumps({'real': records, 'filler': []}))
        return gdir

    def test_collect_reads_real_records_only(self):
        out = self._sweep_dir()
        g1 = self._group(out, 1, 'g00', [_fake_record('a', 0, 1.0),
                                         _fake_record('b', 1, 2.0)])
        g2 = self._group(out, 2, 'g00', [_fake_record('a', 0, 1.5),
                                         _fake_record('a', 1, 1.7),
                                         _fake_record('b', 1, 2.5)])
        (g2 / 'result.json').write_text(json.dumps(
            {'real': [_fake_record('a', 0, 1.5), _fake_record('a', 1, 1.7)],
             'filler': [_fake_record('b', 1, 99.0, status='cancelled',
                                     verified=None)]}))
        samples = _collect_samples(out, [1, 2])
        self.assertEqual(len(samples), 4)
        self.assertTrue(all(s['case'] in ('a', 'b') for s in samples))

    def test_missing_result_json_is_skipped(self):
        out = self._sweep_dir()
        self._group(out, 1, 'g00', [_fake_record('a', 0, 1.0)])
        (out / 'n1' / 'g01').mkdir(parents=True)          # failed group, no data
        (out / 'n2').mkdir()                               # level not started
        samples = _collect_samples(out, [1, 2])
        self.assertEqual(len(samples), 1)

    def test_aggregate_drops_invalid_samples(self):
        out = self._sweep_dir()
        self._group(out, 1, 'g00', [_fake_record('a', 0, 1.0)])
        self._group(out, 2, 'g00', [
            _fake_record('a', 0, 3.0), _fake_record('a', 1, 4.0),
            _fake_record('a', 2, 5.0, verified=False),          # bad checksum
            _fake_record('a', 3, 6.0, status='driver_error'),   # never ran
        ])
        samples = _collect_samples(out, [1, 2])
        agg = _aggregate(samples, [1, 2])
        self.assertEqual(agg[('a', 1)], (1.0, 1, 0))
        self.assertEqual(agg[('a', 2)], (3.5, 2, 2))

    def _read_csv(self, path):
        # write_csv emits utf-8-sig (Excel); read it back the same way.
        return path.read_text(encoding='utf-8-sig').splitlines()

    def test_wide_csv_and_ratio(self):
        out = self._sweep_dir()
        self._group(out, 1, 'g00', [_fake_record('a', 0, 1.0),
                                    _fake_record('b', 0, 4.0)])
        self._group(out, 2, 'g00', [_fake_record('a', 0, 1.5),
                                    _fake_record('a', 1, 2.5),
                                    _fake_record('b', 0, 10.0)])
        samples = _collect_samples(out, [1, 2])
        agg = _aggregate(samples, [1, 2])
        _write_wide(out / 'seconds.csv', ['a', 'b'], [1, 2], agg, seconds=True)
        _write_wide(out / 'ratio.csv', ['a', 'b'], [1, 2], agg, seconds=False)
        lines = self._read_csv(out / 'seconds.csv')
        self.assertEqual(lines[0], 'case,n1,n2')
        self.assertEqual(lines[1], 'a,1.000,2.000')
        self.assertEqual(lines[2], 'b,4.000,10.000')
        lines = self._read_csv(out / 'ratio.csv')
        self.assertEqual(lines[1], 'a,1.000,2.000')
        self.assertEqual(lines[2], 'b,1.000,2.500')

    def test_missing_cell_stays_empty(self):
        out = self._sweep_dir()
        self._group(out, 2, 'g00', [_fake_record('a', 0, 1.5)])
        agg = _aggregate(_collect_samples(out, [1, 2]), [1, 2])
        _write_wide(out / 'seconds.csv', ['a'], [1, 2], agg, seconds=True)
        self.assertEqual(self._read_csv(out / 'seconds.csv')[1], 'a,,1.500')
        # no n1 data: the lowest level *with* data becomes the baseline
        _write_wide(out / 'ratio.csv', ['a'], [1, 2], agg, seconds=False)
        self.assertEqual(self._read_csv(out / 'ratio.csv')[1], 'a,,1.000')


if __name__ == '__main__':
    unittest.main()
