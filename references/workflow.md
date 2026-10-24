# 本地工具与任务交换

需要 Python 3.10+ 和 Pillow；可选自动背景配准另需 NumPy、OpenCV（含 SIFT）。优先使用当前环境已经可用的运行时。缺依赖时使用允许的依赖安装流程，或明确说明缺失；不要因此索要 API Key。所有脚本不联网、不调用模型、不覆盖已有输出。先运行相应脚本 `--help` 看完整参数。

下例从本 Skill 目录运行，路径按当前任务替换。任务与照片输出放在当前工作区，不写进已安装 Skill。

## 一个完整示例

用户要求：把书脊「大」改成「小」，其他地方保留。

```powershell
python scripts/photo_patch.py prepare --image "C:/photos/book.jpg" --rect 902 328 116 104 --prompt "把书脊上的大改成小，匹配白色印刷字、倾斜角度和蓝色底纹" --workdir "C:/work/book-task"
```

脚本先归一化 EXIF 方向，再截取附近参考，不将照片放大后当作原始细节。检查生成的 `original.png`、`reference.png`。`instructions.txt` 的坐标已换算到参考图。参考图不包含绿色框或其他标记。不要把生成的 `allowed-mask.png` 当成图像编辑 API 的透明蒙版：它是本地约束蒙版，白色允许、黑色禁止。

通过内置图像编辑工具修改参考图，或接收用户的候选图。以下候选是保持完整参考构图的返回结果：

```powershell
python scripts/photo_patch.py compose --task "C:/work/book-task/task.json" --candidate "C:/work/candidate.png" --frame reference --feather 2 --output "C:/work/book-final.png"
```

候选为整张照片时选 `--frame photo`；只包含目标选区时选 `--frame patch`。不带 `--quad` 的映射会拒绝超过约 2.5% 的比例差异；通过比例检查也不表示内容位置正确。

## 背景配准

当候选保持背景纹理但裁切或位置不同，尝试从未编辑背景配准：

```powershell
python scripts/register_candidate.py --task "C:/work/book-task/task.json" --candidate "C:/work/candidate.png" --frame photo --output "C:/work/book-quad.json"
python scripts/photo_patch.py compose --task "C:/work/book-task/task.json" --candidate "C:/work/candidate.png" --frame photo --quad "C:/work/book-quad.json" --feather 2 --output "C:/work/book-final-aligned.png"
```

自动配准仅支持 photo/reference。特征检测图长边上限 1600、最多 6000 个特征。它排除原图修改区附近四像素，要求至少 12 个一致匹配、内点率至少 40%、目标附近至少 6 个内点且中位误差不超过 2.5 原图像素。这些是拒绝不可靠映射的门槛，不是质量认证；重复图案、局部弯曲或重新绘制可能仍需人工检查。匹配失败不自动放宽门槛或降级成不明裁切的尺寸拉伸。

人工 `--quad` JSON 是四组候选图坐标，顺序左上、右上、右下、左下；这些点对应 `--frame` 指定的完整画面四角。允许负坐标或超出候选边界，以表达缺失的参考边缘，但目标选区必须完全落在候选图中。

```json
[[ -50, -50 ], [ 250, -50 ], [ 250, 250 ], [ -50, 250 ]]
```

这表示一张 300×300 参考图四边各裁去 50 像素，得到 200×200 候选。坐标不是随意拖到好看的地方：必须由可信对应关系推得。

## 编辑与保护蒙版

`--edit-mask editable.png` 和 `--protect-mask protected.png` 使用归一化原图同尺寸 PNG。灰度为权重，透明处视为 0。有效允许范围为：任务矩形 × 编辑蒙版 ×（1−保护蒙版）。因此保护白区始终不会变化，矩形外永远不会变化。蒙版不会自动缩放或识别目标；由明确的保护要求制作并查看后使用。

羽化为 0–20 原图像素，只向允许范围内部收缩。过大的羽化会保留旧字边缘；检查新旧完整笔画，必要时减小羽化并使用更准确的蒙版。透明候选以 alpha 覆盖原像素，不会将照片擦成透明。

## 输出与复核

合成输出四个文件：最终 PNG、`*.allowed.png`、`*.diff.png`、`*.verification.json`。变化图白色代表发生变化、黑色代表未变化；不会作为最终照片交付。

```powershell
python scripts/photo_patch.py verify --original "C:/work/book-task/original.png" --result "C:/work/book-final.png" --allowed-mask "C:/work/book-final.allowed.png"
```

`verify` 验证失败退出码为 2。关注 `changed_inside_allowed`、`changed_outside_allowed`、`pixel_scope_pass`、尺寸与变化边界。报告不证明文字正确，也不证明修图不可检测。

单张输入上限 4000 万像素；超限先解释并协商缩小。程序保留任务原图的 ICC 配置信息，但不自动将不同候选色域匹配成原图色域，最终需检查局部色差。原文件不覆盖，归一化和 PNG 输出不保证原 JPEG 压缩字节、EXIF 或其他元数据不变。

重试使用新任务目录或新输出文件名。`task.json` 与网站 `.moru.json` 不是同一交换格式，不能直接互换；这个 Skill 不依赖网站。

维护本地合成脚本时，运行 `python -B scripts/test_photo_patch.py -v`。测试使用临时合成素材，覆盖保护蒙版、透明候选、EXIF 方向、裁切透视、羽化、原图校验和选区外差异检测，不联网，也不把测试结果冒充真实图像生成质量。
