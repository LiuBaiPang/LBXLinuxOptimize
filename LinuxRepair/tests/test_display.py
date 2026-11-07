import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from root_helper import preview, edid_preview
from modules.display import outputs, edids, valid_edid

CONFIG='''Section "Device"
    Identifier "Tegra GPU"
    Driver "nvidia"
    Option "AllowEmptyInitialConfiguration" "true"
    Option "ConnectedMonitor" "DFP"
EndSection
'''


class SafetyTests(unittest.TestCase):
    def test_pinned_edid_uses_verified_connector_and_refuses_stacking(self):
        auto=preview(CONFIG)
        pinned=edid_preview(auto,'/var/lib/linux-repair-display/edid-test.bin')
        self.assertIn('Option "ConnectedMonitor" "DFP-1"',pinned)
        self.assertIn('Option "CustomEDID" "DFP-1:/var/lib/linux-repair-display/edid-test.bin"',pinned)
        with self.assertRaises(ValueError):edid_preview(pinned,'/tmp/edid')

    def test_edid_rejects_bad_checksum_and_missing_extension(self):
        data=bytearray(128);data[:8]=b'\x00\xff\xff\xff\xff\xff\xff\x00'
        data[-1]=(-sum(data))%256
        self.assertTrue(valid_edid(data))
        raw=bytes(data).hex()
        prop='DP-1 connected\n\tEDID:\n'+''.join('\t\t'+raw[i:i+32]+'\n' for i in range(0,len(raw),32))
        self.assertEqual(edids(prop),{'DP-1':bytes(data)})
        data[15]^=1;self.assertFalse(valid_edid(data))
        data[126]=1;data[-1]=0;data[-1]=(-sum(data))%256
        self.assertFalse(valid_edid(data))

    def test_preserves_everything_except_override(self):
        result=preview(CONFIG)
        self.assertIn('Option "AllowEmptyInitialConfiguration" "true"',result)
        self.assertEqual([s for s in result.splitlines() if not s.startswith('#')],
                         [s for s in CONFIG.splitlines() if 'ConnectedMonitor' not in s])

    def test_refuses_already_changed(self):
        with self.assertRaises(ValueError):preview(preview(CONFIG))

    def test_refuses_complex_or_foreign_config(self):
        for text in [CONFIG+CONFIG,CONFIG.replace('"nvidia"','"dummy"'),
                     CONFIG.replace('EndSection','Option "CustomEDID" "DFP-0:/tmp/edid"\nEndSection')]:
            with self.assertRaises(ValueError):preview(text)

    def test_disconnected_output_modes_are_not_offered(self):
        data='DP-0 connected primary 1600x900+0+0\n   1600x900 59.82*\n   1280x720 60.00\nDP-1 disconnected\n   640x480 60.00\n'
        self.assertEqual(outputs(data),{'DP-0':['1600x900','1280x720']})


if __name__=='__main__':unittest.main()
