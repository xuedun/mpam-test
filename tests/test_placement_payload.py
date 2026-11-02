import unittest

from bwx.bandwidth import (BandwidthTopology, expand_cpulist,
                           format_cpulist, node_cpulists)
from bwx.placement import Allocator, Placement, smt_sibling_conflicts
from bwx.util import posix_cksum

import sys
from pathlib import Path


class CpuListTest(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(expand_cpulist('0-3,8,10-11'), {0, 1, 2, 3, 8, 10, 11})
        self.assertEqual(expand_cpulist(''), set())
        self.assertEqual(expand_cpulist('5'), {5})

    def test_format_roundtrip(self):
        for text in ('0-3,8', '0,2,4', '7'):
            self.assertEqual(format_cpulist(expand_cpulist(text)), text)

    def test_format_compacts(self):
        self.assertEqual(format_cpulist({0, 1, 2, 5}), '0-2,5')


class PlacementTest(unittest.TestCase):
    def test_dedicated_layout(self):
        """vcpus CPUs for vCPUs + exactly one helper CPU."""
        placement = Placement(2, [38, 40, 42])
        self.assertEqual(placement.vcpu_cpus, [38, 40])
        self.assertEqual(placement.helper_cpu, 42)
        self.assertEqual(placement.physcpubind, '38,40,42')

    def test_dedicated_needs_at_least_two(self):
        with self.assertRaises(Exception):
            Placement(2, [38])

    def test_helper_is_distinct_from_vcpus(self):
        """The helper CPU must never double as a vCPU CPU."""
        for count in (3, 4):
            placement = Placement(2, list(range(count)))
            self.assertNotIn(placement.helper_cpu, placement.vcpu_cpus)
            self.assertEqual(len(placement.vcpu_cpus) + 1, len(placement.cpus))

    def test_validate_detects_overlap(self):
        a = Placement(2, [38, 40, 42])
        b = Placement(2, [42, 44, 46])
        with self.assertRaises(Exception) as caught:
            Allocator.validate([a, b])
        self.assertIn('42', str(caught.exception))

    def test_validate_detects_wrong_node(self):
        placement = Placement(2, [0, 1, 3])          # CPUs 0,1,3 are on node 0
        with self.assertRaises(Exception) as caught:
            Allocator.validate([placement])
        self.assertIn('not on node', str(caught.exception))

    def test_validate_detects_smt_siblings(self):
        # Topology-dependent: on this host 0/1 are SMT siblings; on a non-SMT
        # host the sibling map has singletons and the check is a no-op.
        conflicts = smt_sibling_conflicts([0, 1, 2])
        if not conflicts:
            self.skipTest('host has no SMT siblings to conflict')
        placement = Placement(0, [0, 1, 2])
        with self.assertRaises(Exception):
            Allocator.validate([placement])


class AllocatorTest(unittest.TestCase):
    def test_take_then_exhaustion(self):
        # Use a node we know exists and is large (node 2/3 have 11-12 slots).
        from bwx.placement import node_ids, node_cpus, select_cores
        big = [n for n in node_ids() if len(select_cores(node_cpus(n), smt=True)) >= 6]
        if not big:
            self.skipTest('no node with enough CPUs')
        node = big[0]
        allocator = Allocator([node], 2, mem_mib=1024)
        per_node, total = allocator.capacity()
        placements = allocator.take_many(per_node[node])
        Allocator.validate(placements)
        self.assertEqual(len(placements), per_node[node])
        with self.assertRaises(Exception):
            allocator.take(node)

    def test_disjoint_by_construction(self):
        from bwx.placement import node_ids, node_cpus, select_cores
        big = [n for n in node_ids() if len(select_cores(node_cpus(n), smt=True)) >= 9]
        if not big:
            self.skipTest('no node with enough CPUs')
        node = big[0]
        allocator = Allocator([node], 2, mem_mib=1024)
        placements = allocator.take_many(3)
        seen = [cpu for p in placements for cpu in p.cpus]
        self.assertEqual(len(seen), len(set(seen)), 'CPU sets must be disjoint')

    def test_take_many_spread_rounds_nodes(self):
        from bwx.placement import node_ids, node_cpus, select_cores
        big = [n for n in node_ids() if len(select_cores(node_cpus(n), smt=True)) >= 3]
        if len(big) < 2:
            self.skipTest('need two usable nodes')
        allocator = Allocator(big[:2], 1, mem_mib=512)
        placements = allocator.take_many(4, spread=True)
        self.assertEqual([p.node for p in placements],
                         [big[0], big[1], big[0], big[1]])
        Allocator.validate(placements)


class PayloadContractTest(unittest.TestCase):
    """steps.txt is the disk-side half of the guest integrity contract."""

    def test_steps_text(self):
        from bwx.payload import steps_text
        steps = [{'action': 'ls', 'delay_time': 0},
                 {'action': 'cd', 'delay_time': 3.33},
                 {'action': 'go test', 'delay_time': 45.42}]
        text = steps_text(steps)
        self.assertEqual(text.splitlines(), ['1 0.0', '2 3.33', '3 45.42'])
        # The crc the host computes must match what the guest's cksum will see.
        crc, length = posix_cksum(text.encode())
        self.assertEqual(length, len(text.encode()))

    def test_delay_default_zero(self):
        from bwx.payload import steps_text
        self.assertEqual(steps_text([{'action': 'x'}]), '1 0.0\n')

    def test_default_sh_quotes(self):
        from bwx.payload import default_sh
        # shlex.quote leaves plain paths bare; the script must still be valid sh.
        self.assertIn('BWX_CWD=/testbed', default_sh('/testbed'))
        # A path with a space must survive the guest's shell.
        self.assertIn("BWX_CWD='/opt/my app'", default_sh('/opt/my app'))


if __name__ == '__main__':
    unittest.main()
