import json
import tempfile
import unittest
from pathlib import Path

from bwx.cases import parse_case_file
from bwx.commands import alloc as alloc_mod
from bwx.commands.alloc import _allocate, _mixes


class ParseCaseFileTest(unittest.TestCase):
    """The one file format every command accepts (txt or json)."""

    def _file(self, name, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        path.write_text(content, encoding='utf-8')
        return path

    def test_txt_with_comments_and_blanks(self):
        path = self._file('cases.txt', '# batch one\nA\n\nB  # trailing\n')
        parsed = parse_case_file(path)
        self.assertEqual(parsed['ids'], ['A', 'B'])
        self.assertIsNone(parsed['mixes'])

    def test_txt_dedupes_preserving_order(self):
        path = self._file('cases.txt', 'B\nA\nB\n')
        self.assertEqual(parse_case_file(path)['ids'], ['B', 'A'])

    def test_json_array(self):
        path = self._file('cases.json', '["a", "b", "a"]')
        self.assertEqual(parse_case_file(path)['ids'], ['a', 'b'])

    def test_json_cases_key(self):
        path = self._file('cases.json', '{"cases": ["a", "b"]}')
        self.assertEqual(parse_case_file(path)['ids'], ['a', 'b'])
        self.assertIsNone(parse_case_file(path)['mixes'])

    def test_json_structured_mixes(self):
        doc = {'nightly': {'heavy': {'role': 'hungry', 'weight': 3}, 'light': None}}
        path = self._file('mix.json', json.dumps(doc))
        parsed = parse_case_file(path)
        self.assertIsNotNone(parsed['mixes'])
        self.assertEqual(parsed['ids'], ['heavy', 'light'])
        items = parsed['mixes']['nightly']
        self.assertEqual(items[0], {'case_id': 'heavy', 'role': 'hungry', 'weight': 3.0})
        self.assertEqual(items[1], {'case_id': 'light', 'role': 'mixed', 'weight': 1.0})

    def test_json_bad_shape_rejected(self):
        for bad in ('{"cases": "not-a-list"}', '[1, 2]', '"just a string"',
                    '{"m": ["list-not-dict"]}'):
            path = self._file('cases.json', bad)
            with self.assertRaises(Exception, msg=bad):
                parse_case_file(path)

    def test_json_suffix_with_text_content_is_an_error(self):
        """A .json file must contain JSON: failing loudly beats silently
        misreading an id list."""
        path = self._file('cases.json', 'A\nB\n')
        with self.assertRaises(Exception):
            parse_case_file(path)

    def test_missing_file(self):
        with self.assertRaises(Exception):
            parse_case_file('/nonexistent/cases.txt')


class _Args:
    def __init__(self, concurrency, cap=54, budget=200):
        self.concurrency = concurrency
        self.cap = cap
        self.budget = budget


def entries(*case_ids):
    return [{'case_id': cid, 'role': 'mixed', 'weight': 1.0} for cid in case_ids]


class ExpandToConcurrencyTest(unittest.TestCase):
    """The user-facing contract: truncate down, replicate up, to exactly N VMs."""

    def test_truncation_takes_first_n(self):
        allocation = _allocate('none', entries('A', 'B', 'C', 'D', 'E'),
                               _Args(concurrency=3), None, [2, 3])
        self.assertEqual([i['case_id'] for i in allocation['items']], ['A', 'B', 'C'])

    def test_replication_round_robin(self):
        allocation = _allocate('none', entries('A', 'B'),
                               _Args(concurrency=5), None, [2, 3])
        self.assertEqual([i['case_id'] for i in allocation['items']],
                         ['A', 'B', 'A', 'B', 'A'])

    def test_exact_fit(self):
        allocation = _allocate('none', entries('A', 'B', 'C'),
                               _Args(concurrency=3), None, [2, 3])
        self.assertEqual([i['case_id'] for i in allocation['items']], ['A', 'B', 'C'])

    def test_slots_are_unique(self):
        allocation = _allocate('none', entries('A', 'B'),
                               _Args(concurrency=4), None, [2, 3])
        self.assertEqual(sorted(i['slot'] for i in allocation['items']), [0, 1, 2, 3])

    def test_replicas_get_disjoint_cpus(self):
        """Replicated VMs of the same case must still land on disjoint CPUs."""
        from bwx.placement import Allocator
        config = _Sizing()
        allocation = _allocate('none', entries('A'),
                               _Args(concurrency=3), None, [2, 3], config)
        node = allocation['items'][0]['node']
        allocator = Allocator([node], config.vcpus, mem_mib=config.mem_mib)
        placements = allocator.take_many(3)
        Allocator.validate(placements)


class _Sizing:
    def __init__(self):
        self.vcpus = 2
        self.mem_mib = 1024


class MixesFromFileTest(unittest.TestCase):
    def _args(self, **kw):
        import argparse
        defaults = dict(mix=None, case=[], case_file=None)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def _file(self, name, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        path.write_text(content, encoding='utf-8')
        return path

    def test_txt_file(self):
        mixes = _mixes(self._args(case_file=self._file('c.txt', 'A\nB\n')), None)
        self.assertEqual(list(mixes), ['default'])
        self.assertEqual([i['case_id'] for i in mixes['default']], ['A', 'B'])

    def test_json_array_file(self):
        mixes = _mixes(self._args(case_file=self._file('c.json', '["A","B"]')), None)
        self.assertEqual([i['case_id'] for i in mixes['default']], ['A', 'B'])

    def test_json_structured_file(self):
        doc = {'m1': {'A': {'role': 'hungry', 'weight': 2}}, 'm2': {'B': None}}
        mixes = _mixes(self._args(case_file=self._file('c.json', json.dumps(doc))), None)
        self.assertEqual(sorted(mixes), ['m1', 'm2'])
        self.assertEqual(mixes['m1'][0]['role'], 'hungry')

    def test_mix_flag_plain_list_becomes_default(self):
        path = self._file('m.json', '["A","B"]')
        mixes = _mixes(self._args(mix=path), None)
        self.assertEqual(list(mixes), ['default'])

    def test_case_flags_plus_file(self):
        path = self._file('c.txt', 'B\n')
        mixes = _mixes(self._args(case=['A'], case_file=path), None)
        self.assertEqual([i['case_id'] for i in mixes['default']], ['A', 'B'])


if __name__ == '__main__':
    unittest.main()
