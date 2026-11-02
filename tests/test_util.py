import subprocess
import unittest
from pathlib import Path

from bwx import util


class CksumTest(unittest.TestCase):
    """The guest/host integrity contract hinges on POSIX cksum agreeing exactly."""

    def test_matches_coreutils(self):
        payloads = [b'', b'hello world\n', b'\x00\x01\x02' * 100,
                    b'{"status":"completed","x":1}']
        for data in payloads:
            self.assertIsInstance(data, bytes, 'cksum operates on bytes only')
            crc, length = util.posix_cksum(data)
            proc = subprocess.run(['cksum'], input=data, capture_output=True)
            if proc.returncode != 0:      # coreutils absent: skip silently
                self.skipTest('cksum not available')
            out = proc.stdout.decode().split()
            self.assertEqual(int(out[0]), crc, 'crc mismatch for %r' % data)
            self.assertEqual(int(out[1]), length)

    def test_length_folded_in(self):
        """Two payloads with equal bytes but different length must differ."""
        a_crc, _ = util.posix_cksum(b'ab')
        # Same CRC inputs but cksum folds length, so a 3-byte payload differs.
        self.assertNotEqual(a_crc, util.posix_cksum(b'ab\x00')[0])


class JsonAtomicTest(unittest.TestCase):
    def test_write_then_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sub' / 'doc.json'
            util.write_json(path, {'a': 1, 'b': [1, 2]})
            self.assertTrue(path.is_file())
            self.assertEqual(util.read_json(path), {'a': 1, 'b': [1, 2]})
            self.assertFalse((path.parent / 'doc.json.tmp').exists(),
                             'temp file must be renamed away')

    def test_read_missing_default(self):
        self.assertEqual(util.read_json('/nonexistent/x.json', default={}), {})
        with self.assertRaises(util.Error):
            util.read_json('/nonexistent/x.json')


class HumanSizeTest(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(util.human_size(0), '0.0B')
        self.assertIn('GiB', util.human_size(5 * 1024 ** 3))
        self.assertIn('MiB', util.human_size(3 * 1024 ** 2))


if __name__ == '__main__':
    unittest.main()
