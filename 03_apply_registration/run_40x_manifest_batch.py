"""Sequential, auditable 40x tiled application from verified source manifests."""

import argparse
import csv
import datetime as dt
import json
import time
from pathlib import Path

from apply_40x_tiled import apply_tiled


def load_rows(manifest_directory):
    rows = []
    files = sorted(manifest_directory.glob('*.csv'))
    if not files:
        raise ValueError('No 40x manifest CSVs found')
    for file in files:
        with file.open(newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle)
            required = {'slide_id','scanner','source_40x','source_bytes',
                        'transform_name','warps_dir','destination_40x','destination_exists'}
            if not required.issubset(reader.fieldnames or []):
                continue  # Audit/result CSV in the same directory, not a source manifest.
            rows.extend(reader)
    if not rows:
        raise ValueError('No valid 40x source manifest rows found')
    stems = [r['transform_name'] for r in rows]
    if len(stems) != len(set(stems)):
        raise ValueError('Duplicate transform names across manifests')
    for row in rows:
        if row['destination_exists'].lower() != 'false':
            raise ValueError('A manifest was prepared with existing outputs; review before use')
        if row['scanner'] not in row['transform_name'] or not row['transform_name'].startswith(row['slide_id'] + '_'):
            raise ValueError(f"Invalid slide/scanner mapping: {row['transform_name']}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-directory', required=True, type=Path)
    parser.add_argument('--results', required=True, type=Path)
    parser.add_argument('--only', help='Run only this transform name, for a pilot')
    parser.add_argument('--exclude', action='append', default=[], help='Skip a previously validated pilot')
    parser.add_argument('--tile-size', type=int, default=512)
    args = parser.parse_args()
    if args.results.exists():
        parser.error('Choose a new results path; never replace a previous audit')
    rows = load_rows(args.manifest_directory)
    if args.only:
        rows = [r for r in rows if r['transform_name'] == args.only]
        if len(rows) != 1:
            parser.error('--only must match exactly one manifest row')
    rows = [r for r in rows if r['transform_name'] not in args.exclude]
    args.results.parent.mkdir(parents=True, exist_ok=True)
    print(f'Beginning {len(rows)} 40x images at {dt.datetime.now().isoformat(timespec="seconds")}', flush=True)
    with args.results.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['transform_name','source_40x','destination_40x',
                                                    'status','started_at','ended_at','seconds','detail'])
        writer.writeheader()
        handle.flush()
        for index, row in enumerate(rows, 1):
            stem = row['transform_name']
            source = Path(row['source_40x'])
            output = Path(row['destination_40x'])
            warps = Path(row['warps_dir'])
            if not source.is_file() or source.stat().st_size != int(row['source_bytes']):
                raise ValueError(f'Source missing or changed: {source}')
            if output.exists():
                raise FileExistsError(f'Output already exists, refusing overwrite: {output}')
            if not (warps / (stem + '.mat')).is_file():
                raise FileNotFoundError(f'Missing global warp: {stem}')
            if row['scanner'] != 'S210' and not (warps / 'D' / (stem + '.mat')).is_file():
                raise FileNotFoundError(f'Missing elastic warp: {stem}')
            started = dt.datetime.now().isoformat(timespec='seconds')
            t0 = time.monotonic()
            print(f'[{index}/{len(rows)}] {stem} -> {output}', flush=True)
            def progress(done, total):
                print(f'  {stem}: {done}/{total} tiles', flush=True)
            try:
                shape = apply_tiled(source, warps, stem, output, 5.0, args.tile_size, progress)
                status, detail = 'computed_pending_visual_qc', json.dumps({'shape': shape})
            except Exception as error:
                status, detail = 'failed', repr(error)
                raise
            finally:
                writer.writerow({'transform_name': stem, 'source_40x': str(source),
                                 'destination_40x': str(output), 'status': status,
                                 'started_at': started,
                                 'ended_at': dt.datetime.now().isoformat(timespec='seconds'),
                                 'seconds': round(time.monotonic()-t0, 2), 'detail': detail})
                handle.flush()
            print(f'Finished {stem} in {time.monotonic()-t0:.1f}s; {shape}', flush=True)
    print('All selected 40x images completed.', flush=True)


if __name__ == '__main__':
    main()
