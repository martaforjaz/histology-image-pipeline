"""Freeze disjoint 40x batch shards balanced by padded output pixel count."""

import argparse
import csv
from pathlib import Path

from apply_40x_tiled import canvas_parameters
from apply_registration_to_20x_image import load_mat_file
from run_40x_manifest_batch import load_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-directory', required=True, type=Path)
    parser.add_argument('--output-directory', required=True, type=Path)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--exclude', action='append', default=[])
    args = parser.parse_args()
    if args.workers < 1 or args.output_directory.exists():
        parser.error('Workers must be positive; output directory must not exist')
    rows = [row for row in load_rows(args.manifest_directory)
            if row['transform_name'] not in args.exclude]
    metadata = {}
    weighted = []
    for row in rows:
        warps = row['warps_dir']
        if warps not in metadata:
            metadata[warps] = next(v for p in Path(warps).glob('*.mat')
                                   if 'szz' in (v := load_mat_file(p)) and 'padall' in v)
        shape, _ = canvas_parameters((int(row['height']), int(row['width'])),
                                     metadata[warps], 5 / float(row['mpp_x']))
        weighted.append((shape[0] * shape[1], row))
    loads = [0] * args.workers
    groups = [[] for _ in loads]
    for pixels, row in sorted(weighted, key=lambda item: (-item[0], item[1]['transform_name'])):
        worker = min(range(args.workers), key=lambda i: loads[i])
        groups[worker].append(row)
        loads[worker] += pixels
    if sum(map(len, groups)) != len(rows):
        raise AssertionError('Shard count mismatch')
    args.output_directory.mkdir(parents=True)
    for i, group in enumerate(groups, 1):
        folder = args.output_directory / f'worker_{i:02d}'
        folder.mkdir()
        with (folder / 'manifest.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=group[0].keys())
            writer.writeheader()
            writer.writerows(sorted(group, key=lambda row: row['transform_name']))
        print(f'Worker {i}: {len(group)} images, {loads[i-1]/1e9:.1f} Gpixels')
    print(f'Frozen {len(rows)} unique images in {args.output_directory}')


if __name__ == '__main__':
    main()
