"""Preserve user skills and the running version when a Mac install cannot finish."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

if os.name == "posix":
    from scripts import install_antigravity_mac as installer


@unittest.skipUnless(os.name == "posix", "The Mac installer uses POSIX file locking.")
class MacInstallTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.user = Path(temp.name)
        self.skill = self.user / ".gemini/config/skills/csr-writer/SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.app = self.user / "Library/Application Support/CSR Writer"

    def test_preserves_existing_custom_skill(self):
        self.skill.write_text("My team skill")
        with self.assertRaisesRegex(SystemExit, "已有同名自定义技能"):
            installer.install(self.user)
        self.assertEqual(self.skill.read_text(), "My team skill")
        self.assertFalse((self.app / "current").exists())

    def test_failed_update_leaves_working_install_and_skill_in_place(self):
        old = self.app / "installs/csr-previous"
        old.mkdir(parents=True)
        (self.app / "current").symlink_to(old)
        (old / "installed-skill.md").write_text("Old published entry")
        (old / "package.sha256").write_text("old")
        self.skill.write_bytes((old / "installed-skill.md").read_bytes())
        with patch.object(installer, "verify", side_effect=subprocess.CalledProcessError(1, ["test"])), \
                patch.object(installer.subprocess, "run"), self.assertRaises(subprocess.CalledProcessError):
            installer.install(self.user)
        self.assertEqual(self.skill.read_text(), "Old published entry")
        self.assertEqual((self.app / "current").resolve(), old.resolve())
        self.assertEqual(list(old.parent.iterdir()), [old])
