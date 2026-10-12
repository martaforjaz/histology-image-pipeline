"""Apply numeric CODA transforms to a 40x OME-TIFF without full-slide arrays.

The source is read in regions with OpenSlide. The destination is written as
JPEG-compressed tiled OME-TIFF. No original image is copied or modified.
"""

import argparse
import math
import os
import uuid
from pathlib import Path

import cv2
import numpy as np
import tifffile
from openslide import OpenSlide

from apply_registration_to_20x_image import load_mat_file, read_tiff_mpp


FILL = 241


def canvas_parameters(source_hw, metadata, scale):
    reference_hw = np.ceil(np.asarray(metadata['szz']).reshape(2) * scale).astype(np.int64)
    padall = math.ceil(float(np.asarray(metadata['padall']).squeeze()) * scale)
    difference = reference_hw - np.asarray(source_hw, dtype=np.int64)
    if np.any(difference < 0):
        raise ValueError(f'Reference canvas {reference_hw.tolist()} smaller than 40x input {source_hw}')
    pre = difference // 2 + padall
    output_hw = reference_hw + 2 * padall
    return tuple(map(int, output_hw)), tuple(map(int, pre))


def inverse_affine(values, scale):
    matrix = np.asarray(values['tform_python'], dtype=np.float64).copy()
    if matrix.shape != (3, 3):
        raise ValueError('Numeric CODA transform must be 3x3')
    matrix[2, :2] *= scale
    center = np.asarray(values['cent'], dtype=np.float64).reshape(2) * scale
    shift = np.array([[1, 0, 1-center[0]], [0, 1, 1-center[1]], [0, 0, 1]], dtype=np.float64)
    unshift = np.array([[1, 0, center[0]-1], [0, 1, center[1]-1], [0, 0, 1]], dtype=np.float64)
    return np.linalg.inv(unshift @ matrix.T @ shift)[:2, :].astype(np.float32)


def displacement_components(field):
    field = np.asarray(field)
    if field.shape[-1] == 2:
        return field[..., 0].astype(np.float32), field[..., 1].astype(np.float32)
    if field.shape[0] == 2:
        return field[0].T.astype(np.float32), field[1].T.astype(np.float32)
    raise ValueError(f'Unexpected CODA displacement shape: {field.shape}')


def displacement_tile(dx, dy, x0, y0, width, height, output_hw, scale):
    out_h, out_w = output_hw
    src_h, src_w = dx.shape
    xs = (np.arange(x0, x0+width, dtype=np.float32) + .5) * (src_w / out_w) - .5
    ys = (np.arange(y0, y0+height, dtype=np.float32) + .5) * (src_h / out_h) - .5
    map_x = np.broadcast_to(xs[None, :], (height, width)).copy()
    map_y = np.broadcast_to(ys[:, None], (height, width)).copy()
    delta_x = cv2.remap(dx, map_x, map_y, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE) * scale
    delta_y = cv2.remap(dy, map_x, map_y, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE) * scale
    return delta_x, delta_y


def render_tile(slide, x0, y0, tile_size, output_hw, pre, transform=None,
                flip=False, displacement=None, scale=1.0):
    out_h, out_w = output_hw
    height = min(tile_size, out_h-y0)
    width = min(tile_size, out_w-x0)
    xs = np.broadcast_to(np.arange(x0, x0+width, dtype=np.float32)[None, :],
                         (height, width))
    ys = np.broadcast_to(np.arange(y0, y0+height, dtype=np.float32)[:, None],
                         (height, width))
    valid = np.ones((height, width), dtype=bool)
    if displacement is not None:
        dx, dy = displacement_tile(*displacement, x0, y0, width, height,
                                   output_hw, scale)
        gx = xs + dx
        gy = ys + dy
        valid &= (gx >= 0) & (gx <= out_w-1) & (gy >= 0) & (gy <= out_h-1)
        gx = np.floor(gx + .5).astype(np.float32)
        gy = np.floor(gy + .5).astype(np.float32)
    else:
        gx, gy = xs, ys
    if transform is not None:
        a, b, tx = map(float, transform[0])
        c, d, ty = map(float, transform[1])
        px = (a * gx + b * gy + tx).astype(np.float32)
        py = (c * gx + d * gy + ty).astype(np.float32)
        valid &= (px >= 0) & (px <= out_w-1) & (py >= 0) & (py <= out_h-1)
        px = np.floor(px + .5).astype(np.int64)
        py = np.floor(py + .5).astype(np.int64)
        if flip:
            py = out_h - 1 - py
    else:
        px = gx.astype(np.int64)
        py = gy.astype(np.int64)
    sx = px - pre[1]
    sy = py - pre[0]
    source_w, source_h = slide.dimensions
    valid &= (sx >= 0) & (sx < source_w) & (sy >= 0) & (sy < source_h)
    tile = np.full((tile_size, tile_size, 3), FILL, dtype=np.uint8)
    if not np.any(valid):
        return tile
    left, top = int(sx[valid].min()), int(sy[valid].min())
    right, bottom = int(sx[valid].max()+1), int(sy[valid].max()+1)
    region = np.asarray(slide.read_region((left, top), 0,
                                          (right-left, bottom-top)).convert('RGB'))
    tile[:height, :width][valid] = region[sy[valid]-top, sx[valid]-left]
    return tile


