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

**Do not run the current full-image application on the Hum Brst Cancer A 40x
files.** All six exceed the safety cap. The P1000 source alone is about 19
billion pixels; its padded canvas is still larger. A separate tiled/streaming
application, validated against the existing algorithm on small images and
reviewed visually at 40x, is required before producing these registered
whole-slide outputs. Do not treat manifest creation as completed registration.
