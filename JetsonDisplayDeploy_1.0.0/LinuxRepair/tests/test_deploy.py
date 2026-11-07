import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from deploy import compatibility
import tempfile
from unittest.mock import patch
import root_helper


class FleetTests(unittest.TestCase):
    def test_new_file_rollback_removes_only_created_target(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'target.conf';other=Path(d)/'other.conf'
            target.write_text('created');other.write_text('keep')
            with patch.object(root_helper,'TARGET',target):root_helper.restore_snapshot({'existed':False})
            self.assertFalse(target.exists());self.assertEqual(other.read_text(),'keep')

    def test_existing_file_rollback_restores_bytes_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'target.conf';backup=Path(d)/'backup.conf'
            target.write_bytes(b'new');backup.write_bytes(b'old')
            with patch.object(root_helper,'TARGET',target),patch.object(root_helper.os,'chown'):
                root_helper.restore_snapshot(dict(existed=True,backup=str(backup),mode=0o644,uid=0,gid=0))
            self.assertEqual(target.read_bytes(),b'old')

    def test_supported_platform(self):
        compatibility('NVIDIA Orin NX Developer Kit','# R35 (release), REVISION: 6.4, GCID: 1','aarch64')

    def test_refuses_different_platform(self):
        for model,release,arch in [
            ('NVIDIA Orin Nano','# R35 (release), REVISION: 6.4,','aarch64'),
            ('NVIDIA Orin NX','# R36 (release), REVISION: 4.4,','aarch64'),
            ('NVIDIA Orin NX','# R35 (release), REVISION: 6.4,','x86_64')]:
            with self.assertRaises(ValueError):compatibility(model,release,arch)


if __name__=='__main__':unittest.main()
