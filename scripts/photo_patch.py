"""Local photo task preparation, candidate registration, compositing and verification.

Semantic editing belongs to an image-editing tool. This helper never calls a model.
"""
import argparse
import hashlib
import json
import math
from functools import reduce
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps

MAX_PIXELS = 40_000_000


def load_image(path):
    with Image.open(path) as source:
        if source.width * source.height > MAX_PIXELS:
            raise ValueError('照片超过 4000 万像素，请先确认是否缩小后处理')
        image = ImageOps.exif_transpose(source).convert('RGBA')
        image.load()
    return image


def pixel_hash(image):
    digest = hashlib.sha256()
    for top in range(0, image.height, 128):
        digest.update(image.crop((0, top, image.width, min(top + 128, image.height))).tobytes())
    return digest.hexdigest()


def check_rect(rect, size):
    if len(rect) != 4 or any(type(v) is not int for v in rect):
        raise ValueError('选区必须是四个整数 x y width height')
    x, y, w, h = rect
    if x < 0 or y < 0 or w < 1 or h < 1 or x + w > size[0] or y + h > size[1]:
        raise ValueError('选区超出原图范围')
    return x, y, w, h


def new_output(path):
    path = Path(path).resolve()
    if path.exists():
        raise ValueError(f'输出已存在，请使用新文件名：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_png(image, path):
    image.save(path, format='PNG', icc_profile=image.info.get('icc_profile'))


def prepare(image_path, rect, prompt, workdir):
    image = load_image(image_path)
    x, y, w, h = check_rect(rect, image.size)
    if not prompt.strip() or len(prompt) > 4000:
        raise ValueError('请提供不超过 4000 字的修改要求')
    root = Path(workdir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('任务目录不为空，请使用新目录，避免覆盖原任务')
    root.mkdir(parents=True, exist_ok=True)
    margin = max(100, w, h)
    left, top = max(0, x - margin), max(0, y - margin)
    right, bottom = min(image.width, x + w + margin), min(image.height, y + h + margin)
    context = [left, top, right - left, bottom - top]
    reference = image.crop((left, top, right, bottom))
    save_png(image, root / 'original.png')
    save_png(reference, root / 'reference.png')
    allowed = Image.new('L', image.size, 0)
    allowed.paste(255, (x, y, x + w, y + h))
    allowed.save(root / 'allowed-mask.png')
    manifest = dict(format='moru-photo-task', version=1, size=list(image.size), rect=list(rect),
                    context=context, prompt=prompt, original_sha256=pixel_hash(image))
    (root / 'task.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    instructions = (f'请编辑附上的真实照片局部参考图。\n修改要求：{prompt}\n'
                    f'参考图尺寸：{reference.width} × {reference.height}。唯一修改区：'
                    f'X={x-left}, Y={y-top}, 宽={w}, 高={h}，原点为左上角。\n'
                    '保留其余内容、原有字体与字重、光照、透视、噪点和模糊程度。'
                    '返回完整参考图，保持边界、比例和构图；不要画选框、标注或水印。')
    (root / 'instructions.txt').write_text(instructions, encoding='utf-8')
    return root / 'task.json'


def read_task(path):
    path = Path(path).resolve()
    task = json.loads(path.read_text(encoding='utf-8'))
    if task.get('format') != 'moru-photo-task' or task.get('version') != 1:
        raise ValueError('不是支持的任务文件')
    image = load_image(path.parent / 'original.png')
    if list(image.size) != task.get('size') or pixel_hash(image) != task.get('original_sha256'):
        raise ValueError('任务原图 hash 校验失败，请重新准备任务')
    check_rect(task['rect'], image.size)
    check_rect(task['context'], image.size)
    return task, image


def frame_rect(task, frame):
    if frame == 'photo':
        return [0, 0, *task['size']]
    if frame == 'reference':
        return task['context']
    if frame == 'patch':
        return task['rect']
    raise ValueError('必须明确返回图类型：reference、photo 或 patch')


def quad_transform(quad):
    if len(quad) != 4 or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in quad):
        raise ValueError('四角坐标无效')
    for i in range(4):
        p, a, b = quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]
        if (a[0]-p[0])*(b[1]-a[1]) - (a[1]-p[1])*(b[0]-a[0]) <= 1e-8:
            raise ValueError('四角必须按左上、右上、右下、左下排列，且不能交叉或重合')
    p0, p1, p2, p3 = quad
    dx1, dx2 = p1[0]-p2[0], p3[0]-p2[0]
    dy1, dy2 = p1[1]-p2[1], p3[1]-p2[1]
    dx3, dy3 = p0[0]-p1[0]+p2[0]-p3[0], p0[1]-p1[1]+p2[1]-p3[1]
    g = h = 0.0
    if abs(dx3) + abs(dy3) > 1e-8:
        det = dx1*dy2-dx2*dy1
        if abs(det) < 1e-12:
            raise ValueError('四角无法形成有效透视映射')
        g, h = (dx3*dy2-dx2*dy3)/det, (dx1*dy3-dx3*dy1)/det
    return [p1[0]-p0[0]+g*p1[0], p3[0]-p0[0]+h*p3[0], p0[0],
            p1[1]-p0[1]+g*p1[1], p3[1]-p0[1]+h*p3[1], p0[1], g, h]


