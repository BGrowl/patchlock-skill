# PatchLock · 锁域修图

**让 AI 提供候选，让原照片决定哪里能改。**

PatchLock 是用于真实照片局部精修的 Agent Skill。它将候选图中的目标内容配准并贴回原图，使用保护蒙版限制修改范围，再从保存后的 PNG 验证其他像素是否保持不变。

适合照片里的局部改字、数字替换、物体修补，以及 AI 改图时连带改变了其他细节的情况。

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
    └── test_photo_patch.py
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

```shell
python -B scripts/test_photo_patch.py -v
```

目前包含 10 项测试，覆盖透明候选、保护蒙版、EXIF 方向、裁切映射、羽化、原图校验和范围外变化检测。测试使用本地临时素材，不需要生成模型。

像素范围正确，不等于字形、色差和接缝自然。PatchLock 要求放大检查实际结果，不承诺“无瑕”或“不可检测”。JPG 会重新压缩整张图，因此需要严格像素保持时交付 PNG。比较对象是 EXIF 归一化后的解码像素，不是原 JPEG 文件字节或元数据。

自动配准基于未修改的背景特征，缺少可靠对应点、目标被裁掉或图片局部变形时会拒绝不可靠结果。单张输入上限为 4000 万像素。
