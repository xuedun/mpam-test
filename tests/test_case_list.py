import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from bwx.cases import parse_case_file
from bwx.commands.case_list import _check_trajectories, _scan, _write_list, run


def _case_dir():
    tmp = tempfile.TemporaryDirectory()
    self = None
    return Path(tmp.name), tmp


def _make_case(root, name, *, status='converted', image=True, iid=None,
               steps=47, delay=1.5):
    workspace = root / name
    workspace.mkdir(parents=True)
    image_path = workspace / 'image.ext4'
    if image:
        image_path.write_bytes(b'fake-ext4')
    record = {'instance_id': iid or name, 'status': status,
              'language': 'go', 'bytes': 1024,
              'path': str(image_path) if image else str(workspace / 'gone.ext4'),
              'trajectory': {'steps': steps, 'delay_sum_s': delay}}
    (workspace / 'base-image.json').write_text(json.dumps(record))
    return workspace


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_good_case_is_usable(self):
        _make_case(self.root, 'case-a')
        usable, skipped = _scan(self.root)
        self.assertEqual([r['instance_id'] for r in usable], ['case-a'])
        self.assertEqual(skipped, [])
        self.assertEqual(usable[0]['steps'], 47)
        self.assertEqual(usable[0]['delay_sum_s'], 1.5)

    def test_directory_order_record_id_wins(self):
        # Order follows the directory name; the record's instance_id is the
        # reported id (they only differ when a case dir was renamed).
        _make_case(self.root, 'zzz', iid='aaa')
        _make_case(self.root, 'bbb')
        usable, _ = _scan(self.root)
        self.assertEqual([r['instance_id'] for r in usable], ['bbb', 'aaa'])

    def test_skips_unconverted_status(self):
        _make_case(self.root, 'case-a', status='failed')
        usable, skipped = _scan(self.root)
        self.assertEqual(usable, [])
        self.assertIn('status=', skipped[0]['reason'])

    def test_skips_missing_image(self):
        _make_case(self.root, 'case-a', image=False)
        usable, skipped = _scan(self.root)
        self.assertEqual(usable, [])
        self.assertIn('image missing', skipped[0]['reason'])

    def test_skips_directory_without_record(self):
        (self.root / 'not-a-case').mkdir()
        usable, skipped = _scan(self.root)
        self.assertEqual(usable, [])
        self.assertIn('no base-image.json', skipped[0]['reason'])

    def test_plain_files_are_ignored(self):
        (self.root / 'README.md').write_text('x')
        usable, skipped = _scan(self.root)
        self.assertEqual(usable, [])
        self.assertEqual(skipped, [])


class StrictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset = Path(self.tmp.name) / 'dataset'
        (self.dataset / 'replay-50').mkdir(parents=True)
        self.config = argparse.Namespace(dataset=str(self.dataset))

    def _trajectory(self, iid, steps=None):
        doc = {'trajectory': steps or [{'action': 'ls', 'delay_time': 0}]}
        path = self.dataset / 'replay-50' / ('%s.replay.json' % iid)
        path.write_text(json.dumps(doc))
        return path

    def test_valid_trajectory_passes(self):
        self._trajectory('case-a')
        row = {'instance_id': 'case-a'}
        ok, skipped = _check_trajectories(self.config, [row])
        self.assertEqual(ok, [row])
        self.assertEqual(skipped, [])

    def test_missing_trajectory_is_skipped(self):
        row = {'instance_id': 'case-a'}
        ok, skipped = _check_trajectories(self.config, [row])
        self.assertEqual(ok, [])
        self.assertIn('no trajectory', skipped[0]['reason'])

    def test_malformed_trajectory_is_skipped(self):
        path = self.dataset / 'replay-50' / 'case-a.replay.json'
        path.write_text('{"steps": [{"action": ""}]}')
        ok, skipped = _check_trajectories(self.config,
                                          [{'instance_id': 'case-a'}])
        self.assertEqual(ok, [])
        self.assertIn('trajectory', skipped[0]['reason'])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        _make_case(self.root, 'case-a')
        _make_case(self.root, 'case-b', image=False)

    def _args(self, **kwargs):
        base = dict(dir=self.root, strict=False, out=None, json=False,
                    config=None)
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_stdout_is_a_clean_id_list(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(run(self._args()), 0)
        self.assertEqual(out.getvalue(), 'case-a\n')
        self.assertIn('1 replayable', err.getvalue())
        self.assertIn('image missing', err.getvalue())

    def test_json_output_contains_skips(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(run(self._args(json=True)), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc['count'], 1)
        self.assertEqual(doc['cases'][0]['instance_id'], 'case-a')
        self.assertEqual(doc['skipped'][0]['instance_id'], 'case-b')

    def test_out_file_is_parse_case_file_compatible(self):
        target = self.root / 'list.txt'
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(run(self._args(out=target)), 0)
        ids = parse_case_file(target)['ids']
        self.assertEqual(ids, ['case-a'])
        header = target.read_text().splitlines()[0]
        self.assertTrue(header.startswith('# bwx case-list'))

    def test_missing_dir_is_an_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(Exception):
                run(self._args(dir=self.root / 'nowhere'))


if __name__ == '__main__':
    unittest.main()