def map_point(t, u, v):
    z = t[6]*u+t[7]*v+1
    if abs(z) < 1e-12:
        raise ValueError('四角映射在修改区域内奇异')
    return (t[0]*u+t[1]*v+t[2])/z, (t[3]*u+t[4]*v+t[5])/z


def warp_candidate(candidate, frame, rect, quad):
    t = quad_transform(quad)
    x, y, w, h = rect
    fx, fy, fw, fh = frame
    u0, v0 = (x-fx)/fw, (y-fy)/fh
    for px, py in [(x,y),(x+w,y),(x+w,y+h),(x,y+h)]:
        sx, sy = map_point(t, (px-fx)/fw, (py-fy)/fh)
        if not math.isfinite(sx+sy) or sx < -.01 or sy < -.01 or sx > candidate.width+.01 or sy > candidate.height+.01:
            raise ValueError('修改区域超出候选图范围，请重新对齐或提供完整候选图')
    # Pillow inverse perspective maps output patch coordinates to candidate coordinates.
    z = t[6]*u0+t[7]*v0+1
    coefficients = [t[0]/fw/z, t[1]/fh/z, (t[0]*u0+t[1]*v0+t[2])/z,
                    t[3]/fw/z, t[4]/fh/z, (t[3]*u0+t[4]*v0+t[5])/z,
                    t[6]/fw/z, t[7]/fh/z]
    return candidate.transform((w,h), Image.Transform.PERSPECTIVE, coefficients,
                               resample=Image.Resampling.BICUBIC)


def load_mask(path, size):
    mask = load_image(path)
    if mask.size != size:
        raise ValueError('蒙版尺寸必须与归一化原图一致；不能自动缩放蒙版')
    return ImageChops.multiply(mask.convert('L'), mask.getchannel('A'))


def inward_feather(mask, radius):
    if type(radius) is not int or not 0 <= radius <= 20:
        raise ValueError('羽化需为 0 到 20 像素的整数')
    if radius == 0:
        return mask
    binary = mask.point(lambda v: 255 if v else 0)
    weight = Image.new('L', mask.size, 0)
    for step in range(radius+1):
        increment = round((step+1)*255/(radius+1))-round(step*255/(radius+1))
        layer = binary.point(lambda v: increment if v else 0)
        weight = ImageChops.add(weight, layer)
        padded = ImageOps.expand(binary, 1, fill=0).filter(ImageFilter.MinFilter(3))
        binary = padded.crop((1,1,mask.width+1,mask.height+1))
    return ImageChops.multiply(mask, weight)


def verify(original_path, result_path, allowed_path):
    original, result = load_image(original_path), load_image(result_path)
    if original.size != result.size:
        raise ValueError('输出尺寸与原图不一致')
    allowed = load_mask(allowed_path, original.size).point(lambda v: 255 if v else 0)
    delta = reduce(ImageChops.lighter, ImageChops.difference(original, result).split())
    changed = delta.point(lambda v: 255 if v else 0)
    inside = ImageChops.multiply(changed, allowed)
    outside = ImageChops.multiply(changed, ImageOps.invert(allowed))
    return dict(size=list(result.size), changed_inside_allowed=inside.histogram()[255],
                changed_outside_allowed=outside.histogram()[255],
                allowed_pixels=allowed.histogram()[255], changed_bbox=changed.getbbox(),
                original_rgba_sha256=pixel_hash(original), result_rgba_sha256=pixel_hash(result),
                pixel_scope_pass=outside.getbbox() is None)


