"""Synthetic regression tests. No patient images, network or MATLAB licence needed."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from scipy.io import savemat
import tifffile
from openslide import OpenSlide

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '01_conversion_downsampling'), str(ROOT / '03_apply_registration')]
import WSI2OMEtif_All_file_types as multi
import apply_registration_to_20x_image as registration
import apply_40x_tiled
from image_metadata import read_tiff_mpp
import run_conversion as conversion_runner


def pattern(height=48, width=64):
    y, x = np.indices((height, width))
    return np.stack(((x*7) % 230, (y*11) % 230, (x+y)*3 % 230), -1).astype('uint8')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_script_settings_export_four_resolutions_from_one_read(self):
        source = self.folder / 'input.tif'
        tifffile.imwrite(source, pattern(80, 160), resolution=(40000, 40000), resolutionunit='CENTIMETER')
        output = self.folder / 'output'
        with patch.multiple(conversion_runner, pth0=str(source), outpth=str(output),
                            file_format='other', folder_names=['2x', '10x', '20x', '40x'],
                            pixel_resolutions=[5, 1, 0.5, 0.25], save_ome=[0, 1, 1, 1],
                            load_native_resolution=1):
            with patch.object(multi.Image, 'open', wraps=Image.open) as reader:
                conversion_runner.main([])
                self.assertEqual(reader.call_count, 1)
        for name, mpp, shape, suffix in [('2x',5,(4,8),'.tif'), ('10x',1,(20,40),'.ome.tif'),
                                        ('20x',0.5,(40,80),'.ome.tif'), ('40x',0.25,(80,160),'.ome.tif')]:
            f = output / name / ('input'+suffix)
            self.assertEqual(read_tiff_mpp(f), (mpp, mpp))
            with tifffile.TiffFile(f) as tif:
                self.assertEqual(tif.pages[0].shape[:2], shape)

    def test_dedicated_readers_load_once_for_multiple_outputs(self):
        for module_name, reader_name in [('vsi2ometif', 'read_vsi'), ('CZI2OMEtif', 'read_czi')]:
            with self.subTest(reader=reader_name):
                try:
                    import importlib
                    module = importlib.import_module(module_name)
                except ImportError:
                    self.skipTest('Install scanner dependencies for dedicated reader tests.')
                for resolutions, expected_target in [([5, 1, 0.5, 0.25], 0.25), ([5, 0], 0)]:
                    out = self.folder / module_name / str(expected_target)
                    folders = [str(i) for i in range(len(resolutions))]
                    for folder in folders:
                        (out / folder).mkdir(parents=True)
                    with patch.object(module, reader_name, return_value=(Image.fromarray(pattern(80,160)),0.25,0.25)) as read:
                        module.process_images(str(self.folder), folders, ['slide.'+reader_name[5:]],
                                              resolutions, [0]*len(folders), outpth=str(out))
                        self.assertEqual(read.call_count, 1)
                        self.assertEqual(read.call_args.args[1], expected_target)
                    self.assertEqual(len(list(out.rglob('*.tif'))), len(folders))

    def test_multiresolution_cli(self):
        source = self.folder / 'input.tif'
        tifffile.imwrite(source, pattern(), resolution=(20000,20000), resolutionunit='CENTIMETER')
        output = self.folder / 'out'
        result = subprocess.run([sys.executable, str(ROOT / '01_conversion_downsampling/run_conversion.py'),
            '--format', 'other', '--input', str(source), '--output', str(output),
            '--folder', '2x', '20x', '--mpp', '5', '0.5', '--save-ome', '0', '1'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertTrue((output / '2x/input.tif').is_file())
        self.assertTrue((output / '20x/input.ome.tif').is_file())

    def test_output_settings_are_validated_before_creating_folders(self):
        source = self.folder / 'input.tif'
        source.touch()
        for folders, resolutions, formats in [(['2x','20x'],[5],[0,1]),
                                              (['2x','2X'],[5,1],[0,1]),
                                              (['../outside'],[5],[0])]:
            with self.assertRaises(ValueError):
                conversion_runner.run_conversion(source,self.folder/'out','other',folders,resolutions,formats)
        self.assertFalse((self.folder/'out').exists())

    def test_calibrated_tiff_resolution(self):
        f = self.folder / 'sample.tif'
        tifffile.imwrite(f, pattern(), resolution=(20000, 10000), resolutionunit='CENTIMETER')
        self.assertEqual(read_tiff_mpp(f), (0.5, 1.0))

    def test_unknown_resolution_is_rejected(self):
        f = self.folder / 'sample.tif'
        tifffile.imwrite(f, pattern())
        with self.assertRaises(ValueError):
            read_tiff_mpp(f)

    def test_ome_pyramid_roundtrip(self):
        multi.save_ome_tif(pattern(), str(self.folder), '.', 'sample', 0.5)
        f = self.folder / 'sample.ome.tif'
        self.assertEqual(read_tiff_mpp(f), (0.5, 0.5))
        with tifffile.TiffFile(f) as tif:
            self.assertTrue(tif.is_ome)
            self.assertEqual(tif.pages[0].shape, (48, 64, 3))
            self.assertGreater(len(tif.series[0].levels), 1)
            self.assertEqual(tif.series[0].levels[1].shape[:2], (24, 32))
            self.assertIn(34675, tif.pages[0].tags)
            self.assertLess(np.abs(tif.asarray().astype(float)-pattern()).mean(), 12)

    def test_multiformat_downsample_plain_tiff(self):
        source = self.folder / 'slide.01.TIF'
        tifffile.imwrite(source, pattern(), resolution=(20000, 20000), resolutionunit='CENTIMETER')
        multi.WSI2tif(str(source), ['2x'], [2], [0], outpth=str(self.folder))
        output = self.folder / '2x/slide.01.tif'
        with Image.open(output) as im:
            self.assertEqual(im.size, (16, 12))
            pixels = np.array(im)
        self.assertEqual(read_tiff_mpp(output), (2, 2))
        np.testing.assert_array_equal(pixels, np.array(Image.fromarray(pattern()).resize((16, 12), Image.Resampling.NEAREST)))

    def test_no_upsampling(self):
        source = self.folder / 'input.tif'
        tifffile.imwrite(source, pattern(), resolution=(20000, 20000), resolutionunit='CENTIMETER')
        multi.WSI2tif(str(source), ['fine'], [0.1], [0], outpth=str(self.folder))
        with Image.open(self.folder / 'fine/input.tif') as im:
            self.assertEqual(im.size, (64, 48))
        self.assertEqual(read_tiff_mpp(self.folder / 'fine/input.tif'), (0.5, 0.5))

    def test_affine_identity_and_translation(self):
        image = pattern()
        np.testing.assert_array_equal(registration.transform_image(image, np.eye(3), 0, tile=7), image)
        matrix = np.eye(3)
        matrix[2, :2] = [3, 2]
        out = registration.transform_image(image, matrix, 0, tile=7)
        np.testing.assert_array_equal(out[2:, 3:], image[:-2, :-3])
        self.assertTrue(np.all(out[:2] == 241))

    def test_matlab_one_based_rotation_centre(self):
        image = pattern(8, 10)
        matrix = np.diag([-1., -1., 1.])
        out = registration.transform_image(image, matrix, 0, cent=(5.5, 4.5), tile=3)
        np.testing.assert_array_equal(out, image[::-1, ::-1])

    def test_flip(self):
        image = pattern()
        np.testing.assert_array_equal(registration.transform_image(image, np.eye(3), 1), image[::-1])

    def test_elastic_identity_translation_and_outside(self):
        image = pattern()
        field = np.zeros((48, 64, 2), np.float32)
        np.testing.assert_array_equal(registration.register_image_elastic(image, field, 1, None, tile=7), image)
        field[..., 0] = 2
        moved = registration.register_image_elastic(image, field, 1, None, tile=7)
        np.testing.assert_array_equal(moved[:, :-2], image[:, 2:])
        self.assertTrue(np.all(moved[:, -2:] == 241))
        field[:] = 1000
        self.assertTrue(np.all(registration.register_image_elastic(image, field, 1, None, tile=7) == 241))

    def test_padding(self):
        image = pattern(3, 4)
        padded = registration.pad_image(image, [5, 8], 2)
        self.assertEqual(padded.shape, (9, 12, 3))
        np.testing.assert_array_equal(padded[3:6, 4:8], image)

    def test_dotted_image_name(self):
        self.assertEqual(registration.remove_extension('slide.01.ome.tif'), 'slide.01')

    def test_reference_first_and_numeric_moving_transform(self):
        warps = self.folder / 'registered/elastic registration/save_warps'
        (warps / 'D').mkdir(parents=True)
        image = pattern(8, 10)
        savemat(warps / 'a.reference.mat', {'zc': 1})
        savemat(warps / 'b.moving.mat', {'szz': [8, 10], 'padall': 0, 'f': 0,
                'cent': [5.5, 4.5], 'tform_python': np.eye(3)})
        savemat(warps / 'D/b.moving.mat', {'D': np.zeros((8, 10, 2))})
        for name in ['a.reference', 'b.moving']:
            Image.fromarray(image).save(warps.parent / (name + '.jpg'))
            out, _ = registration.apply_CODA_registration(image, name, str(warps), 1, 0)
            np.testing.assert_array_equal(out, image)

    def test_conversion_cli_reports_read_failure(self):
        bad = self.folder / 'bad.tif'
        bad.write_text('not a TIFF')
        result = subprocess.run([sys.executable, str(ROOT / '01_conversion_downsampling/run_conversion.py'),
            '--format', 'other', '--input', str(bad), '--output', str(self.folder / 'out'),
            '--folder', '2x', '--mpp', '5'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_application_batch_includes_reference_without_D(self):
        source = self.folder / 'images'
        source.mkdir()
        warps = self.folder / 'registered/elastic registration/save_warps'
        (warps / 'D').mkdir(parents=True)
        image = pattern(8, 10)
        savemat(warps / 'a.mat', {'zc': 1})
        savemat(warps / 'b.mat', {'szz': [8, 10], 'padall': 0, 'f': 0,
                'cent': [5.5, 4.5], 'tform_python': np.eye(3)})
        savemat(warps / 'D/b.mat', {'D': np.zeros((8, 10, 2))})
        for name in ['a', 'b']:
            tifffile.imwrite(source / (name+'.tif'), image, resolution=(20000, 20000), resolutionunit='CENTIMETER')
            Image.fromarray(image).save(warps.parent / (name+'.jpg'))
        out = self.folder / 'output'
        registration.apply_registration_to_20x(str(source), str(warps), 0.5, out_folder=str(out))
        self.assertEqual(len(list(out.glob('*.ome.tif'))), 2)
        self.assertEqual(read_tiff_mpp(out / 'a.ome.tif'), (0.5, 0.5))
        import csv
        logs = list((out / 'timings').glob('apply_registration_*.csv'))
        self.assertEqual(len(logs), 1)
        with logs[0].open(newline='', encoding='utf-8') as stream:
            records = list(csv.DictReader(stream))
        totals = {r['image']: r for r in records if r['phase']=='image_total'}
        self.assertEqual(totals['a']['status'], 'reference')
        self.assertEqual(totals['b']['status'], 'ok')
        self.assertEqual({r['phase'] for r in records}, {'read','apply','save','image_total','batch_total'})

    def test_application_one_file_uses_explicit_transform_name(self):
        source = self.folder / 'scanner' / '40x'
        source.mkdir(parents=True)
        warps = self.folder / 'tissue' / '2x' / 'registered' / 'elastic registration' / 'save_warps'
        (warps / 'D').mkdir(parents=True)
        image = pattern(8, 10)
        tifffile.imwrite(source / 'slide.ome.tif', image, resolution=(40000, 40000),
                         resolutionunit='CENTIMETER')
        savemat(warps / 'slide_S210.mat', {'zc': 1})
        savemat(warps / 'slide_S360.mat', {'szz': [8, 10], 'padall': 0, 'f': 0,
                'cent': [5.5, 4.5], 'tform_python': np.eye(3)})
        Image.fromarray(image).save(warps.parent / 'slide_S210.jpg')
        output = self.folder / 'tissue' / '40x' / 'registered'
        result = subprocess.run([sys.executable, str(ROOT / '03_apply_registration/run_apply_registration.py'),
            '--input-file', str(source / 'slide.ome.tif'), '--transform-name', 'slide_S210',
            '--warps', str(warps), '--output', str(output), '--registration-mpp', '0.25'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((output / 'slide_S210.ome.tif').is_file())
        self.assertFalse((source / 'slide_S210.ome.tif').exists())

    def test_tiled_application_matches_existing_algorithm(self):
        source = self.folder / 'slide.ome.tif'
        image = pattern(180, 190)
        registration.save_ome_tif(image, str(source), .25)
        warps = self.folder / 'registered' / 'elastic registration' / 'save_warps'
        (warps / 'D').mkdir(parents=True)
        fields = np.zeros((12, 13, 2), dtype=np.float32)
        fy, fx = np.indices(fields.shape[:2])
        fields[..., 0] = .3 + fx * .002
        fields[..., 1] = -.2 + fy * .002
        savemat(warps / 'slide_S210.mat', {'zc': 1})
        affine = np.array([[.999, .012, 0], [-.012, .999, 0], [.2, -.3, 1]])
        savemat(warps / 'slide_S360.mat', {'szz': [12, 13], 'padall': 1,
                'f': 1, 'cent': [6.5, 6.5], 'tform_python': affine})
        savemat(warps / 'D/slide_S360.mat', {'D': fields})
        for name in ('slide_S210', 'slide_S360'):
            Image.fromarray(pattern(12, 13)).save(warps.parent / (name + '.jpg'))
        for name in ('slide_S210', 'slide_S360'):
            with self.subTest(name=name):
                with OpenSlide(str(source)) as slide:
                    decoded = np.asarray(slide.read_region((0, 0), 0, slide.dimensions).convert('RGB'))
                    expected, _ = registration.apply_CODA_registration(decoded, name, str(warps), 20, 0)
                    metadata = apply_40x_tiled.load_mat_file(warps / 'slide_S360.mat')
                    output_hw, pre = apply_40x_tiled.canvas_parameters(decoded.shape[:2], metadata, 20)
                    if name == 'slide_S210':
                        transform, field, flip = None, None, False
                    else:
                        transform = apply_40x_tiled.inverse_affine(metadata, 20)
                        field = apply_40x_tiled.displacement_components(fields)
                        flip = True
                    rendered = np.zeros_like(expected)
                    for y0 in range(0, output_hw[0], 128):
                        for x0 in range(0, output_hw[1], 128):
                            block = apply_40x_tiled.render_tile(slide, x0, y0, 128,
                                                                 output_hw, pre, transform,
                                                                 flip, field, 20)
                            h = min(128, output_hw[0]-y0)
                            w = min(128, output_hw[1]-x0)
                            rendered[y0:y0+h, x0:x0+w] = block[:h, :w]
                    np.testing.assert_array_equal(rendered, expected)
                output = self.folder / (name + '.ome.tif')
                apply_40x_tiled.apply_tiled(source, warps, name, output,
                                           registration_mpp=5, tile_size=128)
                actual = tifffile.imread(output)
                self.assertEqual(actual.shape, expected.shape)
                self.assertLess(np.mean(np.abs(actual.astype(int) - expected.astype(int))), 4)


    def test_resolution_scaling_10x_20x_40x(self):
        for scale in (5, 10, 20):
            with self.subTest(scale=scale):
                image = pattern(4*scale, 6*scale)
                transform = np.eye(3)
                transform[2, 0] = 1
                moved = registration.transform_image(image, transform, 0, scale=scale, tile=13)
                np.testing.assert_array_equal(moved[:, scale:], image[:, :-scale])
                self.assertTrue(np.all(moved[:, :scale] == 241))
                field = np.zeros((4, 6, 2), np.float32)
                field[..., 0] = 1
                elastic = registration.register_image_elastic(image, field, scale, None, tile=13)
                np.testing.assert_array_equal(elastic[:, :-scale], image[:, scale:])

    def test_real_czi_reader_with_generated_file(self):
        try:
            from pylibCZIrw import czi
            from CZI2OMEtif import read_czi
        except ImportError:
            self.skipTest('Install requirements/scanners.txt to test the CZI reader.')
        source = self.folder / 'generated.czi'
        image = pattern()
        with czi.create_czi(str(source)) as writer:
            writer.write(np.ascontiguousarray(image[..., ::-1]), location=(-10, -20), plane={'C': 0})
            # This writer writes the supplied values into CZI's metre-valued scaling XML.
            writer.write_metadata(scale_x=0.5e-6, scale_y=0.5e-6)
        result, mppx, mppy = read_czi(str(source), 1, 1)
        self.assertAlmostEqual(mppx, 1)
        self.assertAlmostEqual(mppy, 1)
        np.testing.assert_array_equal(np.asarray(result), np.asarray(Image.fromarray(image).resize((32,24), Image.Resampling.NEAREST)))

    def test_vsi_native_reader_with_simulated_scene(self):
        try:
            import vsi2ometif as vsi
        except ImportError:
            self.skipTest('Install requirements/scanners.txt to test the VSI adapter.')
        image = pattern()
        class Scene:
            rect = (0, 0, 64, 48)
            resolution = (0.5e-6, 0.5e-6)
            def read_block(self, rect, size=None):
                x,y,w,h = rect
                part = image[y:y+h,x:x+w]
                return np.array(Image.fromarray(part).resize(size)) if size else part
        class Slide:
            num_scenes = 1
            raw_metadata = ''
            def get_scene(self, index):
                return Scene()
        with patch.object(vsi.slideio, 'open_slide', return_value=Slide()):
            result, x, y = vsi.read_vsi('simulated.vsi', 1, 1)
        self.assertEqual((x, y), (1, 1))
        np.testing.assert_array_equal(np.asarray(result), image[1::2,1::2])

    def test_hdf5_matlab_numeric_array_orientation(self):
        try:
            import h5py
        except ImportError:
            self.skipTest('h5py is required for MATLAB v7.3 numeric arrays.')
        f = self.folder / 'numeric.mat'
        expected = np.arange(8*10*2).reshape(8,10,2).astype(float)
        with h5py.File(f, 'w') as h5:
            dataset = h5.create_dataset('D', data=expected.transpose(2,1,0))
            dataset.attrs['MATLAB_class'] = np.bytes_('double')
        np.testing.assert_array_equal(registration.load_mat_file(f)['D'], expected)


if __name__ == '__main__':
    unittest.main()
