# Downsampling validation

`validate_downsampling.py` uses the slide identifiers in `Slide_record.xlsm` to audit one scanner at a time.

For every slide ID, it reports whether the original WSI, 2x TIFF and 40x OME-TIFF exist. Present downsampled files are opened and checked for valid dimensions, expected physical pixel size (5 micrometres/pixel at 2x and 0.25 micrometres/pixel at 40x), OME dimensions, and an ordered 40x pyramid. It does not decode every pixel and does not modify image files.

Run one scanner at a time so that each scanner receives a separate, easy-to-read Excel report:

```powershell
python 04_downsampling_validation/validate_downsampling.py `
  --slide-record "C:\path\to\Slide_record.xlsm" `
  --scanner-root "\\server\share\Scanner types" `
  --scanner "Hamamatsu_S210_40x" `
  --output-json "validation\Hamamatsu_S210.json" `
  --output-excel "validation\Hamamatsu_S210.xlsx" `
  --workers 2 `
  --timeout 120
```

The `Validation` worksheet contains one row per slide ID and colour-coded WSI/2x/40x cells. The `Details` worksheet records the reason, dimensions, MPP, pyramid level count and matched path.

Statuses:

- `PASS` or `EXISTS`: green
- `MISSING`, `FAIL` or `INCOMPLETE`: red
- `REVIEW`: yellow, including a file that opens but lacks required MPP metadata

Use a small worker count on network storage. P1000 can be omitted while its conversion is still running by simply not selecting it with `--scanner`.