def compose(task_path, candidate_path, frame, output, feather=0, quad=None,
            edit_mask=None, protect_mask=None):
    task, original = read_task(task_path)
    candidate = load_image(candidate_path)
    frame_box = frame_rect(task, frame)
    x, y, w, h = task['rect']
    if quad is None:
        ratio = (candidate.width/candidate.height)/(frame_box[2]/frame_box[3])
        if abs(math.log(ratio)) > .025:
            raise ValueError('候选图比例与返回类型不同；请提供可信四角映射，不能直接拉伸')
        quad = [[0,0],[candidate.width,0],[candidate.width,candidate.height],[0,candidate.height]]
    patch = warp_candidate(candidate, frame_box, task['rect'], quad)
    box = (x,y,x+w,y+h)
    allowed = Image.new('L', original.size, 0)
    allowed.paste(255, box)
    if edit_mask:
        allowed = ImageChops.multiply(allowed, load_mask(edit_mask, original.size))
    if protect_mask:
        allowed = ImageChops.multiply(allowed, ImageOps.invert(load_mask(protect_mask, original.size)))
    old_patch = original.crop(box)
    blended = Image.alpha_composite(old_patch, patch)
    weighted = inward_feather(allowed.crop(box), feather)
    result = original.copy()
    result.paste(Image.composite(blended, old_patch, weighted), (x,y))
    output = Path(output).resolve()
    if output.suffix.lower() != '.png':
        raise ValueError('验证输出必须使用 PNG；JPG 会重新压缩选区外像素')
    allowed_output = output.with_name(output.stem+'.allowed.png')
    report_output = output.with_name(output.stem+'.verification.json')
    diff_output = output.with_name(output.stem+'.diff.png')
    for path in [output, allowed_output, report_output, diff_output]:
        new_output(path)
    save_png(result, output)
    allowed.save(allowed_output)
    report = verify(Path(task_path).resolve().parent/'original.png', output, allowed_output)
    report.update(frame=frame, quad=quad, feather=feather, output=str(output), allowed_mask=str(allowed_output),
                  limits=['像素范围校验不代表文字准确、自然或不可检测；需要视觉检查。',
                          '仅比较 EXIF 归一化后的 RGBA 像素，不保证 JPEG 字节或元数据不变。'])
    report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    delta = reduce(ImageChops.lighter, ImageChops.difference(original, load_image(output)).split())
    delta.point(lambda v: 255 if v else 0).save(diff_output)
    if not report['pixel_scope_pass']:
        raise ValueError(f'选区外像素校验未通过，不可交付：{report_output}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='归一化原图，建立局部任务和干净参考图')
    p.add_argument('--image',required=True);p.add_argument('--rect',type=int,nargs=4,required=True)
    p.add_argument('--prompt',required=True);p.add_argument('--workdir',required=True)
    p = sub.add_parser('compose', help='局部回填并验证，所有输出使用新文件名')
    p.add_argument('--task',required=True);p.add_argument('--candidate',required=True)
    p.add_argument('--frame',choices=['reference','photo','patch'],required=True)
    p.add_argument('--output',required=True);p.add_argument('--feather',type=int,default=0)
    p.add_argument('--quad',help='四个对应角点的 JSON 文件，顺序 TL TR BR BL')
    p.add_argument('--edit-mask');p.add_argument('--protect-mask')
    p = sub.add_parser('verify', help='独立重开磁盘图片验证像素范围')
    p.add_argument('--original',required=True);p.add_argument('--result',required=True);p.add_argument('--allowed-mask',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(prepare(args.image, args.rect, args.prompt, args.workdir))
        elif args.command == 'compose':
            quad = json.loads(Path(args.quad).read_text(encoding='utf-8')) if args.quad else None
            print(json.dumps(compose(args.task,args.candidate,args.frame,args.output,args.feather,quad,args.edit_mask,args.protect_mask),ensure_ascii=False))
        else:
            report = verify(args.original,args.result,args.allowed_mask)
            print(json.dumps(report,ensure_ascii=False))
            if not report['pixel_scope_pass']:
                raise SystemExit(2)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        parser.exit(2,f'Error: {exc}\n')


if __name__ == '__main__':
    main()
