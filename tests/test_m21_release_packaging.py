from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from csr_writer import __version__
from csr_writer.io_utils import sha256_file
from scripts.build_m21_release import build_release


class M21ReleasePackagingTests(unittest.TestCase):
    def test_release_bundle_is_versioned_and_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_release(root / "first")
            second = build_release(root / "second")
            first_package = root / "first" / first["package"]
            second_package = root / "second" / second["package"]

            self.assertEqual("1.1.0", __version__)
            self.assertEqual("1.0.0", first["version"])
            self.assertEqual("frozen", first["status"])
            self.assertFalse(first["real_clinical_files_included"])
            self.assertEqual(sha256_file(first_package), sha256_file(second_package))
            self.assertEqual(first["package_sha256"], second["package_sha256"])


if __name__ == "__main__":
    unittest.main()
