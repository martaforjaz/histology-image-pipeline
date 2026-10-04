"""The streaming converter must write the same pixels as WSI2OMEtif_All_file_types."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'01_conversion_downsampling'))
import numpy as np
import tifffile
from PIL import Image
import streaming_conversion as sc
from WSI2OMEtif_All_file_types import save_ome_tif


def tissue_like(height, width, seed=0):
    """Smooth color gradients plus noise, so JPEG and LANCZOS see realistic detail."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:height, 0:width]
    base = np.stack([128 + 100*np.sin(x/37.0 + c) * np.cos(y/53.0 - c) for c in range(3)], -1)
    return np.clip(base + rng.normal(0, 25, base.shape), 0, 255).astype(np.uint8)


def pages(path):
    """Decoded level 0, every SubIFD level and the thumbnail of an OME-TIFF."""
    with tifffile.TiffFile(path) as tif:
        out = [level.asarray() for level in tif.series[0].levels]
        out.append(tif.pages[1].asarray())  # thumbnail
        return out


class _Collect:
    def __init__(self):
        self.parts = []

    def push(self, rows):
        self.parts.append(rows)


class StreamingConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nearest_resampling_matches_pillow(self):
        image = tissue_like(777, 1301)
        for height, width in [(715, 1197), (37, 61), (777, 1301), (36, 1301)]:
            sink = _Collect()
            output = sc._Output(sink, 1301, 777, width, height)
            for y in range(0, 777, 100):  # bands like the DICOM reader produces
                output.push(image[y:y+100], y)
            expected = np.asarray(Image.fromarray(image).resize((width, height), Image.NEAREST))
            np.testing.assert_array_equal(np.concatenate(sink.parts), expected)

    def test_nearest_index_matches_pillow_at_slide_size(self):
        # Pillow accumulates the step in floating point, which drifts from
        # (x + 0.5) * scale only on gigapixel-sized axes like these
        sizes = [(48384, 0.238), (37632, 0.238), (143872, 0.23), (133376, 0.238), (102400, 0.238)]
        for n_in, mpp in sizes:
            for um in (0.25, 5):
                n_out = int(np.ceil(n_in / (um / mpp)))
                ramp = (np.arange(n_in) % 251).astype(np.uint8)  # distinct neighbours
                line = np.stack([ramp, ramp // 3, ramp // 7], -1)[None]
                expected = np.asarray(Image.fromarray(line).resize((n_out, 1), Image.NEAREST))
                np.testing.assert_array_equal(line[0, sc.nearest_index(n_in, n_out)], expected[0],
                                              err_msg=f'{n_in} -> {n_out}')

    def test_lanczos_vertical_pass_matches_pillow(self):
        image = tissue_like(2101, 333, seed=1)
        for scale in (2, 4, 8, 16, 32, 64):
            first, _, weights = sc.lanczos_coefficients(2101, 2101 // scale)
            acc = np.full((2101 // scale, 333, 3), 1 << 21, np.int64)
            for t in range(weights.shape[1]):
                acc += image[np.minimum(first + t, 2100)].astype(np.int64) * weights[:, t, None, None]
            ours = np.clip(acc >> 22, 0, 255).astype(np.uint8)
            expected = np.asarray(Image.fromarray(image).resize((333, 2101 // scale), Image.LANCZOS))
            np.testing.assert_array_equal(ours, expected, err_msg=f'scale {scale}')

    def assert_streamed_matches_save_ome_tif(self, height, width, chunk):
        image = tissue_like(height, width, seed=height)
        (self.folder/'old').mkdir(exist_ok=True)
        save_ome_tif(Image.fromarray(image), str(self.folder), 'old', f's{height}', 0.25)
        new = self.folder/f'new{height}.ome.tif'
        with ThreadPoolExecutor(8) as pool:
            sink = sc._OmeSink(str(new), width, height, 0.25, pool)
            for y in range(0, height, chunk):
                sink.push(image[y:y+chunk])
            sink.close()
        old_pages = pages(self.folder/'old'/f's{height}.ome.tif')
        new_pages = pages(new)
        self.assertEqual(len(old_pages), len(new_pages))
        for level, (a, b) in enumerate(zip(old_pages, new_pages)):
            np.testing.assert_array_equal(a, b, err_msg=f'{height}x{width} page {level}')

    def test_streamed_ome_tif_is_pixel_identical_to_save_ome_tif(self):
        for height, width, chunk in [(2600, 3335, 700), (1100, 2049, 1024), (517, 129, 33)]:
            self.assert_streamed_matches_save_ome_tif(height, width, chunk)

    def test_streamed_ome_tif_identical_at_slide_height(self):
        # A real Pramana height, just under Pillow's 100:1 vertical-first threshold
        self.assert_streamed_matches_save_ome_tif(46062, 46062 // 99, 2048)

    def test_very_tall_image_falls_back_to_save_ome_tif(self):
        # Over 100:1 Pillow switches pass order; the sink must still match exactly
        self.assert_streamed_matches_save_ome_tif(46062, 130, 2048)

    def assert_slide_matches_wsi2tif(self, slide, native=1):
        from WSI2OMEtif_All_file_types import WSI2tif
        args = (['2x', '40x'], [5, 0.25], [0, 1])
        WSI2tif(str(slide), *args, native, outpth=str(self.folder/'old'))
        written = sc.convert_slide(str(slide), str(self.folder/'new'), *args, threads=4,
                                   load_native_resolution=native, verbose=False)
        self.assertEqual(len(written), 2)
        plain = slide.stem + '.tif'
        np.testing.assert_array_equal(tifffile.imread(self.folder/'old'/'2x'/plain),
                                      tifffile.imread(self.folder/'new'/'2x'/plain),
                                      err_msg=f'{slide.name} 2x')
        old = pages(self.folder/'old'/'40x'/(slide.stem + '.ome.tif'))
        new = pages(self.folder/'new'/'40x'/(slide.stem + '.ome.tif'))
        self.assertEqual(len(old), len(new))
        for level, (a, b) in enumerate(zip(old, new)):
            np.testing.assert_array_equal(a, b, err_msg=f'{slide.name} 40x page {level}')

    def test_plain_tiff_matches_wsi2tif(self):
        # Read whole by the original TIFF reader, then streamed to the writers
        slide = self.folder/'plain.tif'
        tifffile.imwrite(slide, tissue_like(2300, 1900, seed=6), photometric='rgb',
                         resolution=(1e4 / 0.23, 1e4 / 0.23), resolutionunit='CENTIMETER')
        self.assert_slide_matches_wsi2tif(slide)

    def test_pyramidal_tiff_with_icc_matches_wsi2tif(self):
        # Multi-level tiled TIFF: OpenSlide band reads plus the embedded-ICC conversion
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        image = tissue_like(2600, 2100, seed=7)
        # 'ventana' in the name routes it through OpenSlide, as the original does
        slide = self.folder/'ventana_pyramid.tif'
        with tifffile.TiffWriter(slide) as tif:
            for k in range(3):
                level = image[::2**k, ::2**k]
                tif.write(level, photometric='rgb', tile=(256, 256), compression='jpeg',
                          resolution=(1e4 / (0.23 * 2**k),) * 2, resolutionunit='CENTIMETER',
                          extratags=[(34675, 7, len(icc), icc, True)])
        for native in (1, 0):  # native read, and the coarser-level choice of fast mode
            with self.subTest(native=native):
                self.assert_slide_matches_wsi2tif(slide, native)
                for p in self.folder.rglob('*.tif'):
                    if p.parent != self.folder:
                        p.unlink()

    def test_aborted_output_leaves_no_file(self):
        image = tissue_like(3000, 1500, seed=4)
        path = self.folder/'cut.ome.tif'
        with ThreadPoolExecutor(4) as pool:
            sink = sc._OmeSink(str(path), 1500, 3000, 0.5, pool)
            sink.push(image[:1200])  # the slide read fails partway
            sink.abort()
            plain = sc._PlainTifSink(str(self.folder/'cut.tif'), 1500, 3000, 0.5)
            plain.push(image[:1200])
            plain.abort()
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [])

    def test_pyramid_on_disk_is_identical_and_cleaned_up(self):
        image = tissue_like(1500, 1700, seed=3)
        paths = []
        for name, pyramid_dir in [('ram', None), ('disk', str(self.folder))]:
            path = self.folder/f'{name}.ome.tif'
            with ThreadPoolExecutor(4) as pool:
                sink = sc._OmeSink(str(path), 1700, 1500, 0.5, pool, pyramid_dir)
                for y in range(0, 1500, 400):
                    sink.push(image[y:y+400])
                sink.close()
            paths.append(path)
        for a, b in zip(pages(paths[0]), pages(paths[1])):
            np.testing.assert_array_equal(a, b)
        self.assertEqual(list(self.folder.glob('*.npy')), [])


if __name__ == '__main__':
    unittest.main()
