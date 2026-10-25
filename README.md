# PatchLock · 锁域修图

**让 AI 提供候选，让原照片决定哪里能改。**

PatchLock 是用于真实照片局部精修的 Agent Skill。它将候选图中的目标内容配准并贴回原图，使用保护蒙版限制修改范围，再从保存后的 PNG 验证其他像素是否保持不变。

适合照片里的局部改字、数字替换、物体修补，以及 AI 改图时连带改变了其他细节的情况。

## 照片工具扩展

| 需求 | 可运行能力 | 明确边界 |
|---|---|---|
| 同图字样参考 | 提取深/浅色字样、记录颜色与边缘指标、透明供体四角放置 | 需明确供体位置，不自动识字、找字体或创造缺字 |
| 照片质感匹配 | 基于确认未改背景匹配 RGB 和平滑光照，可明确选择轻微模糊/颗粒 | 不自动推断曲面、真实噪声或消失的细节 |
| 笔画级修补 | 局部修复旧笔画并贴入新笔画，以旧新并集限制变化 | 蒙版需确认；复杂底纹修复仍可能不自然 |
| 外部 AI 图回填 | 一个命令串联背景配准、可选匹配、保护合成与报告 | 不提供生成模型；不可靠配准会拒绝 |
| 质量证据 | 像素范围证明、无变化警告、接缝/清晰度/纹理指标、离线 HTML 对比 | 文字和残影需独立观察，指标不认证“无瑕” |

这些工具均无需独立 API Key。新工具可选依赖 NumPy/OpenCV；借字与质量报告只需 Pillow。不会自动安装依赖或打开额外窗口。

已有原图任务和外部候选时：

```shell
python -B scripts/salvage.py --task work/task/task.json --candidate ai-edited.png --frame photo --output work/recovered.png
```

默认自动背景配准，失败时停止；确定构图未变时才使用 `--alignment identity`。最终会输出原始尺寸 PNG、变化蒙版、像素报告、离线 HTML 局部对比和处理记录。完整分支示例见 [照片工具工作流](references/photo-tools.md)、[同图字样](references/donor.md) 和 [质量报告](references/quality.md)。

```text
原照片 + 修改要求
       ↓
局部参考 → 图像编辑工具或已有候选
       ↓
背景配准 → 保护蒙版 → 局部回填
       ↓
视觉检查 + 磁盘 PNG 像素校验
```

## 使用 Skill

附上照片，然后向支持本地 Skills 的 Agent 发出请求：

> 使用 $patchlock 把书脊上的“大”改成“小”，匹配原有字形、透视与质感，其他区域保持原样。

Skill 使用当前 Agent 可用的图像编辑能力，也能接收已经生成的候选。它本身不提供模型或额度，不要求独立 API Key，也不依赖修图网站。

## 安装

将本仓库内容放进个人技能目录下的 `patchlock/` 文件夹，使目录结构为：

```text
skills/patchlock/
├── SKILL.md
├── agents/openai.yaml
├── references/workflow.md
└── scripts/
    ├── photo_patch.py
    ├── register_candidate.py
    ├── glyph_sample.py
    ├── photo_finish.py
    ├── salvage.py
    ├── quality_report.py
    └── test_*.py
```

在默认 Codex 配置中，该目录为 `~/.codex/skills/patchlock/`。使用了自定义技能目录时，按自己的配置放置。

## 本地工具

Python 3.10+。局部合成需要 Pillow；可选的自动背景配准另需 NumPy 与 OpenCV。

```shell
python -m pip install -r requirements.txt
# 需要自动背景配准时：
python -m pip install -r requirements-alignment.txt
```

工具不会联网或调用模型。命令示例从仓库根目录运行：

```shell
python scripts/photo_patch.py prepare --image photo.jpg --rect 100 120 80 60 --prompt "修改这处文字，匹配原照片" --workdir work/task
python scripts/photo_patch.py compose --task work/task/task.json --candidate candidate.png --frame reference --output work/result.png
```

候选为完整局部参考图时选 `reference`，为整张照片时选 `photo`，只包含修改区时选 `patch`。未知裁切或位置偏移不能仅凭尺寸拉伸；自动背景配准或人工四角映射的示例见 [完整工作流](references/workflow.md)。

合成会保存最终 PNG、允许变化蒙版、变化图和验证报告。检查 `pixel_scope_pass`、`changed_inside_allowed` 与 `changed_outside_allowed`；范围外变化应为 0。支持额外编辑蒙版与保护蒙版，保护区域优先。

## 验证与限制

完整测试包含可选的质感与笔画工具，运行前需安装 `requirements-alignment.txt` 中的依赖。

```shell
python -B -m unittest discover -s scripts -p "test_*.py" -v
```

测试覆盖透明候选、保护蒙版、EXIF 方向、裁切映射、羽化、原图校验、同图供体、照片匹配、旧新笔画修复、外部候选回填和离线诊断。测试使用本地临时素材，不需要生成模型；合成测试通过不能代替真实照片的视觉检查。

像素范围正确，不等于字形、色差和接缝自然。PatchLock 要求放大检查实际结果，不承诺“无瑕”或“不可检测”。JPG 会重新压缩整张图，因此需要严格像素保持时交付 PNG。比较对象是 EXIF 归一化后的解码像素，不是原 JPEG 文件字节或元数据。

自动配准基于未修改的背景特征，缺少可靠对应点、目标被裁掉或图片局部变形时会拒绝不可靠结果。单张输入上限为 4000 万像素。

NumPy 质感/修复工具在接近上限时可能需要约 2 GB 或更多内存；资源受限时优先使用原生分辨率局部照片。离线质量报告的整体状态保留 `review_required` 或 `fail`，不会仅凭统计量给出“自然”的自动通过结论。工具本身不执行 OCR，也不会把用户期望文字充当实际观察。
