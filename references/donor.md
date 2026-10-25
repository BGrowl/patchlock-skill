# Explicit donor strokes

`glyph_sample.py` samples an explicitly selected donor rectangle from a photograph and places the transparent result into another explicit quadrilateral. This is a deterministic local helper. It does not recognize text, identify a font, generate missing characters, or call an AI/network service. Use only a donor that a human has identified as suitable.

Coordinates are measured in the EXIF-normalized photo, with origin at the top left. Rectangles are `x y width height`. Quads are ordered TL, TR, BR, BL and describe the corners of the **whole donor canvas**, including its transparent margins.

```powershell
python -B scripts/glyph_sample.py sample --image photo.jpg --rect 120 80 70 90 --polarity dark --output donor.png
python -B scripts/glyph_sample.py sample --image photo.jpg --rect 120 80 70 90 --mask donor-mask.png --output masked-donor.png
python -B scripts/glyph_sample.py place --glyph donor.png --task task/task.json --quad target-quad.json --output candidate.png
```

`target-quad.json` contains a JSON array such as `[[230,90],[300,93],[298,180],[228,176]]`. Every corner must lie inside the task rectangle. Crossed, reversed, degenerate, nonfinite, and out-of-bounds quads are rejected. The quad uses target-photo coordinates; it is not the candidate-coordinate registration quad accepted by `photo_patch.py compose`.

The Python interfaces are:

```python
sample(image_path, rect, output, polarity='dark', mask_path=None) -> dict
place(glyph_path, task_path, quad, output) -> dict
```

Sampling writes a transparent donor PNG at the rectangle's native size and `<output-stem>.sample.json`. The report includes the original file SHA-256, normalized RGBA SHA-256, donor rectangle, source dimensions, polarity, method, alpha-weighted coverage, foreground/background counts, stroke bounding box, mean foreground/background RGB, and an edge/alpha heuristic. Heuristics are not calibrated blur measurements or semantic verification.

Automatic dark/light extraction estimates a locally smooth background using a maximum/minimum rank filter followed by Gaussian smoothing. The rank-filter working image is bounded to 1024 by 1024; native source pixels and donor output remain at native resolution. Thick strokes, texture, gradients with abrupt edges, shadows, and overlapping marks can produce poor masks. Inspect the transparent donor visually and provide a mask when necessary.

RGB samples retain edge/background contamination: this helper does not perform foreground-color unmatting. Extraction alpha can lighten donor edges when those sampled edge pixels are composited again. An explicit mask alone does not remove background RGB contamination. Inspect the actual composite against the target background, especially around stroke edges; a clean-looking transparent donor preview is insufficient.

An explicit mask must match the **full normalized source-photo dimensions**. No resizing occurs. Files with an alpha channel (including black RGBA strokes) use alpha; files without alpha use grayscale luminance. White or alpha 255 selects strokes; black or alpha 0 removes them. Source-photo alpha is multiplied into the mask. A donor needs at least eight foreground pixels (alpha >= 32) and eight background pixels (alpha <= 8); uniform or empty donors are rejected.

Placement writes a transparent PNG with the task photo's native dimensions and `<output-stem>.stroke-mask.png`, containing exactly its new stroke alpha. Perspective resampling uses bilinear interpolation to preserve the donor alpha range without ringing. A donor needs nonempty alpha and at least eight fully transparent pixels; opaque and uniformly translucent rectangles are rejected. The candidate contains only placed strokes. It does not erase the old glyph. Background reconstruction, edit/protect masks, and final protected compositing remain the downstream compositor's responsibility.

For `photo_patch.py compose`, use `--frame photo` with the candidate and no extra quad. Apply any needed edit/protect masks. The stroke mask identifies newly placed strokes; using it as the only edit mask will not remove old strokes beyond that alpha.

All input images, including masks and donors, use the existing 40-megapixel cap. Work uses bounded Pillow raster buffers and no required NumPy/OpenCV dependency. Primary and companion output paths must all be new. Existing files and inputs are never overwritten; files are created exclusively, and partial files created by a failing operation are removed.

Run the focused tests with `python -B scripts/test_glyph_sample.py -v`.
