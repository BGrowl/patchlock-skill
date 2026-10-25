"""Observable photo matching / old-stroke repair invariants, no model required."""
import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageCms


class PhotoFinishTests(unittest.TestCase):
    def api(self):
        spec = importlib.util.find_spec('photo_finish')
        self.assertIsNotNone(spec, 'photo_finish must provide photograph matching and stroke repair')
        import photo_finish
        return photo_finish

    def scene(self):
        yy, xx = np.mgrid[:100, :120]
        values = np.stack([80 + xx / 3 + yy / 8, 90 + xx / 4, 110 + yy / 4], axis=-1)
        return Image.fromarray(values.astype('uint8')).convert('RGBA')

    def test_match_reduces_known_gain_offset_and_smooth_lighting_error(self):
        api = self.api()
        reference = self.scene()
        arr = np.array(reference).copy()
        arr[:, :, :3] = np.clip(arr[:, :, :3] * .8 + 25, 0, 255)
        candidate = Image.fromarray(arr)
        mask = Image.new('L', reference.size, 255)
        ImageDraw.Draw(mask).rectangle((40, 30, 70, 60), fill=0)
        before = np.mean(np.abs(arr[:, :, :3].astype(float) - np.array(reference)[:, :, :3]))
        result, report = api.match_photo(reference, candidate, mask)
        after = np.mean(np.abs(np.array(result)[:, :, :3].astype(float) - np.array(reference)[:, :, :3]))
        self.assertLess(after, before / 3)
        self.assertLess(report['sample_mae_after'], report['sample_mae_before'])
        self.assertEqual(result.getchannel('A').tobytes(), candidate.getchannel('A').tobytes())

    def test_match_preserves_alpha_and_is_deterministic_with_grain(self):
        api = self.api()
        original = self.scene()
        candidate = original.copy()
        candidate.putpixel((20, 20), (12, 15, 18, 0))
        mask = Image.new('L', original.size, 255)
        mask.putpixel((20, 20), 0)
        first, _ = api.match_photo(original, candidate, mask, blur_radius=.5, grain=.25)
        second, _ = api.match_photo(original, candidate, mask, blur_radius=.5, grain=.25)
        self.assertEqual(first.tobytes(), second.tobytes())
        self.assertEqual(first.getchannel('A').tobytes(), candidate.getchannel('A').tobytes())

    def test_matching_rejects_unusable_samples_and_parameters(self):
        api = self.api()
        image = self.scene()
        for mask in [Image.new('L', image.size), Image.new('L', (10, 10), 255)]:
            with self.assertRaises(ValueError):
                api.match_photo(image, image, mask)
        for kwargs in [dict(blur_radius=9), dict(grain=-1), dict(grain=float('nan'))]:
            with self.assertRaises(ValueError):
                api.match_photo(image, image, Image.new('L', image.size, 255), **kwargs)
        bad = Image.new('RGBA', image.size, (240, 10, 240, 255))
        with self.assertRaises(ValueError):
            api.match_photo(image, bad, Image.new('L', image.size, 255))

    def test_strokes_remove_old_ink_place_new_and_preserve_background_and_protection(self):
        api = self.api()
        original = Image.new('RGBA', (90, 80), (130, 150, 180, 255))
        ImageDraw.Draw(original).rectangle((20, 20, 28, 50), fill=(0, 0, 0, 255))
        old = Image.new('L', original.size)
        ImageDraw.Draw(old).rectangle((18, 18, 30, 52), fill=255)
        replacement = Image.new('RGBA', original.size)
        ImageDraw.Draw(replacement).rectangle((45, 25, 51, 49), fill=(245, 245, 245, 255))
        new = replacement.getchannel('A')
        protected = Image.new('L', original.size)
        protected.putpixel((20, 30), 255)
        result, allowed, report = api.repair_strokes(original, replacement, old, new, protected)
        self.assertEqual(result.getpixel((20, 30)), original.getpixel((20, 30)))
        self.assertEqual(allowed.getpixel((20, 30)), 0)
        self.assertGreater(result.getpixel((24, 35))[0], 100)
        self.assertEqual(result.getpixel((48, 35)), (245, 245, 245, 255))
        delta = np.any(np.array(original) != np.array(result), axis=2)
        self.assertFalse(np.any(delta & (np.array(allowed) == 0)))
        self.assertGreater(report['old_stroke_pixels'], 0)

    def test_empty_repair_and_mismatched_masks_are_rejected(self):
        api = self.api()
        original = self.scene()
        empty = Image.new('L', original.size)
        with self.assertRaises(ValueError):
            api.repair_strokes(original, original, empty, empty)
        with self.assertRaises(ValueError):
            api.repair_strokes(original, original, Image.new('L', (1, 1), 255), empty)

    def test_replacement_alpha_is_applied_once_when_new_mask_is_its_alpha(self):
        api = self.api()
        original = Image.new('RGBA', (20, 20), 'white')
        replacement = Image.new('RGBA', (20, 20))
        replacement.putpixel((10, 10), (0, 0, 0, 128))
        result, _, _ = api.repair_strokes(original, replacement, Image.new('L', original.size), replacement.getchannel('A'))
        self.assertEqual(result.getpixel((10, 10)), (127, 127, 127, 255))

    def test_blur_uses_premultiplied_alpha_without_dark_halos(self):
        api = self.api()
        original = Image.new('RGBA', (30, 30), 'white')
        candidate = original.copy()
        candidate.paste((0, 0, 0, 0), (8, 8, 22, 22))
        candidate.putpixel((15, 15), (255, 255, 255, 255))
        sample = Image.new('L', original.size, 255)
        ImageDraw.Draw(sample).rectangle((7, 7, 23, 23), fill=0)
        result, _ = api.match_photo(original, candidate, sample, blur_radius=1)
        self.assertEqual(result.getpixel((15, 15)), (255, 255, 255, 255))

    def test_fractional_rgba_protection_remains_nonzero(self):
        api = self.api()
        original = Image.new('RGBA', (20, 20), 'white')
        replacement = Image.new('RGBA', (20, 20), 'black')
        protect = Image.new('RGBA', original.size)
        protect.putpixel((10, 10), (1, 1, 1, 1))
        result, allowed, _ = api.repair_strokes(original, replacement, Image.new('L', original.size), Image.new('L', original.size, 255), protect)
        self.assertEqual(result.getpixel((10, 10)), original.getpixel((10, 10)))
        self.assertEqual(allowed.getpixel((10, 10)), 0)

    def test_subsample_alias_cannot_hide_inconsistent_background(self):
        api = self.api()
        original = Image.new('RGBA', (1000, 1000), (100, 100, 100, 255))
        candidate = np.array(original)
        candidate[:, 1::2, :3] = 230
        with self.assertRaises(ValueError):
            api.match_photo(original, Image.fromarray(candidate), Image.new('L', original.size, 255))

    def test_explicit_srgb_assumption_converts_untagged_candidate_and_records_it(self):
        api = self.api()
        self.assertIn('assume_untagged_srgb', inspect.signature(api.match_photo).parameters)
        original = self.scene()
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        original.info['icc_profile'] = profile
        candidate = self.scene()
        sample = Image.new('L', original.size, 255)
        with self.assertRaises(ValueError):
            api.match_photo(original, candidate, sample)
        result, report = api.match_photo(original, candidate, sample, assume_untagged_srgb=True)
        self.assertEqual(result.info['icc_profile'], profile)
        self.assertTrue(report['color_management']['assumed_untagged_srgb'])
        self.assertNotIn('icc_profile', candidate.info)

    def test_invalid_embedded_profile_cannot_be_relabelled_as_srgb(self):
        api = self.api()
        self.assertIn('assume_untagged_srgb', inspect.signature(api.match_photo).parameters)
        original = self.scene()
        original.info['icc_profile'] = b'invalid profile'
        with self.assertRaises(ValueError):
            api.match_photo(original, self.scene(), Image.new('L', original.size, 255), assume_untagged_srgb=True)

    def test_identical_invalid_profiles_are_rejected(self):
        api = self.api()
        original, candidate = self.scene(), self.scene()
        original.info['icc_profile'] = candidate.info['icc_profile'] = b'invalid profile'
        with self.assertRaises(ValueError):
            api.match_photo(original, candidate, Image.new('L', original.size, 255))

    def test_single_color_background_does_not_extrapolate_gain_onto_white_lettering(self):
        api = self.api()
        yy, xx = np.mgrid[:100, :120]
        values = np.stack([30 + xx / 20, 60 + xx / 20, 130 + xx / 20], axis=-1)
        original = Image.fromarray(values.astype('uint8')).convert('RGBA')
        candidate = np.array(original)
        candidate[:, :, 0] = candidate[:, :, 0] * 1.2 - 3
        candidate[:, :, 1] = candidate[:, :, 1] * 1.05 + 2
        candidate[40:60, 50:70, :3] = (240, 240, 230)
        sample = Image.new('L', original.size, 255)
        ImageDraw.Draw(sample).rectangle((49, 39, 71, 61), fill=0)
        result, report = api.match_photo(original, Image.fromarray(candidate), sample)
        self.assertGreater(result.getpixel((55, 45))[0], 225)
        self.assertEqual(report['tone_mode'], 'additive')

    def test_affine_tone_mode_requires_explicit_selection_and_valid_name(self):
        api = self.api()
        self.assertIn('tone_mode', inspect.signature(api.match_photo).parameters)
        original = self.scene()
        sample = Image.new('L', original.size, 255)
        _, report = api.match_photo(original, original, sample, tone_mode='affine')
        self.assertEqual(report['tone_mode'], 'affine')
        with self.assertRaises(ValueError):
            api.match_photo(original, original, sample, tone_mode='unknown')

    def test_match_paths_refuses_existing_report_before_writing_image(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.scene().save(root / 'original.png')
            self.scene().save(root / 'candidate.png')
            Image.new('L', self.scene().size, 255).save(root / 'sample.png')
            (root / 'result.match.json').write_text('keep', encoding='utf-8')
            with self.assertRaises(ValueError):
                api.match_paths(root / 'original.png', root / 'candidate.png', root / 'sample.png', root / 'result.png')
            self.assertFalse((root / 'result.png').exists())
            self.assertEqual((root / 'result.match.json').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
