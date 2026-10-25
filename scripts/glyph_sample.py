"""Explicit same-photo stroke sampling and placement. No OCR, font identification or AI.

All coordinates use the EXIF-normalized photo. Outputs never replace existing files.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

from photo_patch import check_rect, load_image, new_output, pixel_hash, quad_transform, read_task


LIMITATIONS = [
    'No semantic recognition, OCR, font identification, or new character synthesis.',
    'Automatic extraction assumes locally smooth background and contrasting strokes; inspect the PNG.',
    'Texture, shadows, broad strokes and overlapping marks may require an explicit mask.',
    'Sampled RGB retains edge/background contamination; no foreground-color unmatting is performed.',
    'Extraction alpha can lighten donor edges; an explicit mask alone does not remove background RGB contamination.',
    'Inspect the actual composite against the target background, especially stroke edges.',
    'Edge measurements are image heuristics, not a camera-blur or font estimate.',
    'Placement does not erase old strokes or decide protection; the downstream compositor controls protection.',
]


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _outputs(paths):
    resolved = []
    for path in paths:
        if Path(path).is_symlink():
            raise ValueError(f'Output is an existing symlink: {path}')
        resolved.append(new_output(path))
    if len(set(resolved)) != len(resolved):
        raise ValueError('Output paths must be distinct')
    return resolved


def _write_artifacts(items):
    """Exclusive creation also protects against a collision after preflight."""
    created = []
    try:
        for path, content in items:
            with path.open('xb') as destination:
                created.append(path)
                if isinstance(content, Image.Image):
                    content.save(destination, format='PNG')
                else:
                    destination.write(json.dumps(content, ensure_ascii=False, indent=2).encode('utf-8'))
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _explicit_mask(path, size):
    # A file with a true alpha channel uses alpha. An opaque RGB or gray file
    # uses luminance. This permits black RGB pixels with explicit nonzero alpha.
    with Image.open(path) as source:
        has_alpha = 'A' in source.getbands() or 'transparency' in source.info
    image = load_image(path)
    if image.size != size:
        raise ValueError('Mask must have the full normalized source-photo dimensions; no resizing')
    return image.getchannel('A') if has_alpha else image.convert('L')


def _extract(crop, polarity):
    luminance = crop.convert('L')
    # Bound the expensive rank filter to at most 1024 x 1024. The native photo
    # and native alpha remain unscaled, and total inputs are capped at 40 MP.
    working = luminance.copy()
    working.thumbnail((1024, 1024), Image.Resampling.BILINEAR)
    kernel = min(31, max(3, (min(working.size) // 2) | 1))
    estimate = working.filter(ImageFilter.MaxFilter(kernel) if polarity == 'dark'
                              else ImageFilter.MinFilter(kernel)).filter(ImageFilter.GaussianBlur(2))
    background = estimate.resize(crop.size, Image.Resampling.BILINEAR)
    contrast = (ImageChops.subtract(background, luminance) if polarity == 'dark'
                else ImageChops.subtract(luminance, background))
    peak = contrast.getextrema()[1]
    threshold = max(8, round(peak * .12))
    if peak <= threshold:
        raise ValueError('No adequate contrasting foreground; use a tighter donor rectangle or explicit mask')
    return contrast.point(lambda value: max(0, min(255, round((value-threshold)*255/(peak-threshold)))))


def _analyze(crop, alpha):
    histogram = alpha.histogram()
    foreground_count = sum(histogram[32:])
    background_count = sum(histogram[:9])
    if foreground_count < 8 or background_count < 8:
        raise ValueError('Donor needs at least 8 foreground and 8 background pixels')
    foreground = alpha.point(lambda value: 255 if value >= 32 else 0)
    background = alpha.point(lambda value: 255 if value <= 8 else 0)
    rgb = crop.convert('RGB')
    edges = crop.convert('L').filter(ImageFilter.FIND_EDGES)
    nonzero = sum(histogram[1:])
    return dict(stroke_coverage=sum(i * count for i, count in enumerate(histogram)) / (255 * crop.width * crop.height),
                foreground_pixels=foreground_count, background_pixels=background_count,
                stroke_bbox=list(alpha.getbbox()),
                stroke_rgb_mean=[round(v, 3) for v in ImageStat.Stat(rgb, foreground).mean],
                background_rgb_mean=[round(v, 3) for v in ImageStat.Stat(rgb, background).mean],
                edge_heuristic=dict(mean_stroke_luma_edge=round(ImageStat.Stat(edges, foreground).mean[0], 3),
                                    mixed_alpha_fraction=round(sum(histogram[1:255]) / nonzero, 6),
                                    interpretation='Relative image/alpha heuristic only; not calibrated blur or font identification'))


def sample(image_path, rect, output, polarity='dark', mask_path=None):
    """Extract a user-selected donor rectangle; return report and write PNG + .sample.json.

    Rect is [x,y,width,height]. Explicit masks cover the full normalized source;
    alpha-channel masks use alpha, otherwise grayscale luminance is stroke alpha.
    """
    if polarity not in ('dark', 'light'):
        raise ValueError('Polarity must be dark or light')
    output = Path(output)
    if output.suffix.lower() != '.png':
        raise ValueError('Donor output must be PNG')
    output, report_path = _outputs([output, output.with_suffix('.sample.json')])
    image = load_image(image_path)
    x, y, width, height = check_rect(rect, image.size)
    crop = image.crop((x, y, x + width, y + height))
    if mask_path is not None:
        full_mask = _explicit_mask(mask_path, image.size)
        alpha = full_mask.crop((x, y, x + width, y + height))
        del full_mask
    else:
        alpha = _extract(crop, polarity)
    alpha = ImageChops.multiply(alpha, crop.getchannel('A'))
    analysis = _analyze(crop, alpha)
    crop.putalpha(alpha)
    report = dict(format='patchlock-glyph-sample', version=1, donor_rect=list(rect),
                  source_file_sha256=_file_hash(image_path), source_rgba_sha256=pixel_hash(image),
                  source_size=list(image.size), polarity=polarity,
                  method='explicit-mask' if mask_path is not None else 'local-smooth-background',
                  output=str(output), limitations=LIMITATIONS, **analysis)
    if mask_path is not None:
        report['mask_file_sha256'] = _file_hash(mask_path)
    _write_artifacts([(output, crop), (report_path, report)])
    return report


def _inverse(t):
    a, b, c, d, e, f, g, h = t
    cofactors = [e-f*h, c*h-b, b*f-c*e, f*g-d, a-c*g, c*d-a*f,
                 d*h-e*g, b*g-a*h, a*e-b*d]
    determinant = a*cofactors[0] + b*cofactors[3] + c*cofactors[6]
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError('Degenerate perspective mapping')
    # Keep all nine entries: h33 may be zero for a valid inverse whose
    # denominator is finite throughout the target quad. Normalize only after
    # translating into the local output patch coordinates.
    return [v / determinant for v in cofactors]


def place(glyph_path, task_path, quad, output):
    """Map entire donor canvas into target-photo TL/TR/BR/BL quad within task.rect.

    Writes a native-size transparent candidate and .stroke-mask.png. Old strokes
    are not erased; compose separately with the required edit/protection masks.
    """
    output = Path(output)
    if output.suffix.lower() != '.png':
        raise ValueError('Candidate output must be PNG')
    output, mask_output = _outputs([output, output.with_name(output.stem + '.stroke-mask.png')])
    task, original = read_task(task_path)
    size = original.size
    del original
    transform = quad_transform(quad)
    x, y, width, height = task['rect']
    if any(px < x or px > x + width or py < y or py > y + height for px, py in quad):
        raise ValueError('Target quad must lie wholly inside task rectangle')
    glyph = load_image(glyph_path)
    alpha = glyph.getchannel('A')
    if alpha.getbbox() is None or alpha.histogram()[0] < 8:
        raise ValueError('Donor must have nonempty strokes and transparent background')
    inverse = _inverse(transform)
    left = max(x, math.floor(min(p[0] for p in quad)))
    top = max(y, math.floor(min(p[1] for p in quad)))
    right = min(x + width, math.ceil(max(p[0] for p in quad)))
    bottom = min(y + height, math.ceil(max(p[1] for p in quad)))
    # Inverse maps photo coordinates to unit donor coordinates; shift the local
    # output origin and then scale the source axes to native donor pixels.
    a, b, c, d, e, f, g, h, i = inverse
    denominators = [g * px + h * py + i for px, py in quad]
    if (any(not math.isfinite(value) or abs(value) < 1e-12 for value in denominators)
            or min(denominators) < 0 < max(denominators)):
        raise ValueError('Singular perspective mapping within target quad')
    # A pole at the bounding-box origin can lie outside a valid target quad.
    # Add at most one pixel of left/top padding to choose a finite local origin;
    # polygon clipping constrains support and paste safely crops negative origins.
    for dx, dy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
        local_left, local_top = left + dx, top + dy
        z = g * local_left + h * local_top + i
        if math.isfinite(z) and abs(z) >= 1e-12:
            left, top = local_left, local_top
            break
    else:
        raise ValueError('Singular perspective mapping')
    coefficients = [a*glyph.width/z, b*glyph.width/z, (a*left+b*top+c)*glyph.width/z,
                    d*glyph.height/z, e*glyph.height/z, (d*left+e*top+f)*glyph.height/z,
                    g/z, h/z]
    patch = glyph.transform((right-left, bottom-top), Image.Transform.PERSPECTIVE, coefficients,
                            resample=Image.Resampling.BILINEAR)
    clip = Image.new('L', patch.size, 0)
    ImageDraw.Draw(clip).polygon([(px-left, py-top) for px, py in quad], fill=255)
    patch.putalpha(ImageChops.multiply(patch.getchannel('A'), clip))
    candidate = Image.new('RGBA', size, (0, 0, 0, 0))
    candidate.paste(patch, (left, top))
    stroke_mask = candidate.getchannel('A')
    if stroke_mask.getbbox() is None:
        raise ValueError('Target quad produces no visible stroke pixels')
    report = dict(format='patchlock-glyph-placement', version=1, frame='photo', quad=quad,
                  output=str(output), stroke_mask=str(mask_output), stroke_bbox=list(stroke_mask.getbbox()),
                  limitations=LIMITATIONS)
    _write_artifacts([(output, candidate), (mask_output, stroke_mask)])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    sampler = commands.add_parser('sample', help='Isolate explicit donor strokes into transparent PNG')
    sampler.add_argument('--image', required=True)
    sampler.add_argument('--rect', nargs=4, type=int, required=True, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'))
    sampler.add_argument('--output', required=True)
    sampler.add_argument('--polarity', choices=('dark', 'light'), default='dark')
    sampler.add_argument('--mask', help='Full normalized source-size alpha or gray stroke mask')
    placer = commands.add_parser('place', help='Place donor in target-photo quad; compositor controls protection')
    placer.add_argument('--glyph', required=True)
    placer.add_argument('--task', required=True)
    placer.add_argument('--quad', required=True, help='JSON file with target-photo TL TR BR BL points')
    placer.add_argument('--output', required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == 'sample':
            report = sample(arguments.image, arguments.rect, arguments.output, arguments.polarity, arguments.mask)
        else:
            quad = json.loads(Path(arguments.quad).read_text(encoding='utf-8'))
            report = place(arguments.glyph, arguments.task, quad, arguments.output)
        print(json.dumps(report, ensure_ascii=False))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
