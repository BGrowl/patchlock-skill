import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from photo_patch import prepare, load_image, pixel_hash


class SalvageTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('salvage'), 'salvage must orchestrate candidate recovery and evidence')
        import salvage
        return salvage

    def setup_task(self, root):
        original = Image.new('RGBA', (160, 140), (80, 110, 160, 255))
        original.save(root / 'source.png')
        task = prepare(root / 'source.png', [50, 40, 40, 40], 'Replace selected mark', root / 'task')
        candidate = original.copy()
        draw = ImageDraw.Draw(candidate)
        draw.rectangle((55, 45, 80, 65), fill=(220, 210, 200, 255))
        draw.rectangle((2, 2, 15, 15), fill=(255, 0, 0, 255))
        candidate.save(root / 'candidate.png')
        return original, task

    def test_external_candidate_keeps_only_target_and_emits_quality_provenance(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, task = self.setup_task(root)
            report = api.run(task, root / 'candidate.png', 'photo', root / 'final.png', alignment='identity')
            result = load_image(root / 'final.png')
            self.assertEqual(result.getpixel((5, 5)), original.getpixel((5, 5)))
            self.assertEqual(result.getpixel((60, 50)), (220, 210, 200, 255))
            self.assertTrue(report['verification']['pixel_scope_pass'])
            self.assertGreater(report['verification']['changed_inside_allowed'], 0)
            self.assertTrue((root / 'final.quality.html').exists())
            self.assertTrue((root / 'final.salvage.json').exists())
            self.assertEqual(report['original_sha256'], pixel_hash(original))
            self.assertEqual(load_image(root / 'source.png').tobytes(), original.tobytes())

    def test_noop_retains_review_required_status(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            report = api.run(task, root / 'source.png', 'photo', root / 'final.png', alignment='identity')
            self.assertEqual(report['verification']['changed_inside_allowed'], 0)
            self.assertNotEqual(report['quality']['status'], 'pass')

    def test_preflight_report_collision_writes_no_final_artifacts(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            (root / 'final.quality.html').write_text('keep', encoding='utf-8')
            with self.assertRaises(ValueError):
                api.run(task, root / 'candidate.png', 'photo', root / 'final.png', alignment='identity')
            self.assertFalse((root / 'final.png').exists())
            self.assertFalse((root / 'final.salvage').exists())

    def test_matching_sample_must_be_unchanged_and_outside_task(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            sample = Image.new('L', (160, 140), 255)
            sample.save(root / 'sample.png')
            with self.assertRaises(ValueError):
                api.run(task, root / 'candidate.png', 'photo', root / 'final.png', alignment='identity', sample_mask=root / 'sample.png')
            self.assertFalse((root / 'final.png').exists())

    def test_auto_alignment_refuses_featureless_photo(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            with self.assertRaises(ValueError):
                api.run(task, root / 'candidate.png', 'photo', root / 'final.png')
            self.assertFalse((root / 'final.png').exists())

    def test_patch_cannot_implicitly_claim_background_registration(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            Image.new('RGBA', (40, 40), (240, 240, 240, 255)).save(root / 'patch.png')
            with self.assertRaises(ValueError):
                api.run(task, root / 'patch.png', 'patch', root / 'final.png')

    def test_invalid_text_evidence_and_empty_effective_mask_leave_no_outputs(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, task = self.setup_task(root)
            Image.new('L', (160, 140)).save(root / 'empty.png')
            for kwargs in [dict(observed_text='x'), dict(edit_mask=root / 'empty.png')]:
                with self.assertRaises(ValueError):
                    api.run(task, root / 'candidate.png', 'photo', root / 'final.png', alignment='identity', **kwargs)
                self.assertFalse((root / 'final.png').exists())
                self.assertFalse((root / 'final.salvage').exists())

    def test_matching_on_confirmed_background_preserves_unrelated_pixels(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, task = self.setup_task(root)
            candidate = Image.new('RGBA', original.size, (90, 120, 170, 255))
            ImageDraw.Draw(candidate).rectangle((55, 45, 80, 65), fill=(230, 220, 210, 255))
            candidate.save(root / 'tinted.png')
            sample = Image.new('L', original.size)
            ImageDraw.Draw(sample).rectangle((5, 5, 40, 120), fill=255)
            sample.save(root / 'sample.png')
            report = api.run(task, root / 'tinted.png', 'photo', root / 'final.png', alignment='identity', sample_mask=root / 'sample.png')
            result = load_image(root / 'final.png')
            self.assertEqual(result.getpixel((5, 5)), original.getpixel((5, 5)))
            self.assertEqual(result.getpixel((60, 50)), (220, 210, 200, 255))
            self.assertLess(report['matching']['sample_mae_after'], report['matching']['sample_mae_before'])

    def test_same_photo_donor_stroke_repair_and_salvage_end_to_end(self):
        api = self.api()
        from glyph_sample import sample, place
        from photo_finish import strokes_paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = Image.new('RGBA', (160, 140), (180, 180, 180, 255))
            draw = ImageDraw.Draw(original)
            draw.rectangle((20, 20, 23, 40), fill=(20, 20, 20, 255))
            draw.rectangle((70, 60, 90, 63), fill=(20, 20, 20, 255))
            original.save(root / 'source.png')
            task = prepare(root / 'source.png', [60, 50, 50, 50], 'Replace horizontal stroke with donor vertical stroke', root / 'task')
            sample(root / 'source.png', [15, 15, 15, 30], root / 'donor.png')
            place(root / 'donor.png', task, [[65, 55], [80, 55], [80, 85], [65, 85]], root / 'placed.png')
            old = Image.new('L', original.size)
            ImageDraw.Draw(old).rectangle((69, 59, 91, 64), fill=255)
            old.save(root / 'old.png')
            strokes_paths(root / 'task/original.png', root / 'placed.png', root / 'old.png', root / 'placed.stroke-mask.png', root / 'repair.png')
            report = api.run(task, root / 'repair.png', 'photo', root / 'final.png', alignment='identity', edit_mask=root / 'repair.stroke-mask.png')
            final = load_image(root / 'final.png')
            self.assertLess(final.getpixel((71, 70))[0], 40)
            self.assertGreater(final.getpixel((87, 61))[0], 160)
            self.assertEqual(final.getpixel((21, 25)), original.getpixel((21, 25)))
            self.assertEqual(report['verification']['changed_outside_allowed'], 0)


if __name__ == '__main__':
    unittest.main()
