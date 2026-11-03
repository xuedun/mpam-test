import json
import tempfile
import unittest
from pathlib import Path

from bwx.cases import expand_cases, parse_case_file
from bwx.placement import (parse_cpu_list, placements_from_pool,
                            placements_from_start_step, read_cpu_pool)


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



class ExpandCasesTest(unittest.TestCase):
    """The concurrency contract: truncate down, replicate up, exactly N."""

    def test_truncation(self):
        self.assertEqual(expand_cases(['A', 'B', 'C', 'D', 'E'], 3), ['A', 'B', 'C'])

    def test_replication_round_robin(self):
        self.assertEqual(expand_cases(['A', 'B'], 5), ['A', 'B', 'A', 'B', 'A'])

    def test_exact(self):
        self.assertEqual(expand_cases(['A', 'B', 'C'], 3), ['A', 'B', 'C'])

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            expand_cases([], 3)

    def test_bad_concurrency_rejected(self):
        with self.assertRaises(Exception):
            expand_cases(['A'], 0)


class CpuListParsingTest(unittest.TestCase):
    def test_commas_and_ranges(self):
        self.assertEqual(parse_cpu_list('0,2,4-6'), [0, 2, 4, 5, 6])

    def test_spaces_and_newlines(self):
        self.assertEqual(parse_cpu_list('0 2\n4'), [0, 2, 4])

    def test_duplicates_rejected(self):
        with self.assertRaises(Exception):
            parse_cpu_list('0,0')

    def test_garbage_rejected(self):
        for bad in ('', 'x', '0-y'):
            with self.assertRaises(Exception):
                parse_cpu_list(bad)


class CpuPoolFileTest(unittest.TestCase):
    def _file(self, name, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        path.write_text(content)
        return path

    def test_text_file(self):
        path = self._file('cpus.txt', '0,2,4\n6-7\n')
        self.assertEqual(read_cpu_pool(path), [0, 2, 4, 6, 7])

    def test_json_array(self):
        path = self._file('cpus.json', '[38, 40, 42]')
        self.assertEqual(read_cpu_pool(path), [38, 40, 42])

    def test_json_object(self):
        path = self._file('cpus.json', '{"cpus": [38, 40, 42]}')
        self.assertEqual(read_cpu_pool(path), [38, 40, 42])

    def test_json_duplicates_rejected(self):
        path = self._file('cpus.json', '[38, 38]')
        with self.assertRaises(Exception):
            read_cpu_pool(path)


class PlacementGenerationTest(unittest.TestCase):
    """start+step and explicit pools must produce valid disjoint placements."""

    def _node(self):
        from bwx.placement import node_ids
        nodes = node_ids()
        if not nodes:
            self.skipTest('no NUMA nodes')
        # Pick a node whose first cpu makes a usable start.
        return nodes[0]

    def test_start_step_sequence(self):
        """cpus are start + i*step, split into per-VM chunks of vcpus+1.

        step=2 with vcpus=1: the pool is start, start+2, start+4, start+6;
        chunks of (vcpus+1)=2 give VM0=[start, start+2], VM1=[start+4, start+6].
        (step=1 only works on hosts whose adjacent cpu numbers are distinct
        cores; on SMT hosts a larger step skips the siblings.)
        """
        from bwx.placement import node_cpus
        node = self._node()
        start = min(node_cpus(node))
        # cpus = start, start+2, start+4, ... -> chunks of 2: [0,2],[4,6]
        placements = placements_from_start_step(start, 2, node, 1, 2)
        self.assertEqual([p.cpus for p in placements],
                         [[start, start + 2], [start + 4, start + 6]])

    def test_start_step_bad_step_rejected(self):
        node = self._node()
        from bwx.placement import node_cpus
        start = min(node_cpus(node))
        with self.assertRaises(Exception):
            placements_from_start_step(start, 0, node, 1, 2)

    def test_pool_too_small_rejected(self):
        node = self._node()
        with self.assertRaises(Exception):
            placements_from_pool([1, 2], node, 2, 2)

    def test_pool_off_node_rejected(self):
        node = self._node()
        from bwx.placement import node_ids, node_cpus, select_cores
        other = [n for n in node_ids() if n != node]
        if not other:
            self.skipTest('single node host')
        cores_here = select_cores(node_cpus(other[0]), smt=True)[:6]
        with self.assertRaises(Exception):
            placements_from_pool(cores_here, node, 2, 2)


if __name__ == '__main__':
    unittest.main()
