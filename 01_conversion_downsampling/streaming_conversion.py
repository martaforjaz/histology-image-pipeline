"""Streaming whole-slide conversion with the same output as WSI2OMEtif_All_file_types.

The slide is decoded in horizontal bands of the selected level (in parallel threads).
DICOM, OpenSlide formats (.ndpi, .ndp, .svs, .scn, .mrxs, .qptiff) and whole-slide
TIFFs are streamed, so they are never held in memory whole. .vsi, .czi, iSyntax and
plain TIFF files are read by the original readers (whole image in memory) and then
written the same way. Each band is resampled into every requested output at once and
handed to a writer:

- OME-TIFF outputs are written tile by tile from a background thread while the next
  band is decoded, and their pyramid levels are built from the same bands.
- Plain TIFF outputs are written into an uncompressed memory-mapped TIFF.

Every pixel matches WSI2OMEtif_All_file_types exactly. Outputs use the same Pillow
NEAREST mapping. Pyramid levels reproduce Pillow's LANCZOS resize of the full image,
which is two passes: a horizontal pass (rows are independent, so Pillow itself runs on
row bands) followed by a vertical pass in 22-bit fixed point, reimplemented here with
Pillow's coefficients and rounding so it can run band by band.

Files are written under a temporary name and renamed when complete, so an interrupted
run is never mistaken for a finished image.
"""
import math
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import tifffile
from PIL import Image

from WSI2OMEtif_All_file_types import SRGB_EXTRATAG
from image_metadata import validate_mpp
from pipeline_timing import active_scanner
from scanner_calibration import calibrate_mpp, is_p1000, P1000_MPP_MULTIPLIER

Image.MAX_IMAGE_PIXELS = None
OPENSLIDE_TYPES = ('.ndpi', '.ndp', '.svs', '.scn', '.mrxs', '.qptiff')
SLIDE_TYPES = OPENSLIDE_TYPES + ('.dcm', '.tif', '.tiff', '.vsi', '.czi', '.isyntax',
                                 '.i2syntax', '.png', '.jpg')  # as listed by WSI2tif
TILE = 1024
JPEG_QUALITY = 95
PYRAMID_LEVELS = 6  # downsamples 2..64, as in save_ome_tif
PRECISION_BITS = 22  # Pillow's fixed-point precision for 8-bit resampling
CHUNK_ROWS = 64     # rows per parallel task when gathering and in the horizontal pass
VERTICAL_ROWS = 16  # output rows per vertical-pass matrix; small keeps it mostly nonzero
PYRAMID_RAM_LIMIT = 2 * 1024**3  # larger pyramids are kept in local temporary files


def nearest_index(n_in, n_out):
    """Source index of each output pixel for Pillow's NEAREST resize.

        Pillow (ImagingScaleAffine) starts at 0.5 * scale and adds scale once per
        pixel, so rounding accumulates; on gigapixel images this differs from
        (x + 0.5) * scale. The sequential float64 accumulation reproduces it exactly
        """
    scale = n_in / n_out
    steps = np.full(n_out, scale)
    steps[0] = 0.0 + scale * 0.5
    idx = np.add.accumulate(steps).astype(np.int64)  # truncation, as Pillow's COORD
    return np.minimum(idx, n_in - 1)


def _sinc(x):
    if x == 0.0:
        return 1.0
    x = x * math.pi
    return math.sin(x) / x


def _lanczos(x):
    if -3.0 <= x < 3.0:
        return _sinc(x) * _sinc(x / 3.0)
    return 0.0


def lanczos_coefficients(in_size, out_size):
    """Pillow's precompute_coeffs + normalize_coeffs_8bpc for LANCZOS (Resample.c).

        Returns the first source index of each output pixel, the number of source
        pixels its kernel spans, and the fixed-point weights zero-padded to the kernel
        size
        """
    scale = in_size / out_size
    filterscale = max(scale, 1.0)
    support = 3.0 * filterscale
    ksize = int(math.ceil(support)) * 2 + 1
    ss = 1.0 / filterscale
    first = np.empty(out_size, np.int64)
    count = np.empty(out_size, np.int64)
    weights = np.zeros((out_size, ksize), np.int32)
    for xx in range(out_size):
        center = (xx + 0.5) * scale
        xmin = max(int(center - support + 0.5), 0)
        xmax = min(int(center + support + 0.5), in_size) - xmin
        k = [_lanczos((x + xmin - center + 0.5) * ss) for x in range(xmax)]
        total = 0.0
        for w in k:
            total += w
        for x, w in enumerate(k):
            if total != 0.0:
                w /= total
            weights[xx, x] = int(-0.5 + w * (1 << PRECISION_BITS)) if w < 0 \
                else int(0.5 + w * (1 << PRECISION_BITS))
        first[xx], count[xx] = xmin, xmax
    return first, count, weights


