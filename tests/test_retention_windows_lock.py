"""Transient sync-client locks must not corrupt or weaken atomic manifests."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from stocking.retention import _replace_json


class WindowsManifestLockTests(unittest.TestCase):
    def test_sharing_lock_retries_then_atomically_replaces(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "manifest.json"
            target.write_text('{"old":true}', encoding="utf-8")
            real_replace = os.replace
            attempts = []

            def temporarily_locked(source, destination):
                attempts.append(1)
                if len(attempts) < 3:
                    exc = PermissionError("simulated sync lock")
                    exc.winerror = 32
                    raise exc
                return real_replace(source, destination)

            with patch("stocking.retention.os.replace", side_effect=temporarily_locked), patch("stocking.retention.time.sleep"):
                _replace_json(target, {"new": True}, root)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(json.loads(target.read_text()), {"new": True})
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_unrelated_permission_error_is_not_retried(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "manifest.json"
            target.write_text('{"old":true}', encoding="utf-8")
            with patch("stocking.retention.os.replace", side_effect=PermissionError("real denial")) as replace:
                with self.assertRaises(PermissionError):
                    _replace_json(target, {"new": True}, root)
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(json.loads(target.read_text()), {"old": True})


if __name__ == "__main__":
    unittest.main()
