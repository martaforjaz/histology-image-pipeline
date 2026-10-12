"""Technical integrity check for a completed tiled 40x OME-TIFF."""

import argparse
import csv
from pathlib import Path

import numpy as np
import tifffile
from openslide import OpenSlide

from apply_40x_tiled import canvas_parameters
from apply_registration_to_20x_image import load_mat_file, read_tiff_mpp


def verify(row):
    output = Path(row['destination_40x'])
    if not output.is_file() or output.stat().st_size == 0:
        raise ValueError(f'Missing or empty output: {output}')
    warps = Path(row['warps_dir'])
    metadata = next(v for p in warps.glob('*.mat')
                    if 'szz' in (v := load_mat_file(p)) and 'padall' in v)
    expected_hw, _ = canvas_parameters((int(row['height']), int(row['width'])),
                                       metadata, 5 / float(row['mpp_x']))
    with tifffile.TiffFile(output) as tif:
        if not tif.is_ome or tif.series[0].shape != (*expected_hw, 3):
            raise ValueError(f'Invalid OME shape: {output}')
        if tif.series[0].dtype != np.uint8:
            raise ValueError(f'Invalid OME dtype: {output}')
    mppx, mppy = read_tiff_mpp(output)
    if abs(mppx-float(row['mpp_x'])) > .001 or abs(mppy-float(row['mpp_y'])) > .001:
        raise ValueError(f'OME pixel size mismatch: {output}')
    with OpenSlide(str(output)) as slide:
        if slide.dimensions != (expected_hw[1], expected_hw[0]):
            raise ValueError(f'OpenSlide dimensions mismatch: {output}')
        samples = []
        for fy in (.25, .5, .75):
            for fx in (.25, .5, .75):
                x = int((expected_hw[1]-256) * fx)
                y = int((expected_hw[0]-256) * fy)
                region = np.asarray(slide.read_region((x, y), 0, (256, 256)).convert('RGB'))
                samples.append(float(np.mean(np.any(region < 220, axis=2))))
    if max(samples) < .001:
        raise ValueError(f'Output samples appear blank: {output}')
    return output.stat().st_size, expected_hw, max(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--transform-name', required=True)
    args = parser.parse_args()
    with args.manifest.open(newline='', encoding='utf-8') as handle:
        rows = [r for r in csv.DictReader(handle)
                if r['transform_name'] == args.transform_name]
    if len(rows) != 1:
        parser.error('Expected exactly one manifest row')
    size, hw, tissue_fraction = verify(rows[0])
    print(f"Verified {args.transform_name}: {hw}, {size/2**30:.2f} GiB, "
          f"max sampled tissue fraction {tissue_fraction:.3f}")


if __name__ == '__main__':
    main()
