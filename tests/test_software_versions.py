"""software_versions / file_sha256 provenance helpers (stage-2.1 version capture)."""
from __future__ import annotations

import hashlib
import platform
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _common


class SoftwareVersionsTests(unittest.TestCase):
    def test_python_version_always_present(self):
        versions = _common.software_versions()
        self.assertEqual(versions["python"], platform.python_version())

    def test_installed_package_gets_a_version_string(self):
        versions = _common.software_versions("pip")
        self.assertNotEqual(versions["pip"], "not-installed")
        self.assertTrue(versions["pip"][0].isdigit())

    def test_missing_package_is_marked_not_installed_without_raising(self):
        versions = _common.software_versions("definitely-not-a-real-package-xyz")
        self.assertEqual(
            versions["definitely-not-a-real-package-xyz"], "not-installed"
        )


class FileSha256Tests(unittest.TestCase):
    def test_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.bin"
            path.write_bytes(b"cellnote" * 1000)
            expected = hashlib.sha256(b"cellnote" * 1000).hexdigest()
            self.assertEqual(_common.file_sha256(str(path)), expected)


if __name__ == "__main__":
    unittest.main()
