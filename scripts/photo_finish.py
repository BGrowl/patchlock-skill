"""Optional local photographic matching and mask-controlled old-stroke repair.

No semantic generation, font recognition or recovered detail. Matching needs a
confirmed unchanged background sample; inpainting is suitable for narrow strokes.
"""
import argparse
import json
import math
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter
from photo_patch import load_image, load_mask, new_output, pixel_hash, save_png, MAX_PIXELS


def numpy_module():
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError('照片质感工具需要本地 NumPy；基础回填仍可使用，不需要 API Key') from exc
    return np


def check_images(*images):
    if not images or any(im.size != images[0].size for im in images):
        raise ValueError('图片和蒙版必须采用相同的归一化原生尺寸，不能自动拉伸')
    if images[0].width * images[0].height > MAX_PIXELS:
        raise ValueError('照片超过 4000 万像素')


def mask_array(mask):
    np = numpy_module()
    gray = np.asarray(mask.convert('L'), dtype=np.uint8)
    if 'A' in mask.getbands():
        gray = ((gray.astype(np.uint16) * np.asarray(mask.getchannel('A')) + 254) // 255).astype(np.uint8)
    return gray


def align_color_profile(reference, candidate, assume_untagged_srgb):
    """Convert candidate into reference encoding, never just relabel RGB bytes."""
    reference_icc, candidate_icc = reference.info.get('icc_profile'), candidate.info.get('icc_profile')
    if reference_icc == candidate_icc:
        if reference_icc:
            from PIL import ImageCms
            try:
                ImageCms.ImageCmsProfile(BytesIO(reference_icc))
            except (OSError, ValueError, ImageCms.PyCMSError) as exc:
                raise ValueError('嵌入 ICC 配置无效；相同字节不代表有效配置') from exc
        return candidate, dict(status='same_profile_encoding', assumed_untagged_srgb=False,
                               limit='Identical valid/missing ICC metadata is treated as a shared RGB encoding, not measured color fidelity.')
    if (not reference_icc or not candidate_icc) and not assume_untagged_srgb:
        raise ValueError('ICC 信息不同且有未标记图片；仅确认未标记图为 sRGB 后使用 --assume-untagged-srgb')
    from PIL import ImageCms
    try:
        reference_profile = ImageCms.ImageCmsProfile(BytesIO(reference_icc)) if reference_icc else ImageCms.createProfile('sRGB')
        candidate_profile = ImageCms.ImageCmsProfile(BytesIO(candidate_icc)) if candidate_icc else ImageCms.createProfile('sRGB')
        converted = ImageCms.profileToProfile(candidate.convert('RGB'), candidate_profile,
                                             reference_profile, outputMode='RGB').convert('RGBA')
    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
        raise ValueError('ICC 配置无法转换；不能将无效配置直接当作 sRGB') from exc
    converted.putalpha(candidate.convert('RGBA').getchannel('A'))
    converted.info.update(candidate.info)
    if reference_icc:
        converted.info['icc_profile'] = reference_icc
    else:
        converted.info.pop('icc_profile', None)
    return converted, dict(status='candidate_converted_to_reference_encoding',
                           assumed_untagged_srgb=not reference_icc or not candidate_icc,
                           limit='Missing ICC is assumed sRGB only by explicit caller choice; sample errors are measured after color conversion.')


def match_photo(reference, candidate, sample_mask, blur_radius=0, grain=0, assume_untagged_srgb=False, tone_mode='additive'):
    """Robust bounded RGB correction + smooth residual illumination plane.

    Blur and stochastic grain are explicit caller choices, never auto-inferred
    as proof of the desired text's sharpness or the camera's true noise model.
    """
    np = numpy_module()
    check_images(reference, candidate, sample_mask)
    if tone_mode not in ('additive', 'affine'):
        raise ValueError('tone_mode 必须是 additive 或 affine')
    if not all(math.isfinite(v) for v in (blur_radius, grain)) or not 0 <= blur_radius <= 3 or not 0 <= grain <= 1:
        raise ValueError('blur_radius 必须为 0–3，grain 必须为 0–1 的有限数值')
    candidate, color_management = align_color_profile(reference, candidate, assume_untagged_srgb)
    ref_rgba, can_rgba = reference.convert('RGBA'), candidate.convert('RGBA')
    r, c = np.asarray(ref_rgba), np.asarray(can_rgba)
    selected = mask_array(sample_mask) > 127
    if np.any(selected & ((r[:, :, 3] != 255) | (c[:, :, 3] != 255))):
        raise ValueError('背景样本必须在原图和候选中都完全不透明')
    yy, xx = np.nonzero(selected)
    if len(xx) < 64:
        raise ValueError('至少需要 64 个确认未改变的背景样本像素')
    # Deterministic bounded fit, avoiding a 40MP floating-point image allocation.
    stride = max(1, math.ceil(len(xx) / 50000))
    yy, xx = yy[::stride], xx[::stride]
    rs, cs = r[yy, xx, :3].astype(float), c[yy, xx, :3].astype(float)
    before = float(np.abs(rs - cs).mean())
    normx = xx / max(1, reference.width - 1) - .5
    normy = yy / max(1, reference.height - 1) - .5
    plane_basis = np.column_stack([np.ones(len(xx)), normx, normy])
    use_plane = np.ptp(normx) >= .2 and np.ptp(normy) >= .2 and np.linalg.matrix_rank(plane_basis) == 3
    gains, offsets, planes = [], [], []
    prediction = np.empty_like(rs)
    for channel in range(3):
        cv, rv = cs[:, channel], rs[:, channel]
        centered = cv - np.median(cv)
        gain = (float(np.dot(centered, rv - np.median(rv)) / np.dot(centered, centered))
                if tone_mode == 'affine' and np.std(cv) > 2 else 1.)
        offset = float(np.median(rv - gain * cv))
        residual = rv - (gain * cv + offset)
        median = np.median(residual)
        keep = np.abs(residual - median) <= max(3., 4 * float(np.median(np.abs(residual - median))))
        if keep.sum() < max(48, len(cv) * .6):
            raise ValueError('背景样本不一致，请排除文字、物体和重绘过的纹理')
        plane = np.linalg.lstsq(plane_basis[keep], residual[keep], rcond=None)[0] if use_plane else np.array([np.median(residual[keep]), 0., 0.])
        if not .5 <= gain <= 2 or abs(offset + plane[0]) > 64 or max(abs(plane[1]), abs(plane[2])) > 64:
            raise ValueError('样本需要过大的颜色或光照修正；请检查配准和背景样本')
        prediction[:, channel] = gain * cv + offset + plane_basis @ plane
        gains.append(gain); offsets.append(offset); planes.append(plane.tolist())
    fit_mae = float(np.abs(rs - prediction).mean())
    if fit_mae > 12 or fit_mae > before + .5:
        raise ValueError('样本无法由可靠的颜色/平滑光照变换解释，不能强行匹配')
    output = can_rgba.copy()
    for top in range(0, candidate.height, 128):
        bottom = min(candidate.height, top + 128)
        block = c[top:bottom, :, :3].astype(float)
        by = np.arange(top, bottom)[:, None] / max(1, candidate.height - 1) - .5
        bx = np.arange(candidate.width)[None, :] / max(1, candidate.width - 1) - .5
        for channel in range(3):
            p = planes[channel]
            block[:, :, channel] = block[:, :, channel] * gains[channel] + offsets[channel] + p[0] + p[1] * bx + p[2] * by
        rgba = c[top:bottom].copy()
        rgba[:, :, :3] = np.clip(np.rint(block), 0, 255).astype('uint8')
        output.paste(Image.fromarray(rgba), (0, top))
    def sample_error(image):
        before_sum = after_sum = count = 0
        for top in range(0, image.height, 128):
            bottom = min(image.height, top + 128)
            support = selected[top:bottom]
            if support.any():
                rb = r[top:bottom, :, :3][support].astype(float)
                cb = c[top:bottom, :, :3][support].astype(float)
                ob = np.asarray(image.crop((0, top, image.width, bottom)))[:, :, :3][support].astype(float)
                before_sum += float(np.abs(rb - cb).sum())
                after_sum += float(np.abs(rb - ob).sum())
                count += int(support.sum()) * 3
        return before_sum / count, after_sum / count
    # Validate every selected background pixel, not merely the bounded fit
    # sample: regularly spaced sampling can otherwise hide structured changes.
    full_before, full_fit_mae = sample_error(output)
    if full_fit_mae > 12 or full_fit_mae > full_before + .5:
        raise ValueError('完整背景样本与拟合不一致，请排除重绘区域或检查配准')
    if blur_radius:
        blurred = output.convert('RGBa').filter(ImageFilter.GaussianBlur(blur_radius)).convert('RGBA')
        blurred.putalpha(can_rgba.getchannel('A'))
        output = blurred
    grain_sigma = [0., 0., 0.]
    if grain:
        # This is a matched amplitude hint, not authentic sensor-noise recovery.
        smooth = np.asarray(ref_rgba.convert('RGB').filter(ImageFilter.GaussianBlur(1)))
        residual = rs - smooth[yy, xx, :3].astype(float)
        grain_sigma = np.minimum(np.std(residual, axis=0), 10).tolist()
        rng = np.random.default_rng(0)
        for top in range(0, output.height, 128):
            bottom = min(output.height, top + 128)
            rgba = np.array(output.crop((0, top, output.width, bottom)))
            noise = rng.normal(size=rgba[:, :, :3].shape) * np.array(grain_sigma) * grain
            rgba[:, :, :3] = np.clip(np.rint(rgba[:, :, :3].astype(float) + noise), 0, 255).astype('uint8')
            output.paste(Image.fromarray(rgba), (0, top))
    _, after = sample_error(output)
    output.info.update(candidate.info)
    return output, dict(sample_pixels=int(selected.sum()), fit_sample_pixels=len(xx), rgb_gain=gains,
                        rgb_offset=offsets, illumination_plane=planes, sample_mae_before=full_before,
                        fit_mae=fit_mae, full_sample_fit_mae=full_fit_mae, sample_mae_after=after, blur_radius=blur_radius, grain=grain,
                        grain_sigma_rgb=grain_sigma, color_management=color_management, tone_mode=tone_mode, limits=[
                            '仅使用人工确认的未改背景拟合，不识字、不找字体、不重建曲面。',
                            'additive 默认只加平滑颜色/光照修正；affine 需要明确选择，单色背景增益外推可能使前景偏色。',
                            '模糊与颗粒为显式局部效果；样本改善不证明字形、接缝或视觉自然。'])


def repair_strokes(original, replacement, old_mask, new_mask, protect_mask=None, radius=3):
    """Inpaint old support and alpha-place replacement; untouched pixels stay exact."""
    np = numpy_module()
    try:
        import cv2
    except ImportError as exc:
        raise ValueError('旧笔画修复需要本地 OpenCV；也可由图像编辑工具提供干净背景候选') from exc
    check_images(original, replacement, old_mask, new_mask, *([protect_mask] if protect_mask is not None else []))
    if not isinstance(radius, (int, float)) or not math.isfinite(radius) or not 1 <= radius <= 15:
        raise ValueError('修复半径必须为 1–15 的有限数值')
    source, rep = np.array(original.convert('RGBA')), np.array(replacement.convert('RGBA'))
    protected = mask_array(protect_mask) > 0 if protect_mask is not None else np.zeros(source.shape[:2], bool)
    old = (mask_array(old_mask) > 0) & ~protected
    # new_mask declares stroke support; replacement alpha encodes coverage once.
    # A donor's own alpha is a valid support mask, without squaring antialiasing.
    new_alpha = np.where(mask_array(new_mask) > 0, rep[:, :, 3], 0).astype('uint8')
    new_alpha[protected] = 0
    support = old | (new_alpha > 0)
    if not support.any():
        raise ValueError('旧笔画与新笔画的有效蒙版均为空')
    if np.any(old & (source[:, :, 3] != 255)):
        raise ValueError('旧笔画修复仅支持不透明照片区域')
    clean = source.copy()
    if old.any():
        # Work on the affected crop + source border to keep memory proportional.
        ys, xs = np.nonzero(old)
        pad = math.ceil(radius * 3) + 4
        x0, x1 = max(0, xs.min() - pad), min(original.width, xs.max() + pad + 1)
        y0, y1 = max(0, ys.min() - pad), min(original.height, ys.max() + pad + 1)
        region_mask = old[y0:y1, x0:x1].astype('uint8') * 255
        if np.all(region_mask) or not np.any(region_mask == 0):
            raise ValueError('旧笔画周围没有可借用的背景，请缩小蒙版或提供修复候选')
        repaired = cv2.inpaint(source[y0:y1, x0:x1, :3], region_mask, radius, cv2.INPAINT_TELEA)
        clean[y0:y1, x0:x1, :3] = repaired
    overlay = Image.fromarray(rep)
    overlay.putalpha(Image.fromarray(new_alpha))
    merged = np.array(Image.alpha_composite(Image.fromarray(clean), overlay))
    # Original canvas alpha is preserved; only effective stroke support is copied.
    merged[:, :, 3] = source[:, :, 3]
    result = source.copy()
    result[support] = merged[support]
    image = Image.fromarray(result)
    image.info.update(original.info)
    return image, Image.fromarray(support.astype('uint8') * 255), dict(
        old_stroke_pixels=int(old.sum()), new_stroke_pixels=int((new_alpha > 0).sum()),
        allowed_pixels=int(support.sum()), protected_pixels=int(protected.sum()), radius=radius,
        limits=['蒙版由调用者确认；旧蒙版须覆盖全部旧笔画与抗锯齿，保护蒙版任意非零都禁止修改。',
                '局部插值适用于窄笔画，不能可靠恢复大面积纹理、反光或遮挡后的真实内容。'])


def match_paths(original_path, candidate_path, sample_mask_path, output, blur_radius=0, grain=0, assume_untagged_srgb=False, tone_mode='additive'):
    output = Path(output).resolve()
    if output.suffix.lower() != '.png':
        raise ValueError('候选输出必须为 PNG')
    report_path = output.with_name(output.stem + '.match.json')
    for path in (output, report_path):
        new_output(path)
    original, candidate = load_image(original_path), load_image(candidate_path)
    mask = load_mask(sample_mask_path, original.size)
    result, report = match_photo(original, candidate, mask, blur_radius, grain, assume_untagged_srgb, tone_mode)
    report.update(original_sha256=pixel_hash(original), candidate_sha256=pixel_hash(candidate),
                  output_sha256=pixel_hash(result))
    save_png(result, output)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def strokes_paths(original_path, replacement_path, old_mask_path, new_mask_path, output, protect_mask_path=None, radius=3):
    output = Path(output).resolve()
    if output.suffix.lower() != '.png':
        raise ValueError('候选输出必须为 PNG')
    mask_path = output.with_name(output.stem + '.stroke-mask.png')
    report_path = output.with_name(output.stem + '.strokes.json')
    for path in (output, mask_path, report_path):
        new_output(path)
    original, replacement = load_image(original_path), load_image(replacement_path)
    old, new = load_image(old_mask_path), load_image(new_mask_path)
    protected = load_image(protect_mask_path) if protect_mask_path else None
    result, mask, report = repair_strokes(original, replacement, old, new, protected, radius)
    report.update(original_sha256=pixel_hash(original), replacement_sha256=pixel_hash(replacement),
                  output_sha256=pixel_hash(result))
    save_png(result, output)
    mask.save(mask_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    match = sub.add_parser('match', help='在确认未改变的背景样本上匹配颜色与平滑光照')
    match.add_argument('--original', required=True); match.add_argument('--candidate', required=True)
    match.add_argument('--sample-mask', required=True); match.add_argument('--output', required=True)
    match.add_argument('--blur-radius', type=float, default=0); match.add_argument('--grain', type=float, default=0)
    match.add_argument('--assume-untagged-srgb', action='store_true', help='仅当确认无 ICC 的图片使用 sRGB 编码时选择')
    match.add_argument('--tone-mode', choices=['additive', 'affine'], default='additive')
    strokes = sub.add_parser('strokes', help='局部修复旧笔画并贴入透明新笔画，输出最小支持蒙版')
    strokes.add_argument('--original', required=True); strokes.add_argument('--replacement', required=True)
    strokes.add_argument('--old-mask', required=True); strokes.add_argument('--new-mask', required=True)
    strokes.add_argument('--protect-mask'); strokes.add_argument('--radius', type=float, default=3)
    strokes.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'match':
            report = match_paths(args.original, args.candidate, args.sample_mask, args.output, args.blur_radius, args.grain, args.assume_untagged_srgb, args.tone_mode)
        else:
            report = strokes_paths(args.original, args.replacement, args.old_mask, args.new_mask, args.output, args.protect_mask, args.radius)
        print(json.dumps(report, ensure_ascii=False))
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
