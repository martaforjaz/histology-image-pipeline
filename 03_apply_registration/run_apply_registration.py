"""Apply existing CODA transforms at any supported image resolution."""
import argparse
import math
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description='Apply CODA transforms to calibrated high-resolution images.')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Directory of images with matching warp stems')
    source.add_argument('--input-file', type=Path, help='One original image; no copy or link needed')
    p.add_argument('--transform-name', help='Warp stem for --input-file, e.g. Hum Brst Cancer A_S210')
    p.add_argument('--max-working-pixels', type=int, default=500_000_000,
                   help='Safety cap for full-image --input-file mode (default: 500 million).')
    p.add_argument('--warps', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--registration-mpp', type=float, required=True,
                   help='Micrometres/pixel of the images used to CALCULATE registration (sx)')
    p.add_argument('--view', action='store_true')
    p.add_argument('--scanner', default='unknown', help='Original scanner/model')
    p.add_argument('--scanner-manifest', type=Path, help='Optional CSV: image,scanner')
    args = p.parse_args()
    if args.input is not None and not args.input.is_dir():
        p.error('Provide an existing input directory.')
    if args.input_file is not None and not args.input_file.is_file():
        p.error('Provide an existing input file.')
    if not args.warps.is_dir() or not (args.warps / 'D').is_dir():
        p.error('Provide a save_warps directory containing D.')
    if args.input_file is not None and not args.transform_name:
        p.error('--input-file requires --transform-name.')
    if args.input is not None and args.transform_name:
        p.error('--transform-name is only supported with --input-file.')
    if not math.isfinite(args.registration_mpp) or args.registration_mpp <= 0:
        p.error('registration-mpp must be finite and positive.')
    input_dir = args.input if args.input is not None else args.input_file.parent
    if input_dir.resolve() == args.output.resolve():
        p.error('Choose an output directory different from the input directory.')
    if args.input_file is not None:
        stem = args.transform_name
        if Path(stem).name != stem or stem in ('.', '..'):
            p.error('--transform-name must be a plain file stem.')
        if not (args.warps / (stem + '.mat')).is_file():
            p.error('No matching global MAT for --transform-name.')
        if args.max_working_pixels <= 0:
            p.error('--max-working-pixels must be positive.')
        import tifffile
        with tifffile.TiffFile(args.input_file) as tif:
            shape = tif.series[0].shape
        if len(shape) < 2 or shape[0] * shape[1] > args.max_working_pixels:
            p.error('40x image exceeds the full-image memory safety cap; use a tiled/streaming implementation.')
        from apply_registration_to_20x_image import load_mat_file, read_tiff_mpp
        mppx, _ = read_tiff_mpp(args.input_file)
        for mat in args.warps.glob('*.mat'):
            metadata = load_mat_file(mat)
            if 'szz' in metadata and 'padall' in metadata:
                import numpy as np
                scale = args.registration_mpp / mppx
                canvas = np.ceil(np.asarray(metadata['szz']).reshape(2) * scale)
                canvas += 2 * math.ceil(float(np.asarray(metadata['padall']).squeeze()) * scale)
                if canvas[0] * canvas[1] > args.max_working_pixels:
                    p.error('40x padded canvas exceeds the full-image memory safety cap; use a tiled/streaming implementation.')
                break
        else:
            p.error('No warp MAT contains canvas metadata.')
    from apply_registration_to_20x_image import apply_registration_to_20x, process_images
    from pipeline_timing import timing_settings
    with timing_settings(args.scanner, args.scanner_manifest):
        if args.input_file is not None:
            process_images(str(input_dir.resolve()), [args.input_file.name],
                           str(args.warps.resolve()), args.registration_mpp,
                           str(args.output.resolve()), int(args.view), ome=1,
                           transform_name=args.transform_name)
        else:
            apply_registration_to_20x(str(input_dir.resolve()), str(args.warps.resolve()),
                                     args.registration_mpp, int(args.view),
                                     str(args.output.resolve()), ome=1)


if __name__ == '__main__':
    main()
