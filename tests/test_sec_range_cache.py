import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sec_range_cache', ROOT / 'scripts/sec_range_cache.py')
sec_cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sec_cache)


class SecRangeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sec-cache-test-', dir=ROOT / 'outputs')
        self.folder = Path(self.tmp.name).resolve()
        self.addCleanup(self.cleanup_workspace_temp)

    def cleanup_workspace_temp(self):
        # TemporaryDirectory recursively removes its contents; verify the final
        # absolute target immediately before that cleanup on Windows.
        target = Path(self.tmp.name).resolve()
        if ROOT.resolve() not in target.parents:
            raise RuntimeError('Refusing to clean a temporary directory outside the workspace')
        self.tmp.cleanup()

    def test_small_slice_of_four_megabyte_block_reads_only_requested_bytes(self):
        block = self.folder / 'block.bin'
        offset = 3 * 1024 * 1024 + 23
        payload = b'EPS facts'
        with block.open('wb') as output:
            output.truncate(4 * 1024 * 1024)
            output.seek(offset)
            output.write(payload)
        with block.open('rb') as actual:
            tracked = Mock(wraps=actual)
            context = MagicMock()
            context.__enter__.return_value = tracked
            with patch.object(Path, 'open', return_value=context), patch.object(
                Path, 'read_bytes', side_effect=AssertionError('Whole-block reads are forbidden')
            ):
                result = sec_cache._read_cached_slice(block, offset, offset + len(payload))
            self.assertEqual(result, payload)
            tracked.seek.assert_called_once_with(offset)
            tracked.read.assert_called_once_with(len(payload))

    def test_cache_identity_rejects_different_content_but_allows_signature_rotation(self):
        origin = 'https://cas-bridge.xethub.hf.co/objects/' + 'a' * 64
        sec_cache._check_source_identity(self.folder, origin + '?Signature=first', 4096)
        identity_path = self.folder / 'source_identity.json'
        original = identity_path.read_bytes()
        sec_cache._check_source_identity(self.folder, origin + '?Signature=renewed', 4096)
        different = 'https://cas-bridge.xethub.hf.co/objects/' + 'b' * 64
        with self.assertRaisesRegex(ValueError, 'different source object'):
            sec_cache._check_source_identity(self.folder, different, 4096)
        with self.assertRaisesRegex(ValueError, 'different source object'):
            sec_cache._check_source_identity(self.folder, origin, 8192)
        self.assertEqual(identity_path.read_bytes(), original)

    def test_invalid_upstream_response_is_never_published_as_a_cached_block(self):
        source = 'https://cas-bridge.xethub.hf.co/objects/' + 'a' * 64
        cases = (
            ('wrong_range', 206, 'bytes 1-4/100', b'abcd'),
            ('short_body', 206, 'bytes 0-3/100', b'abc'),
            ('wrong_status', 200, 'bytes 0-3/100', b'abcd'),
            ('oversized_body', 206, 'bytes 0-3/100', b'abcde'),
        )
        for name, status, content_range, body in cases:
            with self.subTest(name=name):
                dest = self.folder / (name + '.bin')
                response = MagicMock()
                response.__enter__.return_value = response
                response.status = status
                response.headers = {'Content-Range': content_range}
                response.read.return_value = body
                with patch.object(sec_cache, 'urlopen', return_value=response):
                    with self.assertRaisesRegex(ValueError, 'Invalid upstream byte range response'):
                        sec_cache._download_block(dest, source, 0, 4, 100)
                response.read.assert_called_once_with(5)
                self.assertFalse(dest.exists())
                self.assertEqual(list(self.folder.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
