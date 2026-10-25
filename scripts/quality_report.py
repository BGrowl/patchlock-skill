"""Offline scope evidence and limited image diagnostics; never certifies naturalness.

Pillow is the only image dependency. No OCR, model calls or external resources.
"""
import argparse
import base64
from functools import reduce
from html import escape
from io import BytesIO
import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from photo_patch import check_rect, load_image, load_mask, quad_transform, verify


PREVIEW_EDGE = 1200
LIMITS = [
    'Pixel scope compares decoded EXIF-normalized RGBA pixels, including alpha; it does not compare file bytes or metadata.',
    'Low diagnostic values do not prove naturalness, correct text, correct objects, convincing perspective or successful editing.',
    'Boundary, sharpness and texture statistics are descriptive heuristics, not calibrated acceptance tests. Inspect enlarged original and result crops.',
    'Text is unassessed without caller-supplied independent observation; literal comparison is not OCR and cannot verify that observation.',
    'Old-stroke ghosting, protected neighboring details, lighting, typeface and local deformation always need visual review.',
]


def _max_channel(image):
    return reduce(ImageChops.lighter, image.split())


def _mean(image, mask):
    return ImageStat.Stat(image, mask=mask).mean[0]


def _alignment(path):
    if path is None:
        return dict(status='not_supplied', provenance='unverified_not_bound_to_result')
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('frame') not in ('reference', 'photo', 'patch'):
        raise ValueError('alignment report must be an object with a recognized frame')
    quad = data.get('quad')
    if (not isinstance(quad, list) or len(quad) != 4 or
            any(not isinstance(p, list) or len(p) != 2 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in quad)):
        raise ValueError('alignment quad must contain four finite coordinate pairs')
    quad_transform(quad)
    counts = [data.get(key) for key in ('matches', 'inliers', 'nearby_inliers')]
    if any(type(v) is not int or v < 0 for v in counts) or not counts[2] <= counts[1] <= counts[0]:
        raise ValueError('alignment counts must satisfy 0 <= nearby_inliers <= inliers <= matches')
    error = data.get('nearby_median_error_reference_px')
    if type(error) not in (int, float) or not math.isfinite(error) or error < 0:
        raise ValueError('alignment error must be a finite nonnegative number')
    # Select only geometric fields; paths, arbitrary metadata and unverified hashes
    # from another run must not appear as evidence binding this report to a result.
    diagnostics = dict(frame=data['frame'], quad=quad, matches=counts[0], inliers=counts[1],
                       nearby_inliers=counts[2], nearby_median_error_reference_px=error)
    return dict(status='supplied_geometric_diagnostics_only', diagnostics=diagnostics,
                provenance='unverified_not_bound_to_result',
                limit='The supplied geometry was shape-validated only. No provenance or image binding is verified; this may describe a stale candidate and does not validate this result.')


def _boundary(original, result, mask):
    """Compare each internal 4-neighbor mask-crossing pair to its original pair."""
    count = total_increase = total_original = total_result = above = 0
    maximum = 0
    w, h = mask.size
    for dx, dy in ((1, 0), (0, 1)):
        if w <= dx or h <= dy:
            continue
        a, b = (0, 0, w-dx, h-dy), (dx, dy, w, h)
        crossing = ImageChops.difference(mask.crop(a), mask.crop(b))
        n = crossing.histogram()[255]
        if not n:
            continue
        before = _max_channel(ImageChops.difference(original.crop(a), original.crop(b)))
        after = _max_channel(ImageChops.difference(result.crop(a), result.crop(b)))
        increase = ImageChops.subtract(after, before)
        histogram = increase.histogram(mask=crossing)
        count += n
        total_original += _mean(before, crossing) * n
        total_result += _mean(after, crossing) * n
        total_increase += sum(i * v for i, v in enumerate(histogram))
        above += sum(histogram[17:])
        maximum = max(maximum, max(i for i, v in enumerate(histogram) if v))
    mean = total_increase / count if count else None
    fraction = above / count if count else None
    return dict(status='measured' if count else 'unassessed_no_internal_mask_boundary',
                boundary_pairs=count,
                original_mean_jump_rgba=total_original / count if count else None,
                result_mean_jump_rgba=total_result / count if count else None,
                mean_increase_rgba=mean, max_increase_rgba=maximum if count else None,
                fraction_increase_gt_16=fraction,
                review_flag=bool(count and (mean > 8 or fraction > .1)),
                review_thresholds=dict(mean_increase_gt=8, fraction_increase_gt_16_gt=.1),
                limit='Positive increase in maximum RGBA neighbor difference across internal allowed-mask edges, in 0-255 channel units. Existing original edges are subtracted pair by pair. Intended edges can trigger this; low values cannot establish seamlessness. Image-frame edges have no external neighbor and are excluded.')


