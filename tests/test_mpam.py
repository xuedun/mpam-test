import unittest
from pathlib import Path

from bwx import mpam


class SchemataTest(unittest.TestCase):
    def test_parse(self):
        parsed = mpam.parse_schemata('MB:0=0000100;1=0000100\nL3:1=fffffff\n')
        self.assertEqual(parsed['MB'], {'0': '0000100', '1': '0000100'})
        self.assertEqual(parsed['L3'], {'1': 'fffffff'})

    def test_normalize_strips_zero_padding(self):
        """The kernel zero-pads integer resources; string comparison would fail."""
        parsed = mpam.parse_schemata('MB:0=0000054;1=0000054\n')
        self.assertEqual(mpam.normalize(parsed)['MB'], {'0': '54', '1': '54'})

    def test_normalize_keeps_bitmasks(self):
        parsed = mpam.parse_schemata('L3:1=fffffff\n')
        self.assertEqual(mpam.normalize(parsed)['L3'], {'1': 'fffffff'})

    def test_mb_line(self):
        self.assertEqual(mpam.mb_line({0: 26, 1: 26}), 'MB:0=26;1=26')


def fake_platform():
    import types
    platform = types.SimpleNamespace()
    platform.min_bandwidth = 0
    platform.granularity = 9
    platform.num_closids = 32
    platform.domains = ['0', '1', '2', '3']
    platform.root = Path('/tmp/fake-resctrl')
    platform.root_schema = {'MB': {d: '100' for d in platform.domains}}
    platform.existing_groups = []
    return platform


class LevelValidationTest(unittest.TestCase):
    def _platform(self):
        return fake_platform()

    def test_zero_refused(self):
        """Level 0 has hung this class of machine; it is never allowed."""
        platform = self._platform()
        with self.assertRaises(Exception):
            mpam.Platform.check_level(platform, 0)

    def test_below_min_refused(self):
        platform = self._platform()
        platform.min_bandwidth = 9
        with self.assertRaises(Exception):
            mpam.Platform.check_level(platform, 5)

    def test_bool_rejected(self):
        platform = self._platform()
        with self.assertRaises(Exception):
            mpam.Platform.check_level(platform, True)

    def test_float_rejected(self):
        platform = self._platform()
        with self.assertRaises(Exception):
            mpam.Platform.check_level(platform, 54.0)

    def test_out_of_range(self):
        platform = self._platform()
        for bad in (-1, 101, 1000):
            with self.assertRaises(Exception):
                mpam.Platform.check_level(platform, bad)


class GroupLevelsTest(unittest.TestCase):
    def _group(self, levels):
        from bwx.mpam import Group
        return Group(fake_platform(), 'g-test', levels)

    def test_int_applies_to_all_domains(self):
        group = self._group(26)
        self.assertEqual(group.requested, {d: 26 for d in ('0', '1', '2', '3')})

    def test_per_domain_levels(self):
        group = self._group({'0': 9, '1': 54, '2': 54, '3': 100})
        self.assertEqual(group.requested['2'], 54)

    def test_per_domain_missing_domain_rejected(self):
        with self.assertRaises(Exception):
            self._group({'0': 9})

    def test_unknown_domain_rejected(self):
        with self.assertRaises(Exception):
            self._group({'0': 9, '1': 9, '2': 9, '3': 9, '4': 9})

    def test_bad_name_rejected(self):
        with self.assertRaises(Exception):
            self._group('with space')

    def test_join_argv_reexeces_module(self):
        group = self._group(26)
        argv = group.join_argv(['numactl', '--membind=0', 'firecracker'])
        self.assertEqual(argv[:4], [sys_exe(), '-m', 'bwx.mpam', '--join'])
        self.assertIn('numactl', argv)


def sys_exe():
    import sys
    return sys.executable


if __name__ == '__main__':
    unittest.main()
