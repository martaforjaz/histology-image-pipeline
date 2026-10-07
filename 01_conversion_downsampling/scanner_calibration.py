"""Scanner-specific physical pixel-size calibrations.

Calibrations change the interpretation of source MPP before any output size is
calculated. They do not modify pixel colours or the requested output MPP.
"""
from pathlib import Path


# Estimated on 2026-10-07 from 31 robust feature/RANSAC registrations of five
# physical slides scanned on P1000 and seven other scanners. The median scale
# required to map P1000 tissue to the other scanners was 1.0133191074458732.
P1000_MPP_MULTIPLIER = 1.0133191074458732


def _normalise(value):
    return ''.join(character for character in str(value).casefold()
                   if character.isalnum())


def is_p1000(scanner='unknown', source=''):
    """Return True for an explicitly named P1000 batch or P1000 source path."""
    if 'p1000' in _normalise(scanner):
        return True
    return any('p1000' in _normalise(part) for part in Path(source).parts)


def calibrate_mpp(mppx, mppy, scanner='unknown', source=''):
    """Return calibrated source MPP and the applied multiplier."""
    mppx, mppy = float(mppx), float(mppy)
    multiplier = P1000_MPP_MULTIPLIER if is_p1000(scanner, source) else 1.0
    return mppx * multiplier, mppy * multiplier, multiplier
