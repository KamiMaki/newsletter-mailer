import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gsuite_https as g

class MaskEmail(unittest.TestCase):
    def test_normal_address(self):
        self.assertEqual(g.mask_email("alice@example.com"), "a***@example.com")

    def test_empty_string(self):
        self.assertEqual(g.mask_email(""), "***")

    def test_no_at_sign(self):
        self.assertEqual(g.mask_email("notanemail"), "***")

    def test_empty_local_part(self):
        self.assertEqual(g.mask_email("@example.com"), "***")

if __name__ == "__main__":
    unittest.main()
