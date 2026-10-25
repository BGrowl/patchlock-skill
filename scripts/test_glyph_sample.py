import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw
from photo_patch import prepare

try:
    glyph_sample = importlib.import_module('glyph_sample')
except ModuleNotFoundError:
    glyph_sample = None


class GlyphSampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def api(self):
        self.assertIsNotNone(glyph_sample, 'glyph_sample module must provide sample and place')
        return glyph_sample

    def photo(self, light=False):
        image = Image.new('RGB', (120, 80))
        for x in range(120):
            level = (35 if light else 200) + x // 4
            ImageDraw.Draw(image).line((x, 0, x, 79), fill=(level, level, level))
        ImageDraw.Draw(image).rectangle((48, 25, 55, 54), fill='white' if light else 'black')
        path = self.root / ('light.png' if light else 'dark.png')
        image.save(path)
        return path

    def test_dark_and_light_strokes_on_gradient(self):
        for light in (False, True):
            source = self.photo(light)
            before = source.read_bytes()
            output = self.root / ('donor-light.png' if light else 'donor-dark.png')
            report = self.api().sample(source, [20, 10, 80, 60], output,
                                       polarity='light' if light else 'dark')
            with Image.open(output) as donor:
                self.assertEqual(donor.mode, 'RGBA')
                self.assertEqual(donor.size, (80, 60))
                self.assertGreater(donor.getpixel((31, 30))[3], 200)
                self.assertEqual(donor.getpixel((4, 4))[3], 0)
            self.assertLess(report['stroke_coverage'], .2)
            self.assertEqual(report['donor_rect'], [20, 10, 80, 60])
            self.assertEqual(report['source_file_sha256'], hashlib.sha256(before).hexdigest())
            self.assertIn('stroke_bbox', report)
            self.assertIn('stroke_rgb_mean', report)
            self.assertIn('edge_heuristic', report)
            self.assertEqual(json.loads(output.with_suffix('.sample.json').read_text())['format'], 'patchlock-glyph-sample')
            self.assertEqual(source.read_bytes(), before)

    def test_full_photo_gray_and_alpha_masks(self):
        source = self.photo()
        for mode in ('L', 'RGBA'):
            mask = Image.new(mode, (120, 80), 0)
            value = 128 if mode == 'L' else (0, 0, 0, 128)
            ImageDraw.Draw(mask).rectangle((48, 25, 55, 54), fill=value)
            mask_path = self.root / (mode + '.png')
            mask.save(mask_path)
            output = self.root / ('masked-' + mode + '.png')
            self.api().sample(source, [20, 10, 80, 60], output, mask_path=mask_path)
            with Image.open(output) as donor:
                self.assertEqual(donor.getpixel((31, 30))[3], 128)
                self.assertEqual(donor.getpixel((1, 1))[3], 0)
        wrong = self.root / 'wrong.png'
        Image.new('L', (80, 60)).save(wrong)
        with self.assertRaises(ValueError):
            self.api().sample(source, [20, 10, 80, 60], self.root / 'bad.png', mask_path=wrong)

    def task_and_donor(self):
        source = self.photo()
        task = prepare(source, [20, 10, 80, 60], 'replace explicitly selected stroke', self.root / 'task')
        donor = self.root / 'glyph.png'
        self.api().sample(source, [40, 20, 25, 40], donor)
        return task, donor

    def test_place_native_size_transparent_alpha_and_stroke_mask(self):
        task, donor = self.task_and_donor()
        before = donor.read_bytes()
        output = self.root / 'candidate.png'
        report = self.api().place(donor, task, [[65, 20], [90, 22], [88, 62], [62, 60]], output)
        with Image.open(output) as candidate, Image.open(self.root / 'candidate.stroke-mask.png') as mask:
            self.assertEqual(candidate.size, (120, 80))
            self.assertEqual(candidate.getpixel((0, 0))[3], 0)
            self.assertIsNotNone(candidate.getchannel('A').getbbox())
            self.assertEqual(mask.mode, 'L')
            self.assertEqual(candidate.getchannel('A').tobytes(), mask.tobytes())
            self.assertLess(sum(mask.histogram()[1:]), 25 * 40)
            bbox = mask.getbbox()
            self.assertGreaterEqual(bbox[0], 20)
            self.assertLessEqual(bbox[2], 100)
        self.assertEqual(donor.read_bytes(), before)
        self.assertEqual(report['frame'], 'photo')

    def test_perspective_inverse_with_zero_global_bottom_right(self):
        source = self.root / 'square.png'
        Image.new('RGB', (64, 64), 'white').save(source)
        task = prepare(source, [0, 0, 64, 64], 'place donor', self.root / 'square-task')
        donor = self.root / 'square-donor.png'
        glyph = Image.new('RGBA', (10, 10), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).rectangle((2, 2, 7, 7), fill=(0, 0, 0, 255))
        glyph.save(donor)
        quad = [[10, 10], [20, 10], [40, 20], [20, 20]]
        output = self.root / 'perspective.png'
        try:
            self.api().place(donor, task, quad, output)
        except ValueError as exc:
            self.fail(f'Valid perspective quad must be accepted: {exc}')
        with Image.open(output) as candidate:
            alpha = candidate.getchannel('A')
            self.assertEqual(candidate.size, (64, 64))
            # Exact inverse: u=x/y-1, v=2-20/y. (21,14) is central,
            # while (12,14) maps outside the donor strokes.
            self.assertGreater(candidate.getpixel((21, 14))[3], 200)
            self.assertEqual(candidate.getpixel((12, 14))[3], 0)
            self.assertIsNotNone(alpha.getbbox())
            self.assertEqual(candidate.getpixel((0, 0))[3], 0)
        with Image.open(output.with_name('perspective.stroke-mask.png')) as mask:
            self.assertEqual(alpha.tobytes(), mask.tobytes())

    def test_perspective_pole_at_bbox_origin_outside_valid_quad(self):
        source = self.root / 'pole-square.png'
        Image.new('RGB', (64, 64), 'white').save(source)
        task = prepare(source, [0, 0, 64, 64], 'place donor', self.root / 'pole-task')
        donor = self.root / 'pole-donor.png'
        glyph = Image.new('RGBA', (10, 10), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).rectangle((2, 2, 7, 7), fill=(0, 0, 0, 255))
        glyph.save(donor)
        quad = [[30, 27], [60, 12], [48, 54], [20, 47]]
        output = self.root / 'pole-candidate.png'
        try:
            self.api().place(donor, task, quad, output)
        except ValueError as exc:
            self.fail(f'Pole outside target quad must not reject valid placement: {exc}')
        with Image.open(output) as candidate:
            alpha = candidate.getchannel('A')
            self.assertEqual(candidate.size, (64, 64))
            self.assertEqual(alpha.getextrema(), (0, 255))
            self.assertEqual(candidate.getpixel((20, 12))[3], 0)
            self.assertEqual(candidate.getpixel((0, 0))[3], 0)
            self.assertGreater(sum(alpha.histogram()[1:]), 20)
            bbox = alpha.getbbox()
            self.assertGreaterEqual(bbox[0], 20)
            self.assertGreaterEqual(bbox[1], 12)
            self.assertLessEqual(bbox[2], 60)
            self.assertLessEqual(bbox[3], 54)
        with Image.open(output.with_name('pole-candidate.stroke-mask.png')) as mask:
            self.assertEqual(alpha.tobytes(), mask.tobytes())

    def test_bad_quads_rejected_before_outputs(self):
        task, donor = self.task_and_donor()
        for index, quad in enumerate(([[20, 10]] * 4,
                                     [[20, 10], [90, 60], [90, 10], [20, 60]],
                                     [[19, 10], [90, 10], [90, 60], [20, 60]],
                                     [[20, 10], [90, 10], [90, 71], [20, 60]],
                                     [[20, 10], [90, 10], [90, float('nan')], [20, 60]])):
            output = self.root / f'bad-{index}.png'
            with self.assertRaises((ValueError, TypeError)):
                self.api().place(donor, task, quad, output)
            self.assertFalse(output.exists())

    def test_empty_or_solid_samples_rejected(self):
        source = self.root / 'solid.png'
        Image.new('RGB', (50, 50), 'gray').save(source)
        for mask_value in (None, 0, 255):
            mask_path = None
            if mask_value is not None:
                mask_path = self.root / f'mask-{mask_value}.png'
                Image.new('L', (50, 50), mask_value).save(mask_path)
            with self.assertRaises(ValueError):
                self.api().sample(source, [0, 0, 50, 50], self.root / f'out-{mask_value}.png', mask_path=mask_path)

    def test_no_overwrite_for_primary_sidecars_or_inputs(self):
        source = self.photo()
        original = source.read_bytes()
        output = self.root / 'donor.png'
        sidecar = output.with_suffix('.sample.json')
        sidecar.write_text('keep')
        with self.assertRaises(ValueError):
            self.api().sample(source, [20, 10, 80, 60], output)
        self.assertFalse(output.exists())
        self.assertEqual(sidecar.read_text(), 'keep')
        with self.assertRaises(ValueError):
            self.api().sample(source, [20, 10, 80, 60], source)
        self.assertEqual(source.read_bytes(), original)
        task, donor = self.task_and_donor()
        candidate = self.root / 'placed.png'
        stroke_mask = self.root / 'placed.stroke-mask.png'
        stroke_mask.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.api().place(donor, task, [[20, 10], [70, 10], [70, 60], [20, 60]], candidate)
        self.assertFalse(candidate.exists())
        self.assertEqual(stroke_mask.read_bytes(), b'keep')


    def test_place_rejects_semtransparent_rectangles(self):
        task, donor = self.task_and_donor()
        Image.new('RGBA', (25, 40), (0, 0, 0, 128)).save(donor)
        with self.assertRaises(ValueError):
            self.api().place(donor, task, [[20, 10], [70, 10], [70, 60], [20, 60]], self.root / 'rectangle.png')

    def test_source_and_donor_alpha_preserved(self):
        source = self.photo()
        with Image.open(source) as opened:
            image = opened.convert('RGBA')
        image.putalpha(128)
        image.save(source)
        mask_path = self.root / 'alpha-mask.png'
        mask = Image.new('L', image.size, 0)
        ImageDraw.Draw(mask).rectangle((48, 25, 55, 54), fill=128)
        mask.save(mask_path)
        donor = self.root / 'translucent.png'
        self.api().sample(source, [40, 20, 25, 40], donor, mask_path=mask_path)
        with Image.open(donor) as opened:
            self.assertEqual(opened.getchannel('A').getextrema(), (0, 64))
        task = prepare(source, [20, 10, 80, 60], 'replace stroke', self.root / 'alpha-task')
        output = self.root / 'alpha-candidate.png'
        self.api().place(donor, task, [[20, 10], [72, 13], [70, 65], [22, 63]], output)
        with Image.open(output) as opened:
            # Bicubic ringing may overshoot; preserving alpha requires no inflation.
            self.assertLessEqual(opened.getchannel('A').getextrema()[1], 64)
            self.assertIsNotNone(opened.getchannel('A').getbbox())

    def test_exif_normalization_and_source_40mp_limit(self):
        import struct
        import zlib
        source = self.photo()
        with Image.open(source) as image:
            exif = image.getexif()
            exif[274] = 6
            image.save(self.root / 'rotated.png', exif=exif)
        report = self.api().sample(self.root / 'rotated.png', [10, 20, 60, 80], self.root / 'rotated-donor.png')
        self.assertEqual(report['source_size'], [80, 120])
        data = source.read_bytes()
        ihdr = struct.pack('>IIBBBBB', 40000001, 1, 8, 2, 0, 0, 0)
        huge = data[:8] + struct.pack('>I', 13) + b'IHDR' + ihdr + struct.pack('>I', zlib.crc32(b'IHDR' + ihdr)) + data[33:]
        too_big = self.root / 'huge.png'
        too_big.write_bytes(huge)
        with self.assertRaisesRegex(ValueError, '4000'):
            self.api().sample(too_big, [0, 0, 10, 1], self.root / 'huge-out.png')

    def test_cli_help(self):
        script = Path(__file__).with_name('glyph_sample.py')
        for command in ('sample', 'place'):
            result = subprocess.run([sys.executable, '-B', str(script), command, '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--output', result.stdout)


if __name__ == '__main__':
    unittest.main()
