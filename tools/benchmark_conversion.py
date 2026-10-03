"""Benchmark the original and streaming DICOM converters on the same slides.

Each run happens in a fresh process so peak memory is measured per conversion. After
both runs every output page (level 0, each pyramid level, thumbnail) is compared
tile by tile and must be pixel-identical.

    python tools/benchmark_conversion.py --input C:/bench/input --work C:/bench \
        --slides "Hum Kid Fetal B" --folder 2x 40x --mpp 5 0.25 --save-ome 0 1
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'01_conversion_downsampling'))


def child(args):
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')  # before numpy loads
    import psutil
    slide = os.path.join(args.input, args.slide + '.dcm')
    start = time.perf_counter()
    if args.impl == 'old':
        from WSI2OMEtif_All_file_types import WSI2tif
        WSI2tif(slide, args.folder, args.mpp, args.save_ome, 1, outpth=args.out)
    else:
        from streaming_conversion import convert_dicom
        convert_dicom(slide, args.out, args.folder, args.mpp, args.save_ome,
                      threads=args.threads, pyramid_dir=args.pyramid_dir)
    wall = time.perf_counter() - start
    proc = psutil.Process()
    mem, cpu = proc.memory_info(), proc.cpu_times()
    print('RESULT ' + json.dumps({
        'wall_s': round(wall, 1), 'cpu_s': round(cpu.user + cpu.system, 1),
        'peak_ram_gb': round(mem.peak_wset / 1e9, 2),         # peak physical memory
        'peak_commit_gb': round(mem.peak_pagefile / 1e9, 2),  # peak RAM + pagefile
    }), flush=True)


def compare(old, new):
    """Returns (pages compared, pages differing, max abs pixel difference)."""
    import numpy as np
    import tifffile
    with tifffile.TiffFile(old) as a, tifffile.TiffFile(new) as b:
        pages_a = list(a.series[0].levels) + list(a.pages[1:2])
        pages_b = list(b.series[0].levels) + list(b.pages[1:2])
        if len(pages_a) != len(pages_b):
            return len(pages_a), max(len(pages_a), len(pages_b)), None
        differing, worst = 0, 0
        for pa, pb in zip(pages_a, pages_b):
            pa = pa.keyframe if hasattr(pa, 'keyframe') else pa
            pb = pb.keyframe if hasattr(pb, 'keyframe') else pb
            if pa.shape != pb.shape:
                differing += 1
                continue
            if pa.is_tiled and pb.is_tiled and pa.tile == pb.tile:
                diff = 0
                for (ta, _, _), (tb, _, _) in zip(pa.segments(), pb.segments()):
                    if not np.array_equal(ta, tb):
                        diff = max(diff, int(np.abs(ta.astype(int) - tb.astype(int)).max()))
            else:
                x, y = pa.asarray(), pb.asarray()
                diff = int(np.abs(x.astype(int) - y.astype(int)).max())
            differing += diff > 0
            worst = max(worst, diff)
        return len(pages_a), differing, worst


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--input', required=True)
    p.add_argument('--work', required=True)
    p.add_argument('--slides', nargs='+', required=True)
    p.add_argument('--folder', nargs='+', required=True)
    p.add_argument('--mpp', nargs='+', type=float, required=True)
    p.add_argument('--save-ome', nargs='+', type=int, required=True)
    p.add_argument('--impls', nargs='+', default=['old', 'new'])
    p.add_argument('--threads', type=int)
    p.add_argument('--pyramid-dir')
    p.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--impl', help=argparse.SUPPRESS)
    p.add_argument('--slide', help=argparse.SUPPRESS)
    p.add_argument('--out', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.child:
        return child(args)

    work = Path(args.work)
    results = []
    for slide in args.slides:
        for impl in args.impls:
            out = work/f'out_{impl}'
            for folder, ome in zip(args.folder, args.save_ome):
                target = out/folder/(slide + ('.ome.tif' if ome else '.tif'))
                target.unlink(missing_ok=True)
            out.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, __file__, '--child', '--impl', impl, '--slide', slide,
                   '--input', args.input, '--work', args.work, '--out', str(out),
                   '--slides', slide, '--folder', *args.folder,
                   '--mpp', *map(str, args.mpp), '--save-ome', *map(str, args.save_ome)]
            if args.threads:
                cmd += ['--threads', str(args.threads)]
            if args.pyramid_dir and impl == 'new':
                cmd += ['--pyramid-dir', args.pyramid_dir]
            print(f'== {slide}: {impl}', flush=True)
            run = subprocess.run(cmd, capture_output=True, text=True)
            line = [l for l in run.stdout.splitlines() if l.startswith('RESULT ')]
            if run.returncode or not line:
                print(run.stdout[-3000:], run.stderr[-3000:])
                raise SystemExit(f'{impl} failed on {slide}')
            row = {'slide': slide, 'impl': impl, **json.loads(line[0][7:])}
            print('  ', row, flush=True)
            results.append(row)

        if {'old', 'new'} <= set(args.impls):
            for folder, ome in zip(args.folder, args.save_ome):
                name = slide + ('.ome.tif' if ome else '.tif')
                pages, differing, worst = compare(work/'out_old'/folder/name, work/'out_new'/folder/name)
                verdict = 'IDENTICAL' if differing == 0 else f'DIFFERENT ({differing} pages, max diff {worst})'
                print(f'   {folder}: {pages} page(s) compared -> {verdict}', flush=True)
                results.append({'slide': slide, 'impl': f'compare {folder}', 'pixels': verdict})

    with open(work/'benchmark_results.csv', 'a', newline='', encoding='utf-8') as stream:
        fields = ['slide', 'impl', 'wall_s', 'cpu_s', 'peak_ram_gb', 'peak_commit_gb', 'pixels']
        writer = csv.DictWriter(stream, fields)
        if stream.tell() == 0:
            writer.writeheader()
        writer.writerows(results)


if __name__ == '__main__':
    main()
