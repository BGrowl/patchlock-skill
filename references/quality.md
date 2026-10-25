# 离线质量证据报告

`scripts/quality_report.py` 使用 Pillow 在本地生成一份新的 JSON 和同名 HTML，既可审查成功合成，也可记录尚未通过范围或文字比较的结果。它不调用模型、OCR、网络或远程页面资源。

```powershell
python scripts/quality_report.py --original original.png --result result.png --allowed-mask result.allowed.png --output review.json --rect 100 80 120 50
```

可选参数：`--alignment-report candidate.alignment.json`、`--expected-text "42"`、`--observed-text "42"`。`observed-text` 应来自独立的人眼或 OCR 观察，不能把期望值原样当作识别结果。脚本只逐字比较两个调用方提供的字符串，保留大小写、空格与换行差异，报告明确标为 `caller_supplied_literal_comparison_not_ocr`；脚本无法验证观察来源。未提供观察结果时，文字始终待人工检查。旧字残影始终需要视觉复核。

Python API：

```python
from quality_report import assess

report = assess(original_path, result_path, allowed_mask_path, output,
                rect=None, alignment_report=None,
                expected_text=None, observed_text=None)
```

`alignment_report` 只接受 JSON 文件路径，使用 `register_candidate.py` 输出的 `.alignment.json`，不接受四角数组文件或字典。报告检查 frame、有限且不交叉的四角、匹配计数关系与有限非负误差。它只呈现经过结构检查的调用方几何诊断，始终标记 `unverified_not_bound_to_result`。没有验证输入图像绑定或配准来源，旧候选的报告也可能有很低的误差；不得把这些数值说成当前结果已对齐。

JSON 的 `scope` 复用 `photo_patch.verify`，对 EXIF 归一化后的 RGBA 像素做比较。包括 alpha，非零蒙版值允许变化；拒绝空蒙版与尺寸不匹配。`scope_pass` 仅表示允许范围外没有变化，不表示修改已实现。没有任何像素变化时加 `no_op` 警告。

整体 `status` 只有两种：范围失败或调用方文字比较不一致为 `fail`，其余为 `review_required`。没有自动的整体“完美”或“通过”结论。CLI 对 `fail` 或无效输入退出 2，写出有效待审报告退出 0；代码 0 不等于视觉审查通过。

诊断含义与边界：

- 接缝统计遍历允许区和禁止区之间的内部四邻接像素对，以 RGBA 最大通道差表示原图与结果的跳变，逐对减去原图已有跳变，仅统计正增加。原图既有边缘不会因为绝对对比度高而直接触发新增接缝提示。平均增加超过 8，或增加超过 16 的邻接对比例大于 10%，仅触发复核提示；阈值未经感知标定，预期新增边缘也会触发，原图边缘可能掩盖伪影。图像最外边界无外部邻点，不统计；全图允许时此项未评估。
- 变化诊断是允许区内变化比例与 RGBA 最大通道差均值，不能判断是否改对内容。
- 清晰度是亮度图减去 1 像素 Gaussian 模糊后的绝对残差均值；纹理是同一绝对残差的标准差。两项均在允许像素上统计，使用原生像素，不包括 alpha。原图残差均值为零时比例为空。这些值受字形、主体边缘、尺度、模糊和背景影响，不是噪声识别或照片质感验收，没有通过阈值。
- 所有低指标都不能证明自然、字形正确、无变形或无残影。必须对照原图放大检查文字、旧笔画、邻字、透视、底色、光照与纹理。

HTML 自带原图、结果与 RGBA 二值变化的局部裁切 PNG。默认取允许区包围盒加边距，也可用 `rect` 指定检查区域，矩形为原生尺寸 `x y width height`。预览最长边限制 1200 像素，较大裁切会缩小并明确标注；浏览器放大无法恢复像素细节。HTML 不引用原始文件路径、不加载外部脚本或样式，调用方文本作 HTML 转义。透明区域的视觉显示受页面底色影响，应结合 RGBA 变化计数审查。

输出必须使用新的 `.json` 文件名。JSON、同名 HTML 与所有输入路径先检查冲突；任一已存在时拒绝，避免覆盖或留下单份报告。写入使用独占创建；常规写入异常会清理本次创建的输出。此检查不承诺进程被强制结束时的双文件原子提交，也不锁定另一个进程正在修改的输入。