class _PyramidLevel:
    """One LANCZOS pyramid level of the full image, built band by band."""

    def __init__(self, scale, width, height, store):
        self.width, self.height = width // scale, height // scale
        self.first, self.count, self.weights = lanczos_coefficients(height, self.height)
        self.last = np.maximum.accumulate(self.first + self.count)  # source rows needed up to
        self.store = store
        self.rows = np.empty((0, self.width, 3), np.uint8)  # horizontal-pass rows kept
        self.rows_start = 0                                   # source row of rows[0]
        self.done = 0                                         # output rows written

    def horizontal(self, band):
        image = Image.fromarray(band).resize((self.width, len(band)), Image.Resampling.LANCZOS)
        return np.asarray(image)

    def vertical(self, start, stop):
        """Pillow's ResampleVertical_8bpc for output rows start..stop, as a matrix product.

            Uses the full-image coefficients, so band boundaries cannot change the
            result. The weighted sums are integers below 2**53, so float64 computes
            them exactly whatever the summation order
            """
        lo = self.first[start] - self.rows_start
        hi = self.last[stop - 1] - self.rows_start
        taps = self.first[start:stop, None] - self.rows_start - lo + np.arange(self.weights.shape[1])
        used = np.arange(self.weights.shape[1]) < self.count[start:stop, None]
        matrix = np.zeros((stop - start, hi - lo))
        matrix[np.nonzero(used)[0], taps[used]] = self.weights[start:stop][used]
        window = self.rows[lo:hi].reshape(hi - lo, -1).astype(np.float64)
        acc = (matrix @ window).astype(np.int64) + (1 << (PRECISION_BITS - 1))
        out = np.clip(acc >> PRECISION_BITS, 0, 255).astype(np.uint8)
        self.store[start:stop] = out.reshape(stop - start, self.width, 3)

    def ready(self, available):
        """Output rows whose source rows are all available."""
        return int(np.searchsorted(self.last, available, side='right'))


class _PlainTifSink:
    """Uncompressed TIFF written through a memory map, one band at a time."""

    def __init__(self, path, width, height, um):
        self.path, self.tmp = path, path + '.part'
        self.data = tifffile.memmap(
            self.tmp, shape=(height, width, 3), dtype=np.uint8, photometric='rgb',
            resolution=(1e4 / um, 1e4 / um), resolutionunit=3,
            extratags=[SRGB_EXTRATAG], bigtiff=height * width * 3 > 2**31)
        self.row = 0

    def push(self, rows):
        self.data[self.row:self.row + len(rows)] = rows
        self.row += len(rows)

    def close(self):
        self.data.flush()
        del self.data
        os.replace(self.tmp, self.path)

    def abort(self):
        del self.data
        os.remove(self.tmp)


