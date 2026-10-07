import os
import re
import sys
import subprocess
import glob
import tifffile
import numpy as np
from PIL import Image, ImageCms
from pipeline_timing import TimingLog
from pipeline_timing import active_scanner
from scanner_calibration import calibrate_mpp
from openslide import OpenSlide
from image_metadata import read_tiff_mpp, validate_mpp
from chunked_readers import read_rgb_chunks, read_openslide_chunks, read_tiff_chunks

try:
    import slideio
except ImportError:
    slideio = None

try:
    import wsidicom
except ImportError:
    wsidicom = None

try:
    from pylibCZIrw import czi as pyczi
except ImportError:
    pyczi = None

try:
    import isyntax
except ImportError:
    isyntax = None

# Burn the slide's embedded ICC color calibration into the pixels for .svs
# files (1 = on). Leica/Aperio scanners store display-referred 8-bit pixels
# plus an ICC profile in TIFF tag 34675; ImageScope applies it on screen, but
# most analysis tools do not, so raw exports look muted/gray. Applying the
# profile (perceptual intent, ImageScope's default) reproduces the true H&E
# color. Exports are tagged sRGB afterwards so viewers show them as-is.
APPLY_EMBEDDED_ICC = 1
SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
SRGB_EXTRATAG = (34675, 7, len(SRGB_PROFILE), SRGB_PROFILE, True)

# Apply the display gamma stored in the vsi metadata to the pixels (1 = on).
APPLY_DISPLAY_GAMMA = 1

# Suppress SLF4J warning
os.environ['SLF4J_DEFAULT_PROVIDER'] = 'org.slf4j.nop.NOPServiceProvider'
Image.MAX_IMAGE_PIXELS = None  # Disable the limit


