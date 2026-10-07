import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / '01_conversion_downsampling'
sys.path.insert(0, str(SCRIPTS))

from scanner_calibration import (  # noqa: E402
    P1000_MPP_MULTIPLIER, calibrate_mpp, is_p1000,
)


class ScannerCalibrationTests(unittest.TestCase):
    def test_p1000_explicit_scanner_applies_calibration(self):
        x, y, factor = calibrate_mpp(0.248771329, 0.249080882, 'P1000')
        self.assertEqual(factor, P1000_MPP_MULTIPLIER)
        self.assertAlmostEqual(x, 0.248771329 * P1000_MPP_MULTIPLIER)
        self.assertAlmostEqual(y, 0.249080882 * P1000_MPP_MULTIPLIER)

    def test_p1000_path_is_recognised(self):
        self.assertTrue(is_p1000('unknown', r'D:\scanners\P1000_40x\slide.mrxs'))

    def test_other_scanners_are_unchanged(self):
        x, y, factor = calibrate_mpp(0.25, 0.25, 'Hamamatsu S210')
        self.assertEqual((x, y, factor), (0.25, 0.25, 1.0))


if __name__ == '__main__':
    unittest.main()
