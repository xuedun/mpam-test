import unittest

from bwx.commands import alloc as alloc_mod


class _Args:
    def __init__(self, concurrency=4, cap=54, budget=200):
        self.concurrency = concurrency
        self.cap = cap
        self.budget = budget


ENTRIES = [{'case_id': 'hungry-case', 'role': 'hungry', 'weight': 1.0},
           {'case_id': 'light-case', 'role': 'light', 'weight': 1.0},
           {'case_id': 'mid-case', 'role': 'medium', 'weight': 1.0}]
NODES = [2, 3]


class AllocationTest(unittest.TestCase):
    """The strategies differ only in (group, level, node) per VM -- verified here."""

    def _allocate(self, strategy, demand=None, args=None, entries=None, n=None):
        args = args or _Args()
        entries = entries or ENTRIES
        if n:
            args.concurrency = n
        return alloc_mod._allocate(strategy, entries, args, demand, NODES)

    def test_none_has_no_group_and_no_cap(self):
        allocation = self._allocate('none')
        for item in allocation['items']:
            self.assertIsNone(item['level'])
            self.assertIsNone(item['group'])

    def test_shared_single_group_name(self):
        allocation = self._allocate('shared')
        names = {item['group'] for item in allocation['items']}
        self.assertEqual(len(names), 1, 'shared strategy = one CLOSID for all VMs')
        self.assertEqual({item['level'] for item in allocation['items']}, {54})

    def test_equal_group_per_vm_same_cap(self):
        allocation = self._allocate('equal')
        names = [item['group'] for item in allocation['items']]
        self.assertEqual(len(names), len(set(names)), 'each VM gets its own group')
        self.assertEqual({item['level'] for item in allocation['items']}, {54})

    def test_demand_weights_the_cap(self):
        demand = {'hungry-case': {'level': 90, 'gbs': 20, 'class': 'hungry'},
                  'light-case': {'level': 9, 'gbs': 2, 'class': 'light'},
                  'mid-case': {'level': 50, 'gbs': 10, 'class': 'medium'}}
        allocation = self._allocate('demand', demand=demand, args=_Args(budget=149))
        levels = {item['case_id']: item['level'] for item in allocation['items']}
        self.assertGreater(levels['hungry-case'], levels['mid-case'])
        self.assertGreater(levels['mid-case'], levels['light-case'])

    def test_demand_stays_within_1_100(self):
        demand = {'a': {'level': 100}, 'b': {'level': 100}, 'c': {'level': 100},
                  'd': {'level': 100}}
        allocation = self._allocate('demand', demand=demand, args=_Args(budget=40000, concurrency=4))
        for item in allocation['items']:
            self.assertTrue(1 <= item['level'] <= 100, item['level'])

    def test_spread_uses_every_node(self):
        allocation = self._allocate('spread', n=4)
        nodes = [item['node'] for item in allocation['items']]
        self.assertEqual(nodes, [2, 3, 2, 3])

    def test_packed_contends_on_one_node(self):
        allocation = self._allocate('shared', n=4)
        nodes = {item['node'] for item in allocation['items']}
        self.assertEqual(len(nodes), 1, 'non-spread strategies pack to force contention')

    def test_mix_is_replicated_to_concurrency(self):
        allocation = self._allocate('none', n=5)
        self.assertEqual(len(allocation['items']), 5)
        # Round-robin over the 3-case mix: order preserved, case 1 repeats.
        self.assertEqual([i['case_id'] for i in allocation['items']][3],
                         ENTRIES[0]['case_id'])

    def test_clamp_level(self):
        for value, expected in ((0.4, 1), (99.6, 100), (-5, 1), (250, 100), (54.4, 54)):
            self.assertEqual(alloc_mod._clamp_level(value), expected)


if __name__ == '__main__':
    unittest.main()