def isyntax_wsi_mpp(slide_path):
    """Reads the WSI pixel size in microns from the XML header of an iSyntax file.

        libisyntax reports the pixel size of the macro image for these files, so the
        value is read from the header text that precedes the 0x04 terminator instead
        """

    header = bytearray()
    with open(slide_path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            idx = chunk.find(b'\x04')
            if idx >= 0:
                header += chunk[:idx]
                break
            header += chunk

    for block in header.decode('utf-8', 'replace').split('<DataObject ObjectType="DPScannedImage">'):
        if 'PMSVR="IString">WSI<' not in block:
            continue
        scales = re.findall(r'UFS_IMAGE_DIMENSION_SCALE_FACTOR"[^>]*>([\d.eE+-]+)<', block)
        if len(scales) >= 2:
            return float(scales[0]), float(scales[1])

    raise ValueError(f'no WSI pixel size found in the header of {slide_path}')


_ISYNTAX_PROBE = """
import sys, isyntax
slide = isyntax.ISyntax.open(sys.argv[1])
level = int(sys.argv[2])
try:
    w, h = slide.level_dimensions[level]
    for y in range(0, h, 4096):
        for x in range(0, w, 4096):
            slide.read_region(x, y, min(4096, w-x), min(4096, h-y), level)
finally:
    slide.close()
"""


def isyntax_decodes(slide_path, level, pixels):
    """Returns False if libisyntax cannot decode the requested level of an iSyntax file.

        Some i2syntax files hold codeblocks that send the decoder into an endless loop,
        so the level is first decoded in a separate process that can be killed
        """

    timeout = max(30.0, pixels / 4.0e6)
    try:
        probe = subprocess.run([sys.executable, '-c', _ISYNTAX_PROBE, slide_path, str(level)],
                               capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False

    return probe.returncode == 0


def read_isyntax(slide_path, target_um, load_native_resolution=1):
    """Reads an iSyntax file as an RGB image and returns it with its pixel size.

        Decode native resolution, or the existing coarser pyramid selection in
        fast mode, using bounded regions. The assembled RGB image remains in
        memory. Returns None if the file cannot be decoded.
        """

    mppx, mppy = isyntax_wsi_mpp(slide_path)
    slide = isyntax.ISyntax.open(slide_path)

    try:
        level = 0
        if target_um > 0 and not load_native_resolution:
            for lvl, factor in enumerate(slide.level_downsamples):
                if max(mppx, mppy) * factor <= target_um:
                    level = lvl

        w, h = slide.level_dimensions[level]
        if not isyntax_decodes(slide_path, level, w * h):
            return None

        image = read_rgb_chunks((w, h), lambda xy, size: slide.read_region(
            xy[0], xy[1], size[0], size[1], level)[..., :3])
        downsample = slide.level_downsamples[level]
    finally:
        slide.close()

    return image, mppx * downsample, mppy * downsample


def read_embedded_icc(slide_path):
    """Reads the ICC profile embedded in a TIFF-based slide (tag 34675), or None."""

    try:
        with tifffile.TiffFile(slide_path) as t:
            for page in t.pages:
                tag = page.tags.get(34675)
                if tag is not None:
                    return bytes(tag.value)
            return None
    except Exception:
        return None


def apply_embedded_icc(image, icc):
    """Converts a PIL image from the slide's ICC color space to sRGB in place.

        Applied strip-wise so the peak memory stays bounded for gigapixel level-0
        reads. Uses the perceptual intent, matching ImageScope's color management
        """

    import io
    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    print(f"       ...applying embedded color profile "
          f"'{ImageCms.getProfileDescription(profile).strip()}' to the pixels", flush=True)
    transform = ImageCms.buildTransform(profile, ImageCms.createProfile('sRGB'),
                                        'RGB', 'RGB',
                                        renderingIntent=ImageCms.Intent.PERCEPTUAL)
    strip = 4096
    for y0 in range(0, image.height, strip):
        y1 = min(y0 + strip, image.height)
        box = (0, y0, image.width, y1)
        image.paste(ImageCms.applyTransform(image.crop(box), transform), box)
    return image


def read_czi(slide_path, target_um, load_native_resolution=1):
    from CZI2OMEtif import read_czi as read
    return read(slide_path, target_um, load_native_resolution)


def save_ome_tif(image, pth, folder_name, image_name, pixelsize):
    """Exports an image in ome-tif format with metadata compatible with Qupath.

        Each ome-tif file will contain pyramidal copies saved at downsample factors of
        [1, 2, 4, 8, 16, 32] and tiled at a size of [1024 x 1024] for rapid loading
        """

    output_name = os.path.join(pth, folder_name, image_name + '.ome.tif')

    # some settings for the ome-tif file
    tile_size = 1024
    compression_quality = 95
    scale_factors = [int(1), int(2), int(4), int(8), int(16), int(32), int(64)]  # Define downsampling factors to save in each ome-tif

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
            extratags=[SRGB_EXTRATAG],
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

    #print(f"Saved: {output_name}")

def get_image_name(image_file):
    """Strip extension, properly handling .ome.tif / .ome.tiff as well as standard extensions."""
    for ext in ['.ome.tiff', '.ome.tif']:
        if image_file.lower().endswith(ext):
            return image_file[:-len(ext)]
    return image_file.rsplit('.', 1)[0]


def process_images(pth, output_names, image_list, umpix, save_ome, load_native_resolution=1, outpth=None):
    """Process missing images by converting .ndp, .ndpi, .svs, .vsi, .mrxs, .dcm, .czi, .tif, etc. to .tif or .ome.tif."""

    if outpth is None:
        outpth = pth

    if not isinstance(save_ome, list):
        save_ome = [save_ome] * (len(output_names) if isinstance(output_names, list) else 1)

    if not isinstance(umpix, list):
        umpix = [umpix]

    if not isinstance(output_names, list):
        output_names = [output_names]

    if not (len(output_names) == len(umpix) == len(save_ome)):
        raise ValueError('Output folders, resolutions and formats must have equal lengths.')
    if not all(np.isfinite(um) and um >= 0 for um in umpix):
        raise ValueError('Resolutions must be finite and nonnegative.')

    timing = TimingLog(outpth, "conversion")
    with timing.measure(phase="batch_total"):
        for idx, image_in_list in enumerate(image_list):
            with timing.measure(os.path.join(pth, image_in_list)) as image_timing:
                image_timing["detail"] = f"native={load_native_resolution}; folders={output_names}; requested_mpp={umpix}; ome={save_ome}"
                print(f"  Starting image {idx + 1} of {len(image_list)}: {image_in_list}...")

                # check if the image is already downsampled
                image_name = get_image_name(image_in_list)
                image_done = 1
                for folder_name, ome in zip(output_names, save_ome):
                    ft = '.ome.tif' if ome == 1 else '.tif'
                    output_name = os.path.join(outpth, folder_name, image_name + ft)
                    if not os.path.exists(output_name):
                        image_done = 0
                        break
                if image_done == 1:
                    image_timing["status"] = "skipped_existing"
                    print(f"    ...already saved this file")
                    continue

                # Read the image
                slide_path = os.path.join(pth, image_in_list)
                with timing.measure(slide_path, "read") as read_timing:
                    try:
                        # Get file extension
                        file_ext = os.path.splitext(slide_path)[-1].lower()
                        target_um = (0 if 0 in umpix else min(umpix))

                        if file_ext in ['.ndpi', '.ndp', '.svs', '.scn', '.mrxs', '.qptiff']:
                            print(f"    ...reading {slide_path} with OpenSlide")
                            if file_ext == '.mrxs':
                                companion_folder = os.path.join(pth, image_name)
                                if not os.path.isdir(companion_folder):
                                    print(f"       ...NOTE: companion folder '{image_name}' for MRXS file not found in {pth}")

                            wsi = OpenSlide(slide_path)
                            try:
                                mppx = float(wsi.properties['openslide.mpp-x'])
                                mppy = float(wsi.properties['openslide.mpp-y'])

                                raw_mppx, raw_mppy = mppx, mppy
                                mppx, mppy, calibration = calibrate_mpp(
                                    mppx, mppy, active_scanner(), slide_path)
                                if calibration != 1.0:
                                    print(
                                        f"       ...P1000 MPP calibration x{calibration:.9f}: "
                                        f"({raw_mppx:.6f}, {raw_mppy:.6f}) -> "
                                        f"({mppx:.6f}, {mppy:.6f}) um/px")
                                    image_timing['detail'] += (
                                        f"; P1000_mpp_multiplier={calibration:.12f}; "
                                        f"raw_mpp=({raw_mppx:.9f},{raw_mppy:.9f}); "
                                        f"calibrated_mpp=({mppx:.9f},{mppy:.9f})")

                                # Choose coarsest pyramid level finer than or equal to target_um
                                level = 0
                                if target_um > 0 and not load_native_resolution:
                                    for lvl, factor in enumerate(wsi.level_downsamples):
                                        if max(mppx, mppy) * factor <= target_um:
                                            level = lvl

                                w_lvl, h_lvl = wsi.level_dimensions[level]
                                downsample = wsi.level_downsamples[level]
                                if level > 0:
                                    print(f"       ...reading pyramid level {level} ({w_lvl}x{h_lvl}, downsample {downsample:.1f}x)")
                                else:
                                    print(f"       ...reading pyramid level 0 ({w_lvl}x{h_lvl})")

                                image0 = read_openslide_chunks(wsi, level)
                                w, h = w_lvl, h_lvl
                                mppx = mppx * downsample
                                mppy = mppy * downsample
                            finally:
                                wsi.close()

                            # Burn scanner color calibration into .svs pixels
                            if file_ext == '.svs' and APPLY_EMBEDDED_ICC:
                                icc = read_embedded_icc(slide_path)
                                if icc is not None:
                                    image0 = apply_embedded_icc(image0, icc)
                                else:
                                    print("       ...no embedded ICC profile found - keeping stored pixel values")

                        elif file_ext == '.vsi':
                            from vsi2ometif import read_vsi
                            image0, mppx, mppy = read_vsi(slide_path, target_um, load_native_resolution)
                            w, h = image0.size

                        elif file_ext == '.czi':
                            print(f"    ...reading Zeiss CZI {slide_path} with pylibCZIrw")
                            image0, mppx, mppy = read_czi(slide_path, target_um, load_native_resolution)
                            w, h = image0.size[:2]

                        elif file_ext == '.dcm':
                            print(f"    ...reading Pramana DICOM {slide_path}")
                            if wsidicom is not None:
                                wsi = wsidicom.WsiDicom.open(slide_path)
                                try:
                                    w0, h0 = wsi.size.width, wsi.size.height
                                    mpp0_x = float(wsi.mpp.width)
                                    mpp0_y = float(wsi.mpp.height)

                                    level_idx = 0
                                    if target_um > 0 and len(wsi.levels) > 1:
                                        for idx, lvl in enumerate(wsi.levels):
                                            if max(float(lvl.mpp.width), float(lvl.mpp.height)) <= target_um:
                                                level_idx = idx

                                    lvl = wsi.levels[level_idx]
                                    w_lvl, h_lvl = lvl.size.width, lvl.size.height
                                    mppx = float(lvl.mpp.width)
                                    mppy = float(lvl.mpp.height)
                                    print(f"       ...reading DICOM level {level_idx} ({w_lvl}x{h_lvl} at {mppx:.4f} um/px)")
                                    image0 = read_rgb_chunks((w_lvl, h_lvl),
                                        lambda xy, size: wsi.read_region(xy, lvl.level, size))
                                    w, h = w_lvl, h_lvl
                                finally:
                                    wsi.close()
                            else:
                                wsi = OpenSlide(slide_path)
                                try:
                                    mppx = float(wsi.properties['openslide.mpp-x'])
                                    mppy = float(wsi.properties['openslide.mpp-y'])
                                    level = 0
                                    if target_um > 0 and not load_native_resolution:
                                        for lvl, factor in enumerate(wsi.level_downsamples):
                                            if max(mppx, mppy) * factor <= target_um:
                                                level = lvl
                                    w_lvl, h_lvl = wsi.level_dimensions[level]
                                    downsample = wsi.level_downsamples[level]
                                    image0 = read_openslide_chunks(wsi, level)
                                    w, h = w_lvl, h_lvl
                                    mppx = mppx * downsample
                                    mppy = mppy * downsample
                                finally:
                                    wsi.close()

                        elif file_ext in ['.tif', '.tiff']:
                            is_wsi = False
                            wsi = None
                            try:
                                wsi = OpenSlide(slide_path)
                                vendor = wsi.properties.get('openslide.vendor', '').lower()
                                if vendor == 'ventana' or wsi.level_count > 1 or 'roche' in slide_path.lower() or 'ventana' in slide_path.lower():
                                    is_wsi = True
                            except Exception:
                                is_wsi = False

                            if is_wsi:
                                print(f"    ...reading Roche Ventana / WSI TIFF {slide_path} with OpenSlide")
                                try:
                                    if 'openslide.mpp-x' in wsi.properties:
                                        mppx = float(wsi.properties['openslide.mpp-x'])
                                        mppy = float(wsi.properties['openslide.mpp-y'])
                                    elif 'ventana.ScanRes' in wsi.properties:
                                        mppx = float(wsi.properties['ventana.ScanRes'])
                                        mppy = float(wsi.properties['ventana.ScanRes'])
                                    elif 'tiff.XResolution' in wsi.properties and wsi.properties.get('tiff.ResolutionUnit') == 'centimeter':
                                        mppx = 1e4 / float(wsi.properties['tiff.XResolution'])
                                        mppy = 1e4 / float(wsi.properties['tiff.YResolution'])
                                    else:
                                        mppx, mppy = read_tiff_mpp(slide_path)

                                    level = 0
                                    if target_um > 0 and not load_native_resolution:
                                        for lvl, factor in enumerate(wsi.level_downsamples):
                                            if max(mppx, mppy) * factor <= target_um:
                                                level = lvl

                                    w_lvl, h_lvl = wsi.level_dimensions[level]
                                    downsample = wsi.level_downsamples[level]
                                    print(f"       ...reading pyramid level {level} ({w_lvl}x{h_lvl}, downsample {downsample:.1f}x)")
                                    image0 = read_openslide_chunks(wsi, level)
                                    w, h = w_lvl, h_lvl
                                    mppx = mppx * downsample
                                    mppy = mppy * downsample
                                finally:
                                    wsi.close()

                                if APPLY_EMBEDDED_ICC:
                                    icc = read_embedded_icc(slide_path)
                                    if icc is not None:
                                        image0 = apply_embedded_icc(image0, icc)
                                    else:
                                        print("       ...no embedded ICC profile found - keeping stored pixel values")
                            else:
                                if wsi is not None:
                                    wsi.close()
                                print(f"    ...reading standard TIFF {slide_path} in tiles/strips")
                                image0 = read_tiff_chunks(slide_path)
                                w, h = image0.size[:2]
                                try:
                                    wsi = OpenSlide(slide_path)
                                    mppx, mppy = float(wsi.properties['openslide.mpp-x']), float(wsi.properties['openslide.mpp-y'])
                                    wsi.close()
                                except Exception:
                                    mppx, mppy = read_tiff_mpp(slide_path)

                        elif file_ext in ['.png', '.jpg']:
                            raise ValueError('Convert uncalibrated PNG/JPEG inputs to TIFF with known physical pixel spacing first.')

                        elif file_ext in ['.i2syntax', '.isyntax']:
                            print(f"    ...reading {slide_path} with pyisyntax")
                            isyntax_image = read_isyntax(slide_path, target_um, load_native_resolution)
                            if isyntax_image is None:
                                image_timing["status"] = "unsupported"
                                read_timing["status"] = "unsupported"
                                print(f"       ...SKIPPING {image_in_list}: libisyntax cannot decode this file")
                                continue
                            image0, mppx, mppy = isyntax_image
                            w, h = image0.size[:2]

                        else:
                            image_timing["status"] = "unsupported"
                            read_timing["status"] = "unsupported"
                            print(f"    ...unrecognized or unsupported file extension for: {slide_path}")
                            continue

                        validate_mpp(mppx, mppy)
                        read_timing["mpp"] = mppx
                        print(f"       ...image read successfully - file parameters: resolution of {mppx:.4f} um/px and size of ({w}, {h})")

                    except Exception as e:
                        print(f"       ...ERROR reading {image_in_list}: {e}")
                        raise

                # Save the image at each desired resolution
                for folder_name, um, ome in zip(output_names, umpix, save_ome):
                    with timing.measure(slide_path, "resolution_total", folder_name, um) as output_timing:
                        output_name = os.path.join(outpth, folder_name, image_name + '.tif')
                        if um == 0 or um < max(mppx, mppy):
                            um = max(mppx, mppy)

                        output_timing["mpp"] = um
                        # resize the image
                        factor_x, factor_y = um / mppx, um / mppy
                        resize_dimension = (int(np.ceil(w / factor_x)), int(np.ceil(h / factor_y)))
                        with timing.measure(slide_path, "resize", folder_name, um):
                            image = image0.resize(resize_dimension, resample=Image.NEAREST)
                        print(f"          ...saving {folder_name} image at a resolution of {um} um/px - resized to {resize_dimension}")

                        with timing.measure(slide_path, "save", folder_name, um):
                            # save the file as either a normal or an ome-tif
                            if ome == 1:
                                print("             ...saving as an ome tif")
                                save_ome_tif(image, outpth, folder_name, image_name, um)
                            else:
                                try:  # save as normal tif
                                    image.save(output_name, resolution=1e4 / um, resolution_unit=3, compression=None, icc_profile=SRGB_PROFILE)
                                except Exception as e:  # save as ome-tif
                                    save_ome_tif(image, outpth, folder_name, image_name, um)
                                    print(f"          ...error saving {image_in_list} as tif: {e}, try saving this image as an ome-tif")
                                    continue

                print("  Image save successful!")
                print("  ")


def WSI2tif(pth, output_names, umpix, save_ome, load_native_resolution=1, outpth=None):
    print('Making down-sampled images:')

    # Check if a single file was passed instead of a directory
    if os.path.isfile(pth):
        single_file = pth
        pth = os.path.dirname(single_file)
        image_list = [os.path.basename(single_file)]
        if outpth is None:
            outpth = pth
        for folder in output_names:
            pthim = os.path.join(outpth, folder)
            os.makedirs(pthim, exist_ok=True)
        process_images(pth, output_names, image_list, umpix, save_ome, load_native_resolution, outpth)
        return

    if outpth is None:
        outpth = pth

    for folder in output_names:
        pthim = os.path.join(outpth, f'{folder}')
        # Ensure the image directory exists
        if not os.path.isdir(pthim):
            os.makedirs(pthim, exist_ok=True)

    # Get the image names, sorted alphabetically
    # Supports:
    # 1. Hamamatsu: .ndp, .ndpi
    # 2. Leica: .svs
    # 3. Olympus: .vsi (+ companion folder)
    # 4. P1000 (3DHistech): .mrxs (+ companion folder)
    # 5. Pramana: .dcm
    # 6. Zeiss: .czi
    # 7. Roche Ventana: .tif, .tiff
    # 8. SCN, QPTIFF, iSyntax, etc.
    patterns = [
        '*.ndpi', '*.ndp',
        '*.svs',
        '*.vsi',
        '*.mrxs',
        '*.dcm',
        '*.czi',
        '*.tif', '*.tiff',
        '*.scn', '*.qptiff',
        '*.i2syntax', '*.isyntax',
        '*.png', '*.jpg'
    ]
    image_list = [
        os.path.basename(file) for pattern in patterns
        for file in [str(f) for f in __import__('pathlib').Path(pth).iterdir() if f.is_file() and f.name.lower().endswith(pattern[1:])]
        if os.path.isfile(file)
    ]
    image_list = sorted(list(set(image_list)))
    if not image_list:
        print(f"  No supported image files found in {pth}.")
        return

    # process the images
    process_images(pth, output_names, image_list, umpix, save_ome, load_native_resolution, outpth)


if __name__ == '__main__':
    raise SystemExit('Use run_conversion.py --help for explicit input and output paths.')
