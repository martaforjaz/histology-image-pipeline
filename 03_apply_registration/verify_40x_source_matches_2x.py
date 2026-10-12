"""Compare each 40x OME pyramid preview with its same-scanner 2x input."""

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np
import tifffile
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def compare(row):
    source = Path(row['source_40x'])
    low = Path(row['warps_dir']).parents[2] / (row['transform_name'] + '.tif')
    if not low.is_file():
        raise FileNotFoundError(low)
    with tifffile.TiffFile(source) as tif:
        levels = tif.series[0].levels
        if len(levels) < 2:
            raise ValueError(f'40x pyramid missing: {source}')
        preview = levels[-1].asarray()
        full_hw = levels[0].shape[:2]
    if preview.ndim != 3 or preview.shape[2] != 3:
        raise ValueError(f'Expected RGB pyramid preview: {source}')
    with Image.open(low) as image:
        low_hw = (image.height, image.width)
        reduced = np.asarray(image.convert('RGB').resize(
            (preview.shape[1], preview.shape[0]), Image.Resampling.LANCZOS))
    expected_ratio = 5 / float(row['mpp_x'])
    actual_ratio_y = full_hw[0] / low_hw[0]
    actual_ratio_x = full_hw[1] / low_hw[1]
    a = cv2.cvtColor(reduced, cv2.COLOR_RGB2GRAY).astype(np.float32)
    b = cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY).astype(np.float32)
    foreground = (a < 230) | (b < 230)
    if foreground.sum() < 1000:
        raise ValueError(f'Insufficient foreground pixels: {source}')
    return {
        'slide_id': row['slide_id'], 'scanner': row['scanner'],
        'low_2x': str(low), 'source_40x': str(source),
        'low_height': low_hw[0], 'low_width': low_hw[1],
        'high_height': full_hw[0], 'high_width': full_hw[1],
        'expected_scale': round(expected_ratio, 6),
        'actual_scale_y': round(actual_ratio_y, 6),
        'actual_scale_x': round(actual_ratio_x, 6),
        'foreground_correlation': round(float(np.corrcoef(a[foreground], b[foreground])[0, 1]), 6),
        'foreground_mae': round(float(np.mean(np.abs(a[foreground] - b[foreground]))), 4),
        'foreground_fraction': round(float(foreground.mean()), 5),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output CSV exists; choose a new audit path')
    rows = []
    for manifest in sorted(args.manifest_directory.glob('*.csv')):
        with manifest.open(newline='', encoding='utf-8') as handle:
            for item in csv.DictReader(handle):
                result = compare(item)
                rows.append(result)
                print(f"{result['slide_id']} {result['scanner']}: "
                      f"r={result['foreground_correlation']:.4f}, "
                      f"MAE={result['foreground_mae']:.2f}", flush=True)
    if not rows:
        raise ValueError('No manifest rows')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f'Compared {len(rows)} same-scanner pairs; wrote {args.output}', flush=True)


if __name__ == '__main__':
    main()