def _appearance(original, result, mask):
    def residual(image):
        gray = image.convert('L')
        return ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(1)))
    before, after = residual(original), residual(result)
    a, b = ImageStat.Stat(before, mask=mask), ImageStat.Stat(after, mask=mask)
    sharpness = dict(original_mean_abs_highpass=a.mean[0], result_mean_abs_highpass=b.mean[0],
                     result_to_original_ratio=b.mean[0] / a.mean[0] if a.mean[0] else None,
                     limit='Mean absolute luminance residual after a 1 px Gaussian blur within allowed pixels; content edges, font strokes, scale and blur all influence this. A zero original gives no ratio. No calibrated pass threshold.')
    texture = dict(original_highpass_stddev=a.stddev[0], result_highpass_stddev=b.stddev[0],
                   limit='Standard deviation of the absolute 1 px highpass luminance residual within allowed pixels, in 0-255 units. Not a noise estimator or texture-matching proof; content and boundaries confound it. No calibrated pass threshold.')
    return sharpness, texture


def _preview(image):
    small = image.copy()
    small.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    small.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii'), list(small.size)


def _html(report, previews):
    cards = []
    for title, image in previews:
        uri, size = _preview(image)
        cards.append(f'<figure><figcaption>{escape(title)} — embedded {size[0]} × {size[1]} px</figcaption>'
                     f'<img src="{uri}" alt="{escape(title, quote=True)}"></figure>')
    encoded = escape(json.dumps(report, ensure_ascii=False, indent=2))
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>PatchLock local evidence</title><style>'
            'body{font:16px system-ui,sans-serif;margin:24px;max-width:1450px;color:#202124;background:#f4f5f7}'
            'figure{margin:24px 0;padding:16px;background:#fff;overflow:auto}figcaption{margin-bottom:12px;font-weight:600}'
            'img{display:block;min-width:min(100%,900px);max-width:100%;height:auto;image-rendering:auto}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px}p{line-height:1.5}'
            '</style><h1>PatchLock local evidence</h1>'
            f'<p><strong>Overall status: {escape(report["status"])}</strong>. '
            f'Pixel scope: {"PASS" if report["scope_pass"] else "FAIL"}. '
            'Scope evidence and visual quality are separate conclusions. Naturalness, text and ghosting still require review.</p>'
            '<p>Original, result and binary RGBA change crops are enlarged for inspection. '
            f'Each embedded preview is capped at {PREVIEW_EDGE} px on its longest edge; '
            'larger crops are downsampled. Browser enlargement does not restore pixel detail. '
            'Transparent pixels use the page background; consult RGBA scope counts for alpha-only changes.</p>'
            + ''.join(cards) + '<h2>Measurements and limits</h2><pre>' + encoded + '</pre></html>')