def apply_tiled(source, warps, transform_name, output, registration_mpp=5.0,
                tile_size=512, progress=None):
    source, warps, output = map(Path, (source, warps, output))
    if not source.is_file() or not warps.is_dir() or output.exists():
        raise ValueError('Source/warps must exist and output must not already exist')
    if tile_size < 128 or tile_size % 16:
        raise ValueError('tile_size must be at least 128 and divisible by 16')
    global_path = warps / (transform_name + '.mat')
    if not global_path.is_file():
        raise FileNotFoundError(global_path)
    mppx, mppy = read_tiff_mpp(source)
    if not np.isclose(mppx, mppy, rtol=1e-4):
        raise ValueError('40x image must have square pixels')
    scale = registration_mpp / mppx
    metadata = None
    for mat in sorted(warps.glob('*.mat')):
        values = load_mat_file(mat)
        if 'szz' in values and 'padall' in values:
            metadata = values
            break
    if metadata is None:
        raise ValueError('Missing CODA canvas metadata')
    current = load_mat_file(global_path)
    moving = 'tform_python' in current or 'f' in current
    transform = inverse_affine(current, scale) if moving else None
    flip = bool(int(np.asarray(current.get('f', 0)).squeeze())) if moving else False
    displacement = None
    if moving:
        elastic_path = warps / 'D' / (transform_name + '.mat')
        if not elastic_path.is_file():
            raise FileNotFoundError(elastic_path)
        displacement = displacement_components(load_mat_file(elastic_path)['D'])
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + '.partial-' + uuid.uuid4().hex + output.suffix)
    try:
        with OpenSlide(str(source)) as slide:
            source_hw = (slide.dimensions[1], slide.dimensions[0])
            output_hw, pre = canvas_parameters(source_hw, metadata, scale)
            out_h, out_w = output_hw
            count = math.ceil(out_h/tile_size) * math.ceil(out_w/tile_size)
            def tiles():
                index = 0
                for y0 in range(0, out_h, tile_size):
                    for x0 in range(0, out_w, tile_size):
                        index += 1
                        yield render_tile(slide, x0, y0, tile_size, output_hw, pre,
                                          transform, flip, displacement, scale)
                        if progress is not None and (index % 100 == 0 or index == count):
                            progress(index, count)
            metadata_ome = {'axes': 'YXC', 'PhysicalSizeX': mppx,
                            'PhysicalSizeXUnit': 'µm', 'PhysicalSizeY': mppy,
                            'PhysicalSizeYUnit': 'µm'}
            with tifffile.TiffWriter(partial, bigtiff=True, ome=True) as writer:
                writer.write(tiles(), shape=(out_h, out_w, 3), dtype=np.uint8,
                             tile=(tile_size, tile_size), photometric='rgb',
                             compression='jpeg', compressionargs={'level': 90},
                             resolution=(10000/mppx, 10000/mppy),
                             resolutionunit='CENTIMETER', metadata=metadata_ome)
        with tifffile.TiffFile(partial) as check:
            if check.series[0].shape != (out_h, out_w, 3):
                raise ValueError('Written OME-TIFF dimensions failed validation')
        if output.exists():
            raise FileExistsError(output)
        os.rename(partial, output)
        return output_hw
    except Exception:
        # Keep incomplete output for forensic inspection; never call it registered.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-file', required=True, type=Path)
    parser.add_argument('--warps', required=True, type=Path)
    parser.add_argument('--transform-name', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--registration-mpp', type=float, default=5.0)
    parser.add_argument('--tile-size', type=int, default=512)
    args = parser.parse_args()
    if Path(args.transform_name).name != args.transform_name:
        parser.error('transform-name must be a plain stem')
    def progress(done, total):
        print(f'{args.transform_name}: {done}/{total} tiles', flush=True)
    shape = apply_tiled(args.input_file, args.warps, args.transform_name,
                        args.output, args.registration_mpp, args.tile_size, progress)
    print(f'Completed {args.output}: {shape}', flush=True)


if __name__ == '__main__':
    main()
