# 40x source manifest without duplicate OME-TIFFs

`prepare_40x_manifest.py` checks, for one slide, that the scanner sets in the
tissue-organized 2x folder and the eight scanner-organized 40x folders are
identical. It requires S210, each global warp, each non-reference elastic
warp, RGB OME-TIFF metadata, and a calibrated approximately 0.25 µm/pixel
40x source. It writes a small CSV only; it never copies or transforms pixels.

```powershell
python 03_apply_registration/prepare_40x_manifest.py `
  --slide 'Hum Brst Cancer A' `
  --scanner-root '\\server\share\Scanned images\Scanner types' `
  --tissue-root '\\server\share\Scanned images\Tissue types' `
  --output 'Hum_Brst_Cancer_A_40x_manifest.csv'
```

The existing application CLI now accepts `--input-file` plus
`--transform-name`, so a scanner-organized file can be read in place and an
appropriately named result written under the tissue folder. Directory mode
(`--input`) remains available. The new file mode refuses inputs or padded
canvases larger than 500 million pixels by default because the current
application algorithm loads the entire image and expands the full elastic
field in RAM. Raising this limit is not a solution for whole-slide images.

**Do not run the older full-image application on the Hum Brst Cancer A 40x
files.** All six exceed the safety cap. The P1000 source alone is about 19
billion pixels; its padded canvas is still larger. Use the experimental tiled
application below and review its output visually. Do not treat manifest
creation as completed registration.

## Experimental tiled application

`apply_40x_tiled.py` reads source regions through OpenSlide and streams a
JPEG-compressed tiled OME-TIFF without allocating the full image or full-size
elastic field. Its output is presently a **single-resolution OME-TIFF** (tiled,
but not pyramidal). It should be considered `computed_pending_visual_qc` even
after technical integrity checks. Source images remain untouched; a unique
`.partial-<uuid>.tif` file is renamed to the final name only after a complete
write and dimension check. Interrupted partials must be reviewed and removed
manually before retrying.

The implementation is regression-tested against the old full-image algorithm
at a 20x scale factor, including affine, flip and elastic transforms. The
sampled 2x checks on real warps have fewer than 0.013% differing channel
values, attributable to OpenCV's slightly different interpolation rounding
for `resize` and per-tile `remap` at half-pixel ties. This is not a substitute
for visual overlay QC of the registered 40x result.

```powershell
python 03_apply_registration/run_40x_manifest_batch.py `
  --manifest-directory 'C:\path\to\one_frozen_shard' `
  --results 'C:\path\to\new_results.csv'
```

`prepare_40x_shards.py` can freeze disjoint work lists balanced by expected
padded output pixels. Do not launch a large cohort until a pilot output has
been technically checked and reviewed; padded canvases may be far larger than
source files. To check source pairing, use
`verify_40x_source_matches_2x.py` and review any low-correlation previews.