def assess(original_path, result_path, allowed_mask_path, output, rect=None,
           alignment_report=None, expected_text=None, observed_text=None):
    """Write a new JSON report plus same-stem offline HTML, returning the report.

    ``alignment_report`` is a JSON file path, never an in-memory dictionary.
    Caller-supplied observed text must come from independent human/OCR observation;
    this function neither reads text nor verifies the observation's independence.
    """
    output = Path(output).resolve()
    if output.suffix.lower() != '.json':
        raise ValueError('quality output must use a new .json filename')
    html_output = output.with_suffix('.html')
    inputs = [Path(p).resolve() for p in (original_path, result_path, allowed_mask_path)]
    if alignment_report is not None:
        inputs.append(Path(alignment_report).resolve())
    for target in (output, html_output):
        if target in inputs or target.exists():
            raise ValueError('report output collides with an input or existing file; choose a new filename')
    if any(v is not None and not isinstance(v, str) for v in (expected_text, observed_text)):
        raise ValueError('expected_text and observed_text must be strings or None')
    if observed_text is not None and expected_text is None:
        raise ValueError('observed_text requires expected_text for a literal comparison')
    alignment = _alignment(alignment_report)
    original, result = load_image(original_path), load_image(result_path)
    if original.size != result.size:
        raise ValueError('result size does not match original')
    allowed = load_mask(allowed_mask_path, original.size).point(lambda v: 255 if v else 0)
    bbox = allowed.getbbox()
    if bbox is None:
        raise ValueError('allowed mask is empty')
    # Deliberately reuse the core RGBA scope verifier rather than a quality proxy.
    scope = verify(original_path, result_path, allowed_mask_path)
    scope['changed_bbox'] = list(scope['changed_bbox']) if scope['changed_bbox'] else None
    focus = check_rect(rect, original.size) if rect is not None else (bbox[0], bbox[1], bbox[2]-bbox[0], bbox[3]-bbox[1])
    x, y, w, h = focus
    margin = max(16, round(max(w, h) * .25))
    crop = [max(0, x-margin), max(0, y-margin), min(original.width, x+w+margin), min(original.height, y+h+margin)]
    delta = _max_channel(ImageChops.difference(original, result))
    sharpness, texture = _appearance(original, result, allowed)
    text = dict(status='manual_review_required', expected_text=expected_text,
                observed_text=observed_text, method='unassessed_no_ocr', literal_match=None)
    if observed_text is not None:
        match = observed_text == expected_text
        text.update(status='caller_supplied_match' if match else 'caller_supplied_mismatch',
                    method='caller_supplied_literal_comparison_not_ocr', literal_match=match)
    text['limit'] = 'No text recognition is performed. Caller-supplied comparison is literal, including whitespace/case, and does not verify observation provenance, glyph appearance or ghosting.'
    failed = not scope['pixel_scope_pass'] or text['literal_match'] is False
    report = dict(format='patchlock-quality-report', version=1,
                  status='fail' if failed else 'review_required', scope_pass=scope['pixel_scope_pass'],
                  dimensions=list(original.size), scope=scope,
                  warnings=['no_op'] if not (scope['changed_inside_allowed'] + scope['changed_outside_allowed']) else [],
                  diagnostics=dict(change=dict(
                      changed_fraction_allowed=scope['changed_inside_allowed'] / scope['allowed_pixels'],
                      mean_max_rgba_delta_allowed=_mean(delta, allowed),
                      limit='Magnitude and coverage of RGBA change within allowed pixels; no semantic or visual success threshold.'),
                      boundary_seam=_boundary(original, result, allowed), sharpness=sharpness, texture=texture),
                  text=text, ghosting=dict(status='visual_review_required',
                      limit='Old strokes or doubled edges cannot be reliably ruled out by these statistics.'),
                  alignment=alignment,
                  preview=dict(focus_rect=list(focus), crop_box_ltrb=crop, longest_edge_cap=PREVIEW_EDGE,
                               downsampled=max(crop[2]-crop[0], crop[3]-crop[1]) > PREVIEW_EDGE,
                               diff='binary: white means any RGBA channel changed'),
                  limits=list(LIMITS))
    previews = [('Original local crop', original.crop(crop)), ('Result local crop', result.crop(crop)),
                ('Binary RGBA change crop', delta.point(lambda v: 255 if v else 0).crop(crop))]
    json_text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    html_text = _html(report, previews)
    # Preflight both names before touching either, then use exclusive opens to
    # handle races. On a failure remove only files created by this invocation.
    output.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, content in ((output, json_text), (html_output, html_text)):
            with path.open('x', encoding='utf-8') as stream:
                created.append(path)
                stream.write(content)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('original', 'result', 'allowed-mask', 'output'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--rect', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'))
    parser.add_argument('--alignment-report')
    parser.add_argument('--expected-text')
    parser.add_argument('--observed-text')
    args = parser.parse_args()
    try:
        report = assess(args.original, args.result, args.allowed_mask, args.output,
                        args.rect, args.alignment_report, args.expected_text, args.observed_text)
        print(json.dumps(report, ensure_ascii=False))
        if report['status'] == 'fail':
            raise SystemExit(2)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
