"""Exercise actual argparse entrypoints, beyond Namespace-based unit calls."""
from pathlib import Path
import subprocess
import sys
import unittest


class CLIEntrypointTests(unittest.TestCase):
    def test_public_screen_and_adjust_help_can_be_constructed(self):
        root = Path(__file__).resolve().parents[1]
        for command in ([], ["screen"], ["adjust"]):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, str(root / "scripts/stocking.py"), *command, "--help"],
                    cwd=root, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)


if __name__ == "__main__":
    unittest.main()
