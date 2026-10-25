"""Recover only an authorized local edit from an externally edited candidate.

Prepare a photo_patch task first. Auto alignment never falls back silently to
scaling; identity requires an explicit caller assertion of unchanged framing.
"""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageChops
from photo_patch import (read_task, load_image, load_mask, frame_rect, warp_candidate,
                         new_output, pixel_hash, save_png, compose, quad_transform)


def run(task_path, candidate_path, frame, output, alignment='auto', quad=None,
        sample_mask=None, edit_mask=None, protect_mask=None, feather=0,
        blur_radius=0, grain=0, expected_text=None, observed_text=None, assume_untagged_srgb=False, tone_mode='additive'):
    from quality_report import assess
    output = Path(output).resolve()
    if output.suffix.lower() != '.png':
        raise ValueError('最终输出必须是新命名 PNG')
    if alignment not in ('auto', 'identity', 'quad'):
        raise ValueError('alignment 只能是 auto、identity 或 quad')
    if tone_mode not in ('additive', 'affine'):
        raise ValueError('tone_mode 必须是 additive 或 affine')
    if any(value is not None and not isinstance(value, str) for value in (expected_text, observed_text)):
        raise ValueError('expected_text 与 observed_text 必须是字符串')
    if observed_text is not None and expected_text is None:
        raise ValueError('observed_text 需要独立的 expected_text 作比较')
    if not all(math.isfinite(v) for v in (blur_radius, grain)) or not 0 <= blur_radius <= 3 or not 0 <= grain <= 1:
        raise ValueError('blur_radius 必须为 0–3，grain 为 0–1')
    if type(feather) is not int or not 0 <= feather <= 20:
        raise ValueError('羽化必须为 0–20 像素整数')
    if not sample_mask and (blur_radius or grain):
        raise ValueError('模糊/颗粒匹配需要明确的未改背景 sample-mask')
    work = output.with_name(output.stem + '.salvage')
    quality_path = output.with_name(output.stem + '.quality.json')
    workflow_path = output.with_name(output.stem + '.salvage.json')
    # Check the complete artifact set before creating any output or working dir.
    artifacts = [output, output.with_name(output.stem + '.allowed.png'),
                 output.with_name(output.stem + '.verification.json'),
                 output.with_name(output.stem + '.diff.png'), quality_path,
                 quality_path.with_suffix('.html'), workflow_path, work]
    for path in artifacts:
        if path.exists():
            raise ValueError(f'输出已存在，请用新名称：{path}')
    task, original = read_task(task_path)
    candidate = load_image(candidate_path)
    frame_box = frame_rect(task, frame)
    x, y, w, h = task['rect']
    effective = Image.new('L', original.size)
    effective.paste(255, (x, y, x + w, y + h))
    if edit_mask:
        effective = ImageChops.multiply(effective, load_mask(edit_mask, original.size))
    if protect_mask:
        effective = ImageChops.multiply(effective, ImageChops.invert(load_mask(protect_mask, original.size)))
    if effective.getbbox() is None:
        raise ValueError('编辑与保护蒙版组合后没有允许修改的像素')
    selected_sample = load_mask(sample_mask, original.size) if sample_mask else None
    if selected_sample is not None:
        x, y, w, h = task['rect']
        if selected_sample.crop((x, y, x + w, y + h)).getbbox():
            raise ValueError('背景样本不能包含任务修改区，需明确选择未改变区域')
        if selected_sample.getbbox() is None:
            raise ValueError('背景样本为空')
        fx, fy, fw, fh = frame_box
        within_frame = Image.new('L', original.size)
        within_frame.paste(255, (fx, fy, fx + fw, fy + fh))
        if ImageChops.subtract(selected_sample, within_frame).getbbox():
            raise ValueError('背景样本超出候选声明的 frame')
    alignment_report = None
    if alignment == 'auto':
        if frame == 'patch' or quad is not None:
            raise ValueError('patch 无未改背景可供自动配准；请明确 identity 或 quad')
        from register_candidate import register
        work.mkdir(parents=True)
        registration = register(task_path, candidate_path, frame, work / 'quad.json')
        quad = registration['quad']
        alignment_report = work / 'quad.alignment.json'
    elif alignment == 'identity':
        if quad is not None:
            raise ValueError('identity 不能同时提供 quad')
        ratio = (candidate.width / candidate.height) / (frame_box[2] / frame_box[3])
        if abs(math.log(ratio)) > .025:
            raise ValueError('identity 候选比例不匹配；请用可信配准或四角映射')
        quad = [[0, 0], [candidate.width, 0], [candidate.width, candidate.height], [0, candidate.height]]
    else:
        if quad is None:
            raise ValueError('quad 模式需要四角映射 JSON')
        if isinstance(quad, (str, Path)):
            quad = json.loads(Path(quad).read_text(encoding='utf-8'))
        quad_transform(quad)
    # Confirm target coverage even if a cropped candidate lacks frame borders.
    warp_candidate(candidate, frame_box, task['rect'], quad)
    work.mkdir(parents=True, exist_ok=True)
    prepared_candidate = candidate_path
    prepared_frame, prepared_quad = frame, quad
    matching = None
    if selected_sample is not None:
        from photo_finish import match_photo
        sample_bbox = selected_sample.getbbox()
        x, y, w, h = task['rect']
        left, top = min(x, sample_bbox[0]), min(y, sample_bbox[1])
        right, bottom = max(x + w, sample_bbox[2]), max(y + h, sample_bbox[3])
        region = [left, top, right - left, bottom - top]
        aligned = warp_candidate(candidate, frame_box, region, quad)
        ref = original.crop((left, top, right, bottom))
        sample = selected_sample.crop((left, top, right, bottom))
        matched, matching = match_photo(ref, aligned, sample, blur_radius, grain, assume_untagged_srgb, tone_mode)
        # Only the actual target is handed onward; never insert sample background.
        patch = matched.crop((x - left, y - top, x + w - left, y + h - top))
        prepared_candidate = work / 'matched-patch.png'
        save_png(patch, prepared_candidate)
        prepared_frame, prepared_quad = 'patch', None
        (work / 'matching.json').write_text(json.dumps(matching, ensure_ascii=False, indent=2), encoding='utf-8')
    verification = compose(task_path, prepared_candidate, prepared_frame, output, feather,
                           prepared_quad, edit_mask, protect_mask)
    quality = assess(Path(task_path).resolve().parent / 'original.png', output,
                     output.with_name(output.stem + '.allowed.png'), quality_path,
                     rect=task['rect'], alignment_report=alignment_report,
                     expected_text=expected_text, observed_text=observed_text)
    report = dict(format='patchlock-salvage', version=1, output=str(output),
                  task=str(Path(task_path).resolve()), source_frame=frame,
                  alignment_mode=alignment, source_quad=quad,
                  original_sha256=pixel_hash(original), candidate_sha256=pixel_hash(candidate),
                  result_sha256=pixel_hash(load_image(output)), matching=matching,
                  verification=verification, quality=quality,
                  quality_json=str(quality_path), quality_html=str(quality_path.with_suffix('.html')),
                  limits=['候选必须实际包含期望修改；本命令不生成或识别新内容。',
                          'identity 是调用者对构图的明确声明，不是自动配准证据。',
                          '像素范围通过与视觉/文字正确是不同结论，交付前需检查局部。'])
    workflow_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True); parser.add_argument('--candidate', required=True)
    parser.add_argument('--frame', choices=['photo', 'reference', 'patch'], required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--alignment', choices=['auto', 'identity', 'quad'], default='auto')
    parser.add_argument('--quad'); parser.add_argument('--sample-mask')
    parser.add_argument('--edit-mask'); parser.add_argument('--protect-mask')
    parser.add_argument('--feather', type=int, default=0)
    parser.add_argument('--blur-radius', type=float, default=0); parser.add_argument('--grain', type=float, default=0)
    parser.add_argument('--expected-text'); parser.add_argument('--observed-text')
    parser.add_argument('--assume-untagged-srgb', action='store_true')
    parser.add_argument('--tone-mode', choices=['additive', 'affine'], default='additive')
    args = parser.parse_args()
    try:
        report = run(args.task, args.candidate, args.frame, args.output, args.alignment,
                     args.quad, args.sample_mask, args.edit_mask, args.protect_mask, args.feather,
                     args.blur_radius, args.grain, args.expected_text, args.observed_text, args.assume_untagged_srgb, args.tone_mode)
        print(json.dumps(report, ensure_ascii=False))
        if report['quality'].get('status') == 'fail':
            raise SystemExit(2)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
