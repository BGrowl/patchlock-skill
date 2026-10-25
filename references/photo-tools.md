# Photographic matching, stroke repair and candidate salvage

These local tools prepare candidates for the original `photo_patch.py` compositor. They never call a model or website. NumPy and OpenCV are optional dependencies installed with `requirements-alignment.txt`. The original Pillow-only compositor and donor/quality tools remain usable without them. Native EXIF-normalized coordinates apply throughout; no automatic resizing of masks occurs.

## Existing external AI candidate

First prepare a task using the original photograph and the exact authorized rectangle:

```powershell
python -B scripts/photo_patch.py prepare --image photo.jpg --rect 902 328 116 104 --prompt "Replace only this book-spine character" --workdir work/book
python -B scripts/salvage.py --task work/book/task.json --candidate ai-candidate.png --frame photo --output work/book-final.png
```

Default `--alignment auto` uses unchanged-background SIFT registration and the existing local-consistency rejection gates. It needs NumPy/OpenCV and stable background features. It never silently relaxes thresholds or switches to scaling. Photo and reference frames are supported; a patch-only candidate lacks background and cannot use automatic registration.

If the candidate is positively known to preserve framing, use `--alignment identity`. This is a caller assertion, not evidence inferred from matching aspect ratios. A trusted mapping can instead use `--alignment quad --quad frame-quad.json`; quad coordinates are in the candidate and correspond to the declared frame's four corners, as in the original compositor. These are different from a donor placement quad, which is in the target photo.

Add `--edit-mask` and `--protect-mask` as needed. The core compositor's full-size grayscale mask semantics apply. Feathering defaults to zero; extra feathering can leave old-character edges visible. Never enlarge the authorized rectangle to accommodate candidate drift.

Outputs: new PNG, `.allowed.png`, `.diff.png`, `.verification.json`, `.quality.json`, `.quality.html`, `.salvage.json`, and a new `.salvage/` work directory. The workflow JSON records original/candidate/result pixel hashes, source frame/quad, optional matching metrics and independent scope/quality reports. Supplied alignment metadata in the quality report remains unbound geometry, not certification of a particular result. A failed run may retain intermediate evidence for diagnosis; retry with a new output name. Existing output names are checked before starting.

The command does not create the requested edit. If the candidate contains no edit, the quality report warns `no_op`; scope can still pass. Default quality is `review_required`; a scope failure or independently observed text mismatch yields `fail` and CLI exit 2. Review-required is exit 0, not an automatic declaration of editing success.

## Matching photographed color and illumination

Choose a full-photo background sample mask, white on **confirmed unchanged background**, black elsewhere. It must exclude the task rectangle and stay within the candidate's declared frame. Avoid text, reflections that moved, shadows that changed shape, edges and redrawn texture. At least 64 fully opaque sampled pixels are required. An opaque grayscale PNG is the least ambiguous sample format.

```powershell
python -B scripts/salvage.py --task work/book/task.json --candidate ai-candidate.png --frame photo --sample-mask unchanged-background.png --output work/book-matched.png
```

Matching defaults to `--tone-mode additive`: bounded RGB offsets and a smooth residual illumination plane. This avoids learning a blue background's channel gains and extrapolating those gains onto white lettering. Explicit `--tone-mode affine` also fits RGB gains and is appropriate only when the sample reliably covers the relevant tonal/material range; it can discolor foreground content absent from the sample. Neither mode proves foreground color fidelity. Fitting is capped at 50,000 samples, but acceptance and reported errors check **all** selected background pixels in blocks. Inconsistent samples or excessive corrections are rejected. A local plane is an illumination approximation, not curved-surface reconstruction; do not extrapolate from distant, dissimilar material.

For already aligned images of equal size, a standalone command is available:

```powershell
python -B scripts/photo_finish.py match --original aligned-reference.png --candidate aligned-candidate.png --sample-mask sample.png --output matched-candidate.png
```

The standalone tool cannot know which area is a semantic edit: exclude every edited area yourself. Its output is a **candidate** that can change the full frame; final delivery must pass through protected composition. It writes `.match.json` and keeps candidate alpha. When valid embedded ICC profiles differ, the candidate is converted into the reference's encoding. If exactly one image is untagged, matching rejects by default. **Only after confirming the untagged image uses sRGB**, add `--assume-untagged-srgb` to `match` or `salvage`; this performs the conversion, records the assumption in `color_management`, and keeps the reference profile for final composition. It does not merely relabel the candidate's bytes. Invalid embedded profiles are rejected, including when their bytes match. If both images have identical valid/missing ICC metadata, matching treats them as sharing an encoding, not measured color fidelity.

Optional `--blur-radius 0.5` (range 0–3 native pixels) and `--grain 0.2` (range 0–1) are explicit choices after visual assessment, never automatic adjustments based on unequal glyphs. Blur uses premultiplied colors; original alpha is restored. Deterministic grain approximates local residual amplitude, capped at 10 per RGB channel, not real camera noise or recovered texture. The matching report includes before/after full-sample error; a lower error does not prove target text is natural.

## Stroke-level background preservation

Use same-photo donor extraction/placement when a suitable character exists: see `donor.md`. Placement provides a full-photo transparent replacement and new-stroke mask. It **does not** remove the previous character. Prepare a separate old-stroke mask covering every old stroke and antialiased edge, with a small repair margin. Keep inter-letter and inter-stroke background black unless it actually needs repair.

```powershell
python -B scripts/photo_finish.py strokes --original work/book/original.png --replacement placed-donor.png --old-mask old-strokes.png --new-mask placed-donor.stroke-mask.png --protect-mask neighbors.png --output repaired-candidate.png
python -B scripts/salvage.py --task work/book/task.json --candidate repaired-candidate.png --frame photo --alignment identity --edit-mask repaired-candidate.stroke-mask.png --output work/stroke-final.png
```

Mask semantics in the stroke tool:

- Old mask: any positive effective grayscale × alpha selects pixels to reconstruct, using local OpenCV Telea inpainting (radius 1–15, default 3).
- New mask: any positive effective value authorizes a new stroke; **replacement alpha supplies coverage exactly once**. Passing the donor's own alpha as the new mask is supported without squaring antialiasing.
- Protection: any positive effective value completely forbids modification in this tool, including tiny positive RGBA values. Black luminance or alpha zero is not selected. This is stricter than the core compositor's continuous gray weights.
- Output `.stroke-mask.png`: binary union of old and effective new support, with protection removed. Use this union for final composition; a new-stroke-only mask would leave old strokes behind.

The stroke candidate preserves every pixel outside this support. Its `.strokes.json` records pixel counts, radius and input/output hashes. The compositor's task rectangle still bounds final delivery, so ensure it contains the entire old/new union. Using a tight task can clip a changed stroke; verify the actual glyph and do not use scope pass alone.

Inpainting estimates background hidden under old strokes. It works best for narrow marks against smooth local backgrounds. Texture, broad filled glyphs, highlights, creases, overlapping objects and transparent original regions can fail; use a generated clean-background candidate or smaller well-supported repair instead. Inspect residual old edges and nearby protected detail at native resolution.

## Resource limits

Inputs retain the original 40MP guard. Optional NumPy tools hold several full-size buffers and can require roughly 2 GB or more near that limit; actual memory varies. Prefer a native-resolution local task/crop on constrained machines rather than downsampling and claiming lost detail was recovered. Reports and masks must all use the same coordinate space. No optional tool installs dependencies or opens GUI windows on its own.

Run all portable tests with `python -B -m unittest discover -s scripts -p "test_*.py" -v`.
