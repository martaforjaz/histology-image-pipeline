import os
import glob
import tifffile
from pathlib import Path
from math import ceil
from PIL import Image
import scipy.io as sio
import numpy as np, cv2
from openslide import OpenSlide
import matplotlib.pyplot as plt

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '01_conversion_downsampling'))
from image_metadata import read_tiff_mpp, validate_mpp

Image.MAX_IMAGE_PIXELS = None  # Disable the limit

def save_ome_tif(image, output_name, pixelsize):
    """Exports an image in ome-tif format with metadata compatible with Qupath.

        Each ome-tif file will contain pyramidal copies saved at downsample factors of
        [1, 2, 4, 8, 16, 32] and tiled at a size of [1024 x 1024] for rapid loading
        """

    # some settings for the ome-tif file
    tile_size = 1024
    compression_quality = 95
    scale_factors = [1, 2, 4, 8, 16]  # Define downsampling factors to save in each ome-tif

    # Ensure shape matches expected format (Y, X, Channels)
    image = np.array(image)
    shape = image.shape
    axes = 'YXS' if shape[-1] == 1 else 'YXC'  # 'YXC' for RGB, 'YXS' for single-channel

    metadata = {
        'axes': axes,
        'SignificantBits': 8,
        'PhysicalSizeX': pixelsize,
        'PhysicalSizeXUnit': 'µm',
        'PhysicalSizeY': pixelsize,
        'PhysicalSizeYUnit': 'µm',
        'Software': 'tifffile',
    }

    options = dict(
        photometric='rgb' if shape[-1] == 3 else 'minisblack',
        tile=(tile_size, tile_size),
        compression='jpeg',
        compressionargs={"level": compression_quality},
        resolutionunit=3,  # 3 = Centimeter
    )

    with tifffile.TiffWriter(output_name, bigtiff=True) as tif:
        subifds_data = []  # List to store downsampled images for SubIFDs

        # Generate downsampled images
        for scale in scale_factors[1:]:  # Start from the second level (skip 1x)
            new_size = (shape[1] // scale, shape[0] // scale)
            if min(new_size) < 1:
                break  # Stop if downsampling is too small

            downsampled = Image.fromarray(image).resize(new_size, resample=Image.Resampling.LANCZOS)
            subifds_data.append(np.array(downsampled, dtype=np.uint8))

        # Save main image with SubIFDs for pyramidal TIFF structure
        tif.write(
            image.astype(np.uint8),
            subifds=len(subifds_data),  # Define how many SubIFDs will follow
            resolution=(1e4 / pixelsize, 1e4 / pixelsize),
            metadata=metadata,
            **options
        )

        # Write the SubIFDs (pyramidal levels)
        for idx, sub_image in enumerate(subifds_data):
            scale = scale_factors[idx + 1]  # Get scale factor
            res_val = 1e4 / scale / pixelsize

            tif.write(
                sub_image,
                subfiletype=1,  # Mark as pyramid level
                resolution=(res_val, res_val),
                **options
            )

        # Add a thumbnail image for QuPath and ImageScope
        thumbnail = image[::8, ::8]  # Downsample by factor of 8
        tif.write(thumbnail.astype(np.uint8), metadata={'Name': 'thumbnail'})

def load_mat_file(path):
    """
    Load variables from a MATLAB .mat file (handles v7.3 HDF5 and older).
    Returns a dict mapping variable names to numpy arrays / Python objects.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such file: {path}")

    # Try legacy MAT (<= v7.2) via SciPy
    try:
        try:
            data = sio.loadmat(path, squeeze_me=True, struct_as_record=False, simplify_cells=True)
        except TypeError:
            # simplify_cells not available on older SciPy
            data = sio.loadmat(path, squeeze_me=True, struct_as_record=False)
        # drop metadata keys
        return {k: v for k, v in data.items() if not k.startswith("__")}
    except Exception:
        # Fall back to HDF5 (v7.3) via h5py
        import h5py

        def _read_h5(obj):
            import h5py
            if isinstance(obj, h5py.Dataset):
                arr = obj[()]
                # convert bytes -> str where appropriate
                if isinstance(arr, bytes):
                    return arr.decode("utf-8", "ignore")
                if hasattr(arr, "dtype") and arr.dtype.kind == "S":
                    return arr.astype(str)
                if isinstance(arr, np.ndarray) and arr.ndim > 1 and 'MATLAB_class' in obj.attrs:
                    arr = arr.transpose(tuple(reversed(range(arr.ndim))))
                return arr
            elif isinstance(obj, h5py.Group):
                return {k: _read_h5(obj[k]) for k in obj.keys()}
            else:
                return obj

        out = {}
        with h5py.File(path, "r") as f:
            for k in f.keys():
                out[k] = _read_h5(f[k])
        return out

def sample_nearest(image, map_x, map_y, fill=(241, 241, 241)):
    """Nearest sampling with explicit outside fill and no OpenCV dimension limit."""
    x = np.floor(map_x + 0.5).astype(np.int64)
    y = np.floor(map_y + 0.5).astype(np.int64)
    x, y = np.broadcast_arrays(x, y)
    valid = ((map_x >= 0) & (map_x <= image.shape[1]-1) &
             (map_y >= 0) & (map_y <= image.shape[0]-1))
    valid = np.broadcast_to(valid, x.shape)
    out = np.empty(x.shape + (image.shape[2],), dtype=image.dtype)
    out[...] = np.asarray(fill[:image.shape[2]], dtype=image.dtype)
    out[valid] = image[y[valid], x[valid]]
    return out


def register_image_elastic(image, displacement_field, scale, image_elastic, tile=8192):
    """
    Apply a dense displacement field using tiled NumPy nearest-neighbour sampling.
    - image: HxWxC (uint8/uint16)
    - displacement_field: HxWx2 (dx, dy) at some base scale
    - scale: multiply the displacement after resizing to the image size
    - tile: destination tile size, controlling coordinate-buffer memory
    """
    # Ensure types
    image = np.ascontiguousarray(image)
    H, W = image.shape[:2]
    C = 1 if image.ndim == 2 else image.shape[2]
    if image.ndim == 2:
        image = image[..., None]

    # Resize displacement to image size & scale
    displacement_field = np.asarray(displacement_field)
    print(f"      displacement_field shape: {np.asarray(displacement_field).shape}")
    if displacement_field.shape[-1] == 2:  # (H, W, 2)
        dx, dy = displacement_field[..., 0], displacement_field[..., 1]
        dy = cv2.resize(dy.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR) * scale
        dx = cv2.resize(dx.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR) * scale
    elif displacement_field.shape[0] == 2: # (2, H, W)
        dx, dy = displacement_field[0, ...], displacement_field[1, ...]
        dy, dx = dy.T, dx.T
        dy = cv2.resize(dy.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR) * scale
        dx = cv2.resize(dx.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR) * scale
    else:
        raise ValueError(f"Unexpected shape for displacement field: {displacement_field.shape}")


    # Output
    out = np.empty_like(image)
    fill = (241, 241, 241)

    # Process tiles
    for y0 in range(0, H, tile):
        for x0 in range(0, W, tile):
            ht = min(tile, H - y0)
            wt = min(tile, W - x0)

            dx_tile = dx[y0:y0+ht, x0:x0+wt]
            dy_tile = dy[y0:y0+ht, x0:x0+wt]

            xs = x0 + np.arange(wt, dtype=np.float64)[None, :] + dx_tile
            ys = y0 + np.arange(ht, dtype=np.float64)[:, None] + dy_tile
            out[y0:y0+ht, x0:x0+wt] = sample_nearest(image, xs, ys, fill)

    # Drop single-channel axis if needed
    out = out[..., 0] if C == 1 else out
    out = np.ascontiguousarray(out)

    return out

def transform_image(image, tform_T, flip, cent=None, scale=1.0, fill=(241, 241, 241), tile=4096):
    """
    MATLAB-equivalent:
        cent = cent * scale;
        tform.T(3,1:2) = tform.T(3,1:2) * scale;
        Rin = imref2d(size(IM));
        Rin.XWorldLimits -= cent(1); Rin.YWorldLimits -= cent(2);
        IM = imwarp(IM, Rin, tform, 'nearest', 'outputview', Rin, 'fillvalues', fill);
    """
    import numpy as np, cv2

    img = np.ascontiguousarray(image)
    h, w = img.shape[:2]
    C = img.shape[2] if img.ndim == 3 else 1
    if img.ndim == 2:
        img = img[..., None]

    # (1) optional vertical flip (MATLAB flips before register_IM)
    if int(flip) == 1:
        img = img[::-1, ...]

    # (2) center shift (MATLAB: [cent_x, cent_y], scaled)
    if cent is None:
        cx = cy = 0.0
    else:
        cx = float(cent[0]) * float(scale)  # X (cols)
        cy = float(cent[1]) * float(scale)  # Y (rows)

    # (3) MATLAB affine2d.T is row-vector form; scale translation, then transpose
    T_row = np.array(tform_T, dtype=np.float64, copy=True)
    if T_row.shape != (3, 3):
        raise ValueError("tform_T must be 3x3 (MATLAB affine2d.T)")
    T_row[2, 0:2] *= float(scale)
    T_col = T_row.T  # convert to column-vector convention

    # (4) MATLAB world coordinates = zero-based array index + 1 - centre.
    S     = np.array([[1, 0, 1-cx],
                      [0, 1, 1-cy],
                      [0, 0,   1]], dtype=np.float64)
    S_inv = np.array([[1, 0, cx-1],
                      [0, 1, cy-1],
                      [0, 0,   1]], dtype=np.float64)

    # forward (src→dst) in index space
    M_full = S_inv @ T_col @ S

    # OpenCV needs inverse (dst→src)
    Minv = np.linalg.inv(M_full)
    M_cv2 = Minv[:2, :].astype(np.float32)

    # Tile destination coordinates and sample the full source array directly.
    a, b, tx = float(M_cv2[0, 0]), float(M_cv2[0, 1]), float(M_cv2[0, 2])
    c, d, ty = float(M_cv2[1, 0]), float(M_cv2[1, 1]), float(M_cv2[1, 2])
    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    out = np.empty_like(img)

    for y0 in range(0, h, tile):
        ht = min(tile, h - y0)
        Y = ys[y0:y0+ht][:, None]                # (ht,1)
        for x0 in range(0, w, tile):
            wt = min(tile, w - x0)
            X = xs[x0:x0+wt][None, :]            # (1,wt)

            # Global dest->source maps for this dest tile
            map_x = (a * X + b * Y + tx).astype(np.float32)  # (ht,wt)
            map_y = (c * X + d * Y + ty).astype(np.float32)  # (ht,wt)

            out[y0:y0+ht, x0:x0+wt] = sample_nearest(img, map_x, map_y, fill)

    return np.ascontiguousarray(out[..., 0] if C == 1 else out)

def pad_image(image, refsize, padall):
    """pads a pixmap image to a defined width and height.
    Used in multiple tabs
    Args:
        image: image array
        refsize: maximum size of images in the registration project
        padall: additional sizes to add to the registered image
    Returns:
        image_pad: padded image array
        image_pad_grey: greyscale padded image array
    """

    fillval = (241, 241, 241)
    img = np.asarray(image)
    if img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("image must be an (H, W, 3) array")

    refsize = np.asarray(refsize, dtype=int).reshape(2)  # [rows, cols]
    H, W = img.shape[:2]
    szim = refsize - np.array([H, W], dtype=int)  # how much to add overall

    if (szim < 0).any():
        raise ValueError(f"refsize {refsize.tolist()} is smaller than image size {[H, W]}")

    padall_arr = np.asarray(padall)
    if padall_arr.size == 1:
        padall_arr = np.array([int(padall_arr), int(padall_arr)], dtype=int)
    else:
        padall_arr = padall_arr.astype(int).reshape(2)

    szA = (szim // 2).astype(int)  # pre pad (base)
    szB = (szim - szA + padall_arr).astype(int)  # post pad (base + padall)
    szA = (szA + padall_arr).astype(int)  # pre pad (+ padall)

    # Build pad widths: ((pre_rows, post_rows), (pre_cols, post_cols))
    pads_2d = ((int(szA[0]), int(szB[0])), (int(szA[1]), int(szB[1])))

    # Per-channel constant padding (MATLAB pads each channel separately with its own fill)
    ch0 = np.pad(img[..., 0], pads_2d, mode='constant', constant_values=int(fillval[0]))
    ch1 = np.pad(img[..., 1], pads_2d, mode='constant', constant_values=int(fillval[1]))
    ch2 = np.pad(img[..., 2], pads_2d, mode='constant', constant_values=int(fillval[2]))

    image_pad = np.stack((ch0, ch1, ch2), axis=2).astype(img.dtype, copy=False)
    return image_pad

def overlay_rgb(image_a, image_b, view_image, alpha=0.5, filename=None):
    """
    Overlay image_b on image_a.
    """
    image_a = np.asarray(image_a, dtype=np.uint8)
    image_b = np.asarray(image_b, dtype=np.uint8)
    assert image_a.ndim == 3 and image_a.shape[2] == 3
    assert image_b.ndim == 3 and image_b.shape[2] == 3

    h, w = image_a.shape[:2]
    b_resized = cv2.resize(image_b, (w, h), interpolation=cv2.INTER_NEAREST)
    overlay = cv2.addWeighted(image_a, 1.0 - float(alpha), b_resized, float(alpha), 0)

    if view_image:
        plt.imshow(overlay), plt.axis('off'), plt.show()

    if filename:
        Image.fromarray(overlay).save(filename, quality=95, subsampling=0, optimize=True, dpi=(300, 300))

    return overlay

def overlay_greyscale(image_a, image_b, view_image, filename=None):
    """
    Overlay image_b on image_a.
    """
    image_a = np.asarray(image_a, dtype=np.uint8)
    image_b = np.asarray(image_b, dtype=np.uint8)
    assert image_a.ndim == 3 and image_a.shape[2] == 3
    assert image_b.ndim == 3 and image_b.shape[2] == 3

    h, w = image_a.shape[:2]
    image_b = cv2.resize(image_b, (w, h), interpolation=cv2.INTER_NEAREST)
    image_a = cv2.bitwise_not(cv2.cvtColor(image_a, cv2.COLOR_RGB2GRAY))
    image_b = cv2.bitwise_not(cv2.cvtColor(image_b, cv2.COLOR_RGB2GRAY))
    overlay = np.dstack([image_a, image_a, image_b])

    if view_image:
        plt.imshow(overlay), plt.axis('off'), plt.show()

    if filename:
        Image.fromarray(overlay).save(filename, quality=95, subsampling=0, optimize=True, dpi=(300, 300))

    return overlay

def apply_CODA_registration(image, image_name, pthdata, scale, view_image, timing_row=None):

    # Load the registration metadata
    global_registration_file = os.path.join(pthdata, image_name+'.mat')
    elastic_registration_file = os.path.join(pthdata, 'D', image_name+'.mat')

    # Read only the elastic preview used for validation.
    preview = Path(pthdata).parent / (image_name + '.jpg')
    with Image.open(preview) as im:
        image_elastic = im.convert('RGB').copy()

    # The reference MAT can sort first and contains zc only in original CODA.
    metadata = None
    for candidate in sorted(Path(pthdata).glob('*.mat')):
        values = load_mat_file(candidate)
        if 'szz' in values and 'padall' in values:
            metadata = values
            break
    if metadata is None:
        raise ValueError('No MAT file contains szz and padall; calculate registration first.')
    refsize = np.ceil(np.asarray(metadata['szz']).reshape(2) * scale).astype(np.int64)
    padall = ceil(np.asarray(metadata['padall']).squeeze().item() * float(scale))
    image = pad_image(image, refsize, padall)

    # account for the situation where the mat file exists but tform is missing (reference image
    if not os.path.isfile(global_registration_file):
        raise FileNotFoundError(global_registration_file)
    moving_image = 0
    if os.path.isfile(global_registration_file):
        vars_dict = load_mat_file(global_registration_file)
        moving_image = int('tform_python' in vars_dict) or int('f' in vars_dict)

    # account for reference image
    if not moving_image:
        if timing_row is not None:
            timing_row["status"] = "reference"
        print("         ...reference image, registration not required. save padded image.")
        overlay = overlay_greyscale(image_elastic, image, view_image)
        return image, overlay

    # load image-specific registration information
    vars_dict = load_mat_file(global_registration_file)
    vars_dict_elastic = load_mat_file(elastic_registration_file)

    # Numeric exports avoid requiring a MATLAB session during application.
    if 'tform_python' in vars_dict:
        tform = np.asarray(vars_dict['tform_python'], dtype=float)
    else:
        import matlab.engine
        eng = matlab.engine.start_matlab()
        try:
            safe_path = str(global_registration_file).replace('\\', '/').replace("'", "''")
            eng.eval(f"load('{safe_path}')", nargout=0)
            eng.eval("T = tform.T;", nargout=0)
            tform = np.array(eng.workspace['T'])
        finally:
            eng.quit()
    flip = int(np.asarray(vars_dict.get('f', 0)).squeeze().item())
    cent = np.asarray(vars_dict['cent'], dtype=float).reshape(2)
    image = transform_image(image, tform, flip, cent, scale)
    #overlay_greyscale(image_global, image, view_image)

    # elastic registration
    D = vars_dict_elastic.get('D')
    image = register_image_elastic(image, D, scale, image_elastic)

    overlay = overlay_greyscale(image_elastic, image, view_image)
    print("         ...registration successful!")

    return image, overlay

def process_images(pthim, image_list, pthdata, sx, out_folder, view_image=0, ome=1,
                   transform_name=None):
    """Process missing images by converting .ndpi, .svs, .scn, or .tif files to .tif or .ome.tif."""

    # Ensure the image directory exists
    out_folder_v = os.path.join(out_folder, 'validation_overlay')
    if not os.path.isdir(out_folder_v):
        os.makedirs(out_folder_v)

    from pipeline_timing import TimingLog
    timing = TimingLog(out_folder, "apply_registration")
    with timing.measure(phase="batch_total"):
        for idx, image_in_list in enumerate(image_list):
            with timing.measure(os.path.join(pthim, image_in_list)) as image_timing:
                print(f"  Starting image {idx + 1} of {len(image_list)}: {image_in_list}...")

                # check if the image is already registered
                #image_name = image_in_list.rsplit('.', 1)[0]
                image_name = transform_name or remove_extension(image_in_list)
                if ome==1:
                    output_name = os.path.join(out_folder, image_name + '.ome.tif')
                else:
                    output_name = os.path.join(out_folder, image_name + '.tif')
                if os.path.exists(output_name):
                    image_timing["status"] = "skipped_existing"
                    print(f"    ...already saved this file")
                    continue

                # Read the image
                slide_path = os.path.join(pthim, image_in_list)
                with timing.measure(slide_path, "read", Path(pthim).name) as read_timing:
                    try:
                        # Get file extension
                        file_ext = os.path.splitext(slide_path)[-1].lower()
                        if file_ext in ['.ndpi', '.svs', '.scn']:
                            print(f"    ...reading {slide_path} with OpenSlide")
                            wsi = OpenSlide(slide_path)
                            image = wsi.read_region(location=(0, 0), level=0, size=wsi.level_dimensions[0]).convert('RGB')
                            w, h = image.width, image.height
                            mppx, mppy = float(wsi.properties['openslide.mpp-x']), float(wsi.properties['openslide.mpp-y'])
                        elif file_ext in ['.tif', '.tiff']:
                            print(f"    ...reading {slide_path} with PIL")
                            image = Image.open(slide_path).convert('RGB')
                            mppx, mppy = read_tiff_mpp(slide_path)
                            w, h = image.size[:2]

                        read_timing["mpp"] = mppx
                        print(f"       ...image read successfully - file parameters: resolution of {mppx} and size of ({w}, {h})")

                    except Exception as e:
                        print(f"       ...ERROR reading {image_in_list}: {e}")
                        raise

                # get the scale between the high-resolution and registered images
                validate_mpp(mppx, mppy)
                if not np.isclose(mppx, mppy, rtol=1e-4):
                    raise ValueError('Registration application currently requires square pixels.')
                image_timing["mpp"] = mppx
                image_timing["resolution"] = Path(pthim).name
                image_timing["detail"] = f"registration_mpp={sx}; view={view_image}; ome={ome}"
                scale = sx / mppx

                # register the image
                with timing.measure(slide_path, "apply", Path(pthim).name, mppx):
                    image, overlay = apply_CODA_registration(image, image_name, pthdata, scale, view_image, image_timing)
                with timing.measure(slide_path, "save", Path(pthim).name, mppx):
                    filename = os.path.join(out_folder_v, image_name + '.jpg')
                    Image.fromarray(overlay).save(filename, quality=95, subsampling=0, optimize=True, dpi=(300, 300))

                    # save the file as either a normal or an ome-tif
                    if ome == 1:
                        print("             ...saving as an ome tif")
                        save_ome_tif(image, output_name, mppx)
                    else:
                        try: # save as normal tif
                            Image.fromarray(image).save(output_name, resolution=1e4 / mppx, resolution_unit=3, compression=None)
                        except Exception as e: # Preserve failure so timing and exit status remain accurate
                            print(f"          ...error saving {image_in_list} as tif: {e}")
                            raise

                print("  Image save successful!")
                print("  ")

def apply_registration_to_20x(pthim, pthdata, sx, validate_imgs=0, out_folder=None, ome=1):
    print('Applying CODA registration to calibrated images:')

    # Get the .ndpi and .svs image names, sorted alphabetically
    patterns = ['*.ndpi', '*.svs', '*.scn', '*.tif', '*.tiff']
    image_list = [file for pattern in patterns for file in [str(f) for f in Path(pthim).iterdir() if f.is_file() and f.name.lower().endswith(pattern[1:])]]
    image_list = sorted(image_list)
    image_list = [os.path.basename(file) for file in image_list]
    if not image_list:
        print("  No image files found.")
        return
    else:
        print(f"  Of {len(image_list)} image files:")

    # Every input must have a matching global MAT; references do not need D.
    mat_paths = glob.glob(os.path.join(pthdata, "*.mat"))
    mat_names = {os.path.splitext(os.path.basename(p))[0] for p in mat_paths}  # basenames without .mat

    filtered, dropped = [], []
    for img in image_list:
        base = remove_extension((img))
        #base = os.path.splitext(img)[0]
        if base in mat_names:
            filtered.append(img)
        else:
            dropped.append(img)

    if dropped:
        raise ValueError('No matching global MAT for: ' + ', '.join(dropped))
    image_list = filtered
    if not image_list:
        print("  No images have matching .mat files; nothing to process.")
        return

    if not out_folder:
        out_folder = os.path.join(pthim, 'registeredE')
    process_images(pthim, image_list, pthdata, sx, out_folder, validate_imgs, ome)

def remove_extension(filename):
    name = Path(filename).name
    for suffix in ('.ome.tiff', '.ome.tif', '.tiff', '.tif', '.ndpi', '.svs', '.scn', '.png', '.jpg'):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return Path(name).stem


if __name__ == '__main__':
    raise SystemExit('Use run_apply_registration.py --help for explicit paths.')
