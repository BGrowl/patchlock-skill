"""Offline evidence-report behavior, independent of semantic image generation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

try:
    from quality_report import assess
except ModuleNotFoundError:
    assess = None


class QualityReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'original.png'
        self.result = self.root / 'result.png'
        self.mask = self.root / 'mask.png'
        self.output = self.root / 'report.json'
        image = Image.new('RGBA', (32, 24), (80, 80, 80, 255))
        image.save(self.original)
        image.save(self.result)
        mask = Image.new('L', image.size, 0)
        mask.paste(255, (10, 8, 22, 16))
        mask.save(self.mask)

    def report(self, **kwargs):
        self.assertIsNotNone(assess, 'quality_report.assess must implement evidence reports')
        return assess(self.original, self.result, self.mask, self.output, **kwargs)

    def edit(self, box, color):
        with Image.open(self.result) as image:
            image.paste(color, box)
            image.save(self.result)

    def test_noop_warns_and_never_overall_passes(self):
        report = self.report()
        self.assertTrue(report['scope_pass'])
        self.assertEqual(report['status'], 'review_required')
        self.assertIn('no_op', report['warnings'])
        self.assertEqual(report['text']['status'], 'manual_review_required')
        self.assertEqual(report['ghosting']['status'], 'visual_review_required')

    def test_outside_change_fails_scope(self):
        self.edit((0, 0, 1, 1), (90, 80, 80, 255))
        report = self.report()
        self.assertFalse(report['scope_pass'])
        self.assertEqual(report['status'], 'fail')
        self.assertEqual(report['scope']['changed_outside_allowed'], 1)

    def test_alpha_only_change_is_detected(self):
        self.edit((0, 0, 1, 1), (80, 80, 80, 200))
        self.assertFalse(self.report()['scope_pass'])

    def test_added_seam_is_boundary_increase(self):
        self.edit((10, 8, 22, 16), (180, 180, 180, 255))
        seam = self.report()['diagnostics']['boundary_seam']
        self.assertGreater(seam['mean_increase_rgba'], 90)
        self.assertTrue(seam['review_flag'])

    def test_existing_boundary_is_not_flagged_as_added_seam(self):
        self.edit((10, 8, 22, 16), (180, 180, 180, 255))
        self.result.replace(self.original)
        with Image.open(self.original) as image:
            image.save(self.result)
        seam = self.report()['diagnostics']['boundary_seam']
        self.assertEqual(seam['mean_increase_rgba'], 0)
        self.assertFalse(seam['review_flag'])

    def test_empty_mask_rejected_without_outputs(self):
        Image.new('L', (32, 24)).save(self.mask)
        with self.assertRaisesRegex(ValueError, 'empty'):
            self.report()
        self.assertFalse(self.output.exists())

    def test_size_mismatch_rejected(self):
        Image.new('RGBA', (5, 5)).save(self.result)
        with self.assertRaises(ValueError):
            self.report()

    def test_text_requires_independent_observation_and_compares_literally(self):
        report = self.report(expected_text='42')
        self.assertEqual(report['text']['status'], 'manual_review_required')
        self.output = self.root / 'compare.json'
        report = self.report(expected_text='42', observed_text='42 ')
        self.assertEqual(report['status'], 'fail')
        self.assertFalse(report['text']['literal_match'])
        self.assertEqual(report['text']['method'], 'caller_supplied_literal_comparison_not_ocr')
        self.output = self.root / 'matching.json'
        self.assertEqual(self.report(expected_text='42', observed_text='42')['status'], 'review_required')

    def test_html_collision_creates_no_partial_json(self):
        self.output.with_suffix('.html').write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.report()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.output.with_suffix('.html').read_text(), 'keep')

    def test_input_collision_is_rejected(self):
        self.output = self.original
        with self.assertRaises(ValueError):
            self.report()

    def test_html_escapes_supplied_text_and_contains_only_embedded_previews(self):
        evil = '<script>alert("x")</script>'
        self.report(expected_text=evil, observed_text=evil)
        page = self.output.with_suffix('.html').read_text(encoding='utf-8')
        self.assertNotIn(evil, page)
        self.assertIn('&lt;script&gt;', page)
        self.assertEqual(page.count('src="data:image/png;base64,'), 3)
        self.assertNotIn('file:', page)
        self.assertNotIn(str(self.root), page)

    def test_schema_hashes_dimensions_and_metrics(self):
        report = self.report(rect=[10, 8, 12, 8])
        self.assertEqual(report['format'], 'patchlock-quality-report')
        self.assertEqual(report['version'], 1)
        self.assertEqual(report['dimensions'], [32, 24])
        self.assertEqual(len(report['scope']['original_rgba_sha256']), 64)
        self.assertIn('sharpness', report['diagnostics'])
        self.assertIn('texture', report['diagnostics'])
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8')), report)
        self.assertTrue(report['limits'])

    def test_alignment_is_validated_and_explicitly_unbound(self):
        path = self.root / 'alignment.json'
        alignment = dict(frame='reference', quad=[[0,0],[32,0],[32,24],[0,24]],
                         matches=30, inliers=20, nearby_inliers=8,
                         nearby_median_error_reference_px=0.5)
        path.write_text(json.dumps(alignment), encoding='utf-8')
        report = self.report(alignment_report=path)
        self.assertEqual(report['alignment']['provenance'], 'unverified_not_bound_to_result')
        self.assertEqual(report['alignment']['diagnostics']['inliers'], 20)
        self.output = self.root / 'bad.json'
        alignment['quad'][0][0] = float('nan')
        path.write_text(json.dumps(alignment), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.report(alignment_report=path)
        self.assertFalse(self.output.exists())

    def test_alignment_counts_rejected(self):
        path = self.root / 'alignment.json'
        path.write_text(json.dumps(dict(frame='photo', quad=[[0,0],[32,0],[32,24],[0,24]],
                          matches=1, inliers=2, nearby_inliers=1,
                          nearby_median_error_reference_px=0)), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.report(alignment_report=path)

    def test_full_image_mask_reports_missing_boundary(self):
        Image.new('L', (32, 24), 255).save(self.mask)
        seam = self.report()['diagnostics']['boundary_seam']
        self.assertEqual(seam['boundary_pairs'], 0)
        self.assertEqual(seam['status'], 'unassessed_no_internal_mask_boundary')
        self.assertIsNone(seam['mean_increase_rgba'])

    def test_mask_dimension_mismatch_rejected_without_partial_files(self):
        Image.new('L', (2, 2), 255).save(self.mask)
        with self.assertRaises(ValueError):
            self.report()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix('.html').exists())

    def test_bad_alignment_frame_rejected(self):
        path = self.root / 'alignment.json'
        path.write_text('{"frame":"unknown"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.report(alignment_report=path)

    def test_observed_without_expected_is_rejected(self):
        with self.assertRaises(ValueError):
            self.report(observed_text='42')

    def test_preview_downsampling_is_declared(self):
        Image.new('RGBA', (1500, 5), (80, 80, 80, 255)).save(self.original)
        Image.new('RGBA', (1500, 5), (80, 80, 80, 255)).save(self.result)
        Image.new('L', (1500, 5), 255).save(self.mask)
        report = self.report()
        self.assertTrue(report['preview']['downsampled'])
        self.assertEqual(report['preview']['longest_edge_cap'], 1200)
        self.assertIn('embedded 1200 × 4 px', self.output.with_suffix('.html').read_text(encoding='utf-8'))

    def test_boundary_alpha_increase_is_visible_in_diagnostics(self):
        self.edit((10, 8, 22, 16), (80, 80, 80, 100))
        report = self.report()
        self.assertTrue(report['scope_pass'])
        self.assertEqual(report['scope']['changed_inside_allowed'], 96)
        self.assertEqual(report['diagnostics']['boundary_seam']['mean_increase_rgba'], 155)

    def test_cli_exit_codes(self):
        script = Path(__file__).with_name('quality_report.py')
        command = [sys.executable, str(script), '--original', str(self.original),
                   '--result', str(self.result), '--allowed-mask', str(self.mask),
                   '--output', str(self.output)]
        process = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('review_required', process.stdout)
        self.output.unlink()
        self.output.with_suffix('.html').unlink()
        process = subprocess.run(command + ['--expected-text', '42', '--observed-text', '43'],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)


if __name__ == '__main__':
    unittest.main()
