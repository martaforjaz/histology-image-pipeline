# Simple downsampling validation

`validate_downsampling.py` checks one scanner folder at a time using one ID per line from `list_IDshort.txt` (or another text file supplied with `--id-list`). It checks whether the original WSI, `2x`, and `40x` files exist. Present 2x and 40x outputs are opened with `tifffile`; dimensions, available MPP metadata, and the basic 40x pyramid structure are checked. `.part` files, invalid completed outputs, and files without MPP metadata are reported separately.

The validator does not modify images and does not decode every pixel. It writes `missing.txt`, `failed_review.txt`, and `summary.json` inside the selected output folder.

Example:

```powershell
python .\validate_downsampling.py `
  --scanner-path "\\10.17.182.53\kiemen-lab-data\Marta Pereira\Multi scanner project\Scanned images\Scanner types\Hamamatsu_S210_40x" `
  --id-list .\list_IDshort.txt `
  --output-dir "\\10.17.182.53\kiemen-lab-data\Marta Pereira\Multi scanner project\Scanned images\Scanner types\Hamamatsu_S210_40x\validation"
```

Run the same command once per scanner by changing only `--scanner-path` and `--output-dir`. P1000 should be run after its conversion is complete.
