"""Compare sampled tiled output with legacy full-image CODA application at 2x."""

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '03_apply_registration'))
from apply_40x_tiled import (canvas_parameters, displacement_components,
                            inverse_affine, render_tile)
from apply_registration_to_20x_image import apply_CODA_registration, load_mat_file


class ArraySlide:
    def __init__(self, pixels):
        self.pixels = pixels
        self.dimensions = (pixels.shape[1], pixels.shape[0])

    def read_region(self, location, level, size):
        x, y = location
        w, h = size
        return Image.fromarray(self.pixels[y:y+h, x:x+w])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-2x', type=Path, required=True)
    parser.add_argument('--warps', type=Path, required=True)
    parser.add_argument('--transform-name', required=True)
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    with Image.open(args.input_2x) as image:
        pixels = np.asarray(image.convert('RGB'))
    expected, _ = apply_CODA_registration(pixels, args.transform_name,
                                          str(args.warps), 1, 0)
    metadata = next(v for p in sorted(args.warps.glob('*.mat'))
                    if 'szz' in (v := load_mat_file(p)) and 'padall' in v)
    shape, pre = canvas_parameters(pixels.shape[:2], metadata, 1)
    if tuple(expected.shape[:2]) != shape:
        raise AssertionError('Canvas size mismatch')
    values = load_mat_file(args.warps / (args.transform_name + '.mat'))
    moving = 'tform_python' in values
    transform = inverse_affine(values, 1) if moving else None
    flip = bool(int(np.asarray(values.get('f', 0)).squeeze())) if moving else False
    displacement = (displacement_components(load_mat_file(
        args.warps / 'D' / (args.transform_name + '.mat'))['D']) if moving else None)
    rng = np.random.default_rng(20261012)
    positions = [(0, 0), (max(0, shape[1]//2-64), max(0, shape[0]//2-64))]
    positions += [(int(rng.integers(0, shape[1]-128)),
                   int(rng.integers(0, shape[0]-128))) for _ in range(args.samples)]
    slide = ArraySlide(pixels)
    differences = []
    for x0, y0 in positions:
        actual = render_tile(slide, x0, y0, 128, shape, pre,
                             transform, flip, displacement, 1)
        truth = expected[y0:y0+128, x0:x0+128]
        differences.append(np.count_nonzero(actual != truth))
    total = len(positions) * 128 * 128 * 3
    mismatch_fraction = sum(differences) / total
    print(f'{args.transform_name}: {len(positions)} real-data blocks; '
          f'{sum(differences)}/{total} mismatched channel values '
          f'({mismatch_fraction:.4%}); max per block={max(differences)}')
    # OpenCV resize and remap use slightly different float interpolation paths.
    # Rare half-pixel rounding ties can differ by one source pixel.
    if mismatch_fraction > .001:
        raise AssertionError('Tiled calculation differs materially from legacy algorithm')


if __name__ == '__main__':
    main()
