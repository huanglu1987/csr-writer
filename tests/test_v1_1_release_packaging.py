from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from csr_writer import __version__
from csr_writer.io_utils import sha256_file
from scripts.build_v1_1_release import build_release


class VisualQCReleasePackagingTests(unittest.TestCase):
    def test_v1_1_bundle_is_optional_and_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_release(root / "first")
            second = build_release(root / "second")
            first_package = root / "first" / first["package"]
            second_package = root / "second" / second["package"]

            self.assertEqual("1.1.0", __version__)
            self.assertEqual("1.1.0", first["version"])
            self.assertFalse(first["external_visual_dependencies_bundled"])
            self.assertFalse(first["real_clinical_files_included"])
            self.assertEqual(sha256_file(first_package), sha256_file(second_package))
            self.assertEqual(first["package_sha256"], second["package_sha256"])
            dependencies = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
            self.assertNotIn('"PyMuPDF', dependencies)
            self.assertNotIn('"Pillow', dependencies)
            self.assertNotIn('"LibreOffice', dependencies)


if __name__ == "__main__":
    unittest.main()