class _OmeSink:
    """Tiled, pyramidal OME-TIFF written from a background thread."""

    def __init__(self, path, width, height, um, pool, pyramid_dir=None):
        self.path, self.tmp = path, path + '.part'
        self.width, self.height, self.um = width, height, um
        self.pool = pool
        self.queue = queue.Queue(maxsize=4)
        self.error = None
        self.aborted = self.ended = False

        # Pillow resizes images more than 100x taller than wide vertical-first, which
        # band streaming cannot reproduce; such images (never whole slides) are
        # collected and written by the original save_ome_tif instead.
        self.whole = [] if height > width * 100 else None
        if self.whole is not None:
            return

        # Sub-resolution levels must be complete before they can be written, so
        # they are accumulated while level 0 streams to disk: in RAM when small,
        # otherwise in temporary files on the local disk
        shapes = [(height // 2**k, width // 2**k, 3) for k in range(1, PYRAMID_LEVELS + 1)]
        shapes = [s for s in shapes if min(s[:2]) >= 1]
        if pyramid_dir is None and sum(math.prod(s) for s in shapes) > PYRAMID_RAM_LIMIT:
            import tempfile
            pyramid_dir = tempfile.gettempdir()
        self.levels = []
        for k, shape in enumerate(shapes, start=1):
            if pyramid_dir:
                store = np.lib.format.open_memmap(
                    os.path.join(pyramid_dir, f'{os.getpid()}_{os.path.basename(path)}.level{k}.npy'),
                    mode='w+', dtype=np.uint8, shape=shape)
            else:
                store = np.empty(shape, np.uint8)
            self.levels.append(_PyramidLevel(2**k, width, height, store))
        self.received = 0
        self.thumbnail = np.empty((math.ceil(height / 8), math.ceil(width / 8), 3), np.uint8)
        self.thumb_row = 0

        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def push(self, rows):
        if self.whole is not None:
            self.whole.append(rows)
            return
        if self.error:
            raise self.error
        self.queue.put(rows)

    def close(self):
        if self.whole is not None:
            from WSI2OMEtif_All_file_types import save_ome_tif
            folder = os.path.dirname(self.path)
            stem = os.path.basename(self.path)[:-len('.ome.tif')] + '.part'
            save_ome_tif(Image.fromarray(np.concatenate(self.whole)), folder, '', stem, self.um)
            os.replace(os.path.join(folder, stem + '.ome.tif'), self.path)
            return
        self.queue.put(None)
        self.thread.join()
        self._release()
        if self.error:
            raise self.error
        os.replace(self.tmp, self.path)

    def abort(self):
        """Stops writing and deletes the unfinished file."""
        if self.whole is None:
            self.aborted = True
            self.queue.put(None)
            self.thread.join()
            self._release()
        if os.path.exists(self.tmp):
            os.remove(self.tmp)

    def _release(self):
        names = [level.store.filename for level in self.levels
                 if isinstance(level.store, np.memmap)]
        self.levels = []
        for name in names:
            os.remove(name)

    def _bands(self):
        """Regroup incoming rows into bands of TILE rows."""
        pending, count = [], 0
        while True:
            rows = self.queue.get()
            if rows is None:
                self.ended = True
                if self.aborted:
                    raise RuntimeError('conversion aborted')
                break
            pending.append(rows)
            count += len(rows)
            while count >= TILE:
                band = np.concatenate(pending) if len(pending) > 1 else pending[0]
                yield band[:TILE]
                pending, count = [band[TILE:]], count - TILE
        if count:
            yield np.concatenate(pending)

    def _tiles(self):
        for band in self._bands():
            self._reduce(band)
            for x in range(0, self.width, TILE):
                yield band[:, x:x + TILE]

    def _reduce(self, band):
        thumb = band[::8, ::8]
        self.thumbnail[self.thumb_row:self.thumb_row + len(thumb)] = thumb
        self.thumb_row += len(thumb)
        self.received += len(band)
        chunks = range(0, len(band), CHUNK_ROWS)

        # Horizontal pass: rows are independent, so Pillow runs on row chunks in parallel
        jobs = [(level, self.pool.submit(level.horizontal, band[r:r + CHUNK_ROWS]))
                for level in self.levels for r in chunks]
        parts = {id(level): [] for level in self.levels}
        for level, job in jobs:
            parts[id(level)].append(job.result())

        # Vertical pass for every output row whose source rows have all arrived
        jobs, stops = [], []
        for level in self.levels:
            level.rows = np.concatenate([level.rows, *parts[id(level)]])
            stop = level.ready(self.received)
            for start in range(level.done, stop, VERTICAL_ROWS):
                jobs.append(self.pool.submit(level.vertical, start, min(start + VERTICAL_ROWS, stop)))
            stops.append(stop)
        for job in jobs:
            job.result()
        for level, stop in zip(self.levels, stops):
            level.done = stop
            if level.done < level.height:  # drop rows no remaining output needs
                keep = level.first[level.done] - level.rows_start
                level.rows = level.rows[keep:]
                level.rows_start += keep

    def _write(self):
        try:
            options = dict(photometric='rgb', tile=(TILE, TILE), compression='jpeg',
                           compressionargs={'level': JPEG_QUALITY}, resolutionunit=3)
            metadata = {'axes': 'YXC', 'SignificantBits': 8,
                        'PhysicalSizeX': self.um, 'PhysicalSizeXUnit': 'µm',
                        'PhysicalSizeY': self.um, 'PhysicalSizeYUnit': 'µm',
                        'Software': 'tifffile'}
            with tifffile.TiffWriter(self.tmp, bigtiff=True, ome=True) as tif:
                tif.write(self._tiles(), shape=(self.height, self.width, 3), dtype=np.uint8,
                          subifds=len(self.levels), resolution=(1e4 / self.um, 1e4 / self.um),
                          metadata=metadata, extratags=[SRGB_EXTRATAG], **options)
                for k, level in enumerate(self.levels, start=1):
                    res = 1e4 / (2**k) / self.um
                    tif.write(level.store, subfiletype=1, resolution=(res, res), **options)
                tif.write(self.thumbnail, metadata={'Name': 'thumbnail'})
        except BaseException as exc:
            self.error = exc
            while not self.ended:  # unblock the producer until it signals the end
                self.ended = self.queue.get() is None


class _Output:
    """One requested resolution: maps level-0 rows and columns to output pixels."""

    def __init__(self, sink, width0, height0, width, height, pool=None):
        self.sink, self.pool = sink, pool
        self.rows = nearest_index(height0, height)
        self.cols = None if width == width0 else nearest_index(width0, width)
        self.width, self.done = width, 0

    def push(self, band, y0):
        stop = int(np.searchsorted(self.rows, y0 + len(band), side='left'))
        if stop == self.done:
            return
        rows = self.rows[self.done:stop] - y0
        if self.cols is None:
            self.sink.push(band[rows])
        else:
            out = np.empty((len(rows), self.width, 3), np.uint8)

            def gather(i):
                out[i:i + CHUNK_ROWS] = band[rows[i:i + CHUNK_ROWS, None], self.cols]

            chunks = range(0, len(rows), CHUNK_ROWS)
            list(self.pool.map(gather, chunks) if self.pool else map(gather, chunks))
            self.sink.push(out)
        self.done = stop


class UnsupportedSlide(Exception):
    """The original converter would also skip this file as unsupported."""


class _DicomSource:
    """DICOM slide (Pramana); level chosen as in WSI2OMEtif_All_file_types."""
    row_step = 1

    def __init__(self, path, target_um, native, threads):
        import wsidicom  # optional backend; only needed to read DICOM
        self.wsi, self.threads = wsidicom.WsiDicom.open(path), threads
        index = 0
        if target_um > 0 and len(self.wsi.levels) > 1:
            for i, level in enumerate(self.wsi.levels):
                if max(float(level.mpp.width), float(level.mpp.height)) <= target_um:
                    index = i
        self.level = self.wsi.levels[index]
        self.width, self.height = self.level.size.width, self.level.size.height
        self.mppx, self.mppy = float(self.level.mpp.width), float(self.level.mpp.height)

    def read(self, y, rows, pool):
        return self.wsi.read_region((0, y), self.level.level, (self.width, rows),
                                    threads=self.threads, as_array=True)

    def close(self):
        self.wsi.close()


class _OpenSlideSource:
    """OpenSlide formats and whole-slide TIFFs, read in column chunks in parallel.

        Level selection, pixel size, level-0 read coordinates and the embedded ICC
        conversion follow WSI2OMEtif_All_file_types and chunked_readers exactly
        """
    CHUNK = 4096

    def __init__(self, path, target_um, native, mpp=None, apply_icc=False):
        from fractions import Fraction
        from openslide import OpenSlide
        self.slide = OpenSlide(path)
        props = self.slide.properties
        mppx, mppy = mpp or (float(props['openslide.mpp-x']), float(props['openslide.mpp-y']))
        raw_mppx, raw_mppy = mppx, mppy
        mppx, mppy, calibration = calibrate_mpp(
            mppx, mppy, active_scanner(), path)
        if calibration != 1.0:
            print(f'P1000 MPP calibration x{calibration:.9f}: '
                  f'({raw_mppx:.6f}, {raw_mppy:.6f}) -> '
                  f'({mppx:.6f}, {mppy:.6f}) um/px', flush=True)
        level = 0
        if target_um > 0 and not native:
            for lvl, factor in enumerate(self.slide.level_downsamples):
                if max(mppx, mppy) * factor <= target_um:
                    level = lvl
        self.level, self.downsample = level, self.slide.level_downsamples[level]
        self.width, self.height = self.slide.level_dimensions[level]
        self.mppx, self.mppy = mppx * self.downsample, mppy * self.downsample
        # Keep chunk edges on integral level-0 coordinates, as read_openslide_chunks does
        step = Fraction(float(self.downsample)).limit_denominator(self.CHUNK).denominator
        self.row_step, self.chunk = step, max(step, self.CHUNK // step * step)
        self.transform = None
        if apply_icc:
            from WSI2OMEtif_All_file_types import APPLY_EMBEDDED_ICC, read_embedded_icc
            icc = read_embedded_icc(path) if APPLY_EMBEDDED_ICC else None
            if icc is not None:  # same transform as apply_embedded_icc
                import io
                from PIL import ImageCms
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                self.transform = ImageCms.buildTransform(
                    profile, ImageCms.createProfile('sRGB'), 'RGB', 'RGB',
                    renderingIntent=ImageCms.Intent.PERCEPTUAL)

    def read(self, y, rows, pool):
        band = np.empty((rows, self.width, 3), np.uint8)

        def part(x):
            width = min(self.chunk, self.width - x)
            location = (round(x * self.downsample), round(y * self.downsample))
            region = self.slide.read_region(location, self.level, (width, rows))
            band[:, x:x + width] = np.asarray(region.convert('RGB'))

        list(pool.map(part, range(0, self.width, self.chunk)))
        if self.transform is not None:  # per-pixel, so band-wise equals whole-image
            from PIL import ImageCms
            band = np.asarray(ImageCms.applyTransform(Image.fromarray(band), self.transform))
        return band

    def close(self):
        self.slide.close()


class _ImageSource:
    """An image the original reader already loaded whole (vsi, czi, iSyntax, plain TIFF)."""
    row_step = 1

    def __init__(self, image, mppx, mppy):
        self.image, self.mppx, self.mppy = image, mppx, mppy
        self.width, self.height = image.size

    def read(self, y, rows, pool):
        return np.asarray(self.image.crop((0, y, self.width, y + rows)).convert('RGB'))

    def close(self):
        self.image = None


def open_source(slide_path, umpix, load_native_resolution=1, threads=4):
    """Opens a slide exactly as WSI2OMEtif_All_file_types.process_images reads it."""
    import WSI2OMEtif_All_file_types as original
    ext = os.path.splitext(slide_path)[1].lower()
    target_um = 0 if 0 in umpix else min(umpix)
    native = load_native_resolution
    if ext == '.dcm':
        if original.wsidicom is None:  # the original falls back to OpenSlide
            return _OpenSlideSource(slide_path, target_um, native)
        return _DicomSource(slide_path, target_um, native, threads)
    if ext in OPENSLIDE_TYPES:
        return _OpenSlideSource(slide_path, target_um, native, apply_icc=ext == '.svs')
    if ext in ('.tif', '.tiff'):
        from openslide import OpenSlide
        try:
            with OpenSlide(slide_path) as wsi:
                props = dict(wsi.properties)
                is_wsi = (props.get('openslide.vendor', '').lower() == 'ventana' or wsi.level_count > 1
                          or 'roche' in slide_path.lower() or 'ventana' in slide_path.lower())
        except Exception:
            is_wsi = False
        if is_wsi:
            if 'openslide.mpp-x' in props:
                mpp = float(props['openslide.mpp-x']), float(props['openslide.mpp-y'])
            elif 'ventana.ScanRes' in props:
                mpp = (float(props['ventana.ScanRes']),) * 2
            elif 'tiff.XResolution' in props and props.get('tiff.ResolutionUnit') == 'centimeter':
                mpp = 1e4 / float(props['tiff.XResolution']), 1e4 / float(props['tiff.YResolution'])
            else:
                mpp = original.read_tiff_mpp(slide_path)
            return _OpenSlideSource(slide_path, target_um, native, mpp, apply_icc=True)
        image = original.read_tiff_chunks(slide_path)
        try:
            with OpenSlide(slide_path) as wsi:
                mpp = float(wsi.properties['openslide.mpp-x']), float(wsi.properties['openslide.mpp-y'])
        except Exception:
            mpp = original.read_tiff_mpp(slide_path)
        return _ImageSource(image, *mpp)
    if ext == '.vsi':
        from vsi2ometif import read_vsi
        return _ImageSource(*read_vsi(slide_path, target_um, native))
    if ext == '.czi':
        return _ImageSource(*original.read_czi(slide_path, target_um, native))
    if ext in ('.isyntax', '.i2syntax'):
        result = original.read_isyntax(slide_path, target_um, native)
        if result is None:
            raise UnsupportedSlide('libisyntax cannot decode this file')
        return _ImageSource(*result)
    if ext in ('.png', '.jpg'):
        raise ValueError('Convert uncalibrated PNG/JPEG inputs to TIFF with known physical pixel spacing first.')
    raise UnsupportedSlide(f'unrecognized or unsupported file extension: {ext}')


def convert_slide(slide_path, outpth, folders, umpix, save_ome, threads=None,
                  load_native_resolution=1, band_rows=2048, pyramid_dir=None, verbose=True):
    """Converts one slide to every requested resolution in a single streamed pass.

        Outputs that already exist are skipped. Returns the list of written paths.
        """
    threads = threads or os.cpu_count()
    image_name = os.path.splitext(os.path.basename(slide_path))[0]
    names = [image_name + ('.ome.tif' if ome else '.tif') for ome in save_ome]
    if all(os.path.exists(os.path.join(outpth, f, n)) for f, n in zip(folders, names)):
        return []  # checked before opening, so finished slides cost no read

    with ThreadPoolExecutor(threads) as pool:
        source = open_source(slide_path, umpix, load_native_resolution, threads)
        try:
            return _stream(source, pool, slide_path, outpth, folders, umpix, save_ome,
                           band_rows, pyramid_dir, verbose)
        finally:
            source.close()


def _stream(source, pool, slide_path, outpth, folders, umpix, save_ome, band_rows,
            pyramid_dir, verbose):
    image_name = os.path.splitext(os.path.basename(slide_path))[0]
    written = []
    width0, height0 = source.width, source.height
    mppx, mppy = source.mppx, source.mppy
    validate_mpp(mppx, mppy)
    band_rows = max(source.row_step, band_rows // source.row_step * source.row_step)

    outputs = []
    for folder, um, ome in zip(folders, umpix, save_ome):
        path = os.path.join(outpth, folder, image_name + ('.ome.tif' if ome else '.tif'))
        if os.path.exists(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if um == 0 or um < max(mppx, mppy):
            um = max(mppx, mppy)
        width = int(np.ceil(width0 / (um / mppx)))
        height = int(np.ceil(height0 / (um / mppy)))
        sink = (_OmeSink(path, width, height, um, pool, pyramid_dir) if ome
                else _PlainTifSink(path, width, height, um))
        outputs.append(_Output(sink, width0, height0, width, height, pool))
        written.append(path)
        if verbose:
            print(f'    {folder}: {um} um/px -> {width}x{height}', flush=True)
    if not outputs:
        return written

    try:
        with ThreadPoolExecutor(1) as prefetch:  # decode the next band while this one is used
            def read(y):
                return source.read(y, min(band_rows, height0 - y), pool)

            pending = prefetch.submit(read, 0)
            for y in range(0, height0, band_rows):
                band = pending.result()
                if y + band_rows < height0:
                    pending = prefetch.submit(read, y + band_rows)
                for output in outputs:
                    output.push(band, y)
    except BaseException:
        for output in outputs:  # never leave a partial file under the final name
            output.sink.abort()
        raise
    errors = []
    for output in outputs:
        try:
            output.sink.close()
        except BaseException as exc:
            errors.append(exc)
            if os.path.exists(output.sink.tmp):
                os.remove(output.sink.tmp)
    if errors:
        raise errors[0]
    return written


_worker_log = None  # one timing CSV per worker process


def _convert_one(job):
    """Worker-process entry: converts one slide and records its time like WSI2tif."""
    global _worker_log
    (slide, outpth, folders, umpix, save_ome, threads, native, pyramid_dir, scanner,
     manifest, workers) = job
    from pipeline_timing import TimingLog, timing_settings
    if _worker_log is None:
        with timing_settings(scanner, manifest):
            _worker_log = TimingLog(outpth, 'conversion')
    start = time.perf_counter()
    status = 'ok'
    with _worker_log.measure(slide) as row:
        # Slides run concurrently, so per-image times depend on the number of workers
        row['detail'] = (f'implementation=streaming; workers={workers}; threads={threads}; '
                         f'native={native}; folders={folders}; requested_mpp={umpix}; '
                         f'ome={save_ome}')
        if is_p1000(scanner, slide):
            row['detail'] += f'; P1000_mpp_multiplier={P1000_MPP_MULTIPLIER:.12f}'
        try:
            written = convert_slide(slide, outpth, folders, umpix, save_ome, threads, native,
                                    pyramid_dir=pyramid_dir, verbose=False)
            status = 'ok' if written else 'skipped_existing'
        except UnsupportedSlide as exc:
            status = 'unsupported'
            row['detail'] += f'; {exc}'
        row['status'] = status
    return os.path.basename(slide), time.perf_counter() - start, status


def convert_folder(pth, outpth, folders, umpix, save_ome, workers=4, threads=4,
                   scanner='unknown', scanner_manifest=None, load_native_resolution=1,
                   pyramid_dir=None):
    """Converts every slide in pth (or one slide file), several at a time.

        Each worker process converts one slide with `threads` threads. Streamed
        formats (DICOM, OpenSlide, whole-slide TIFF) need roughly 2-4 GB plus the
        pyramid levels of the slide's largest output; .vsi, .czi, iSyntax and plain
        TIFF also hold the decoded slide. A slide that fails is reported at the end;
        the others still run
        """
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from pipeline_timing import TimingLog, timing_settings

    if not folders or not (len(folders) == len(umpix) == len(save_ome)):
        raise ValueError('folder_names, pixel_resolutions and save_ome must have equal nonzero lengths.')
    if len({folder.casefold() for folder in folders}) != len(folders):
        raise ValueError('Output subfolder names must be unique.')
    if os.path.isfile(pth):
        slides = [pth]
    else:
        slides = sorted(os.path.join(pth, f) for f in os.listdir(pth)
                        if f.lower().endswith(SLIDE_TYPES) and os.path.isfile(os.path.join(pth, f)))
    if not slides:
        raise ValueError(f'No supported slides found in {pth}')

    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')  # parallelism comes from workers
    jobs = [(s, outpth, folders, umpix, save_ome, threads, load_native_resolution,
             pyramid_dir, scanner, scanner_manifest, workers) for s in slides]
    with timing_settings(scanner, scanner_manifest):
        batch_log = TimingLog(outpth, 'conversion')
    failed = []
    with batch_log.measure(phase='batch_total') as batch, ProcessPoolExecutor(workers) as pool:
        batch['detail'] = f'implementation=streaming; workers={workers}; threads={threads}'
        futures = {pool.submit(_convert_one, job): job[0] for job in jobs}
        for n, future in enumerate(as_completed(futures), start=1):
            name = os.path.basename(futures[future])
            try:
                _, seconds, status = future.result()
                note = {'ok': f'{seconds:.0f} s', 'skipped_existing': 'already saved'}.get(
                    status, 'SKIPPED: unsupported')
            except Exception as exc:
                failed.append(name)
                note = f'ERROR {type(exc).__name__}: {exc}'
            print(f'  {n}/{len(jobs)} {name}: {note}', flush=True)
    if failed:
        raise RuntimeError(f'{len(failed)} slide(s) failed: {failed}')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('input', help='A slide or a folder of slides')
    p.add_argument('--output', required=True)
    p.add_argument('--folder', nargs='+', required=True)
    p.add_argument('--mpp', nargs='+', type=float, required=True)
    p.add_argument('--save-ome', nargs='+', type=int, required=True)
    p.add_argument('--workers', type=int, default=4, help='Slides converted at once')
    p.add_argument('--threads', type=int, default=4, help='Threads per slide')
    p.add_argument('--scanner', default='unknown')
    p.add_argument('--scanner-manifest')
    p.add_argument('--fast-pyramid', action='store_true',
                   help='Read a coarser source pyramid level when possible (as run_conversion.py)')
    args = p.parse_args()
    convert_folder(args.input, args.output, args.folder, args.mpp, args.save_ome,
                   args.workers, args.threads, args.scanner, args.scanner_manifest,
                   int(not args.fast_pyramid))
