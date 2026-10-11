"""Map scanner-organized 40x originals to tissue-organized 2x CODA warps.

This is a read-only preflight for images and warps. Only the requested CSV is
written; no OME-TIFF is copied, linked, or transformed.
"""

import argparse
import csv
from pathlib import Path

import tifffile

from apply_registration_to_20x_image import load_mat_file, read_tiff_mpp


SCANNER_FOLDERS = {
    "S210": "Hamamatsu_S210_40x",
    "S360": "Hamamatsu_S360_40x",
    "Leica": "Leica Aperio GT 180_40x",
    "Olympus": "Olympus VS200_40x",
    "P1000": "P1000_40x",
    "Pramana": "Pramana_40x",
    "Roche": "Roche Ventana_40x",
    "Zeiss": "Zeiss_40x",
}


def build_rows(slide, scanner_root, tissue_root):
    tissue_dir = tissue_root / slide
    low_dir = tissue_dir / "2x"
    warps = low_dir / "registered" / "elastic registration" / "save_warps"
    if not low_dir.is_dir() or not warps.is_dir() or not (warps / "D").is_dir():
        raise ValueError("Missing 2x folder or complete CODA warp directory")
    low_files = list(low_dir.glob("*.tif"))
    names = {code: f"{slide}_{code}.tif" for code in SCANNER_FOLDERS}
    if len(low_files) != len({f.name for f in low_files}):
        raise ValueError("Duplicate 2x input names")
    unexpected = sorted(f.name for f in low_files if f.name not in names.values())
    if unexpected:
        raise ValueError(f"Unexpected 2x TIFFs: {unexpected}")
    low_scanners = {code for code, name in names.items() if (low_dir / name).is_file()}
    if "S210" not in low_scanners:
        raise ValueError("S210 2x reference is absent")
    high_files = {}
    for code, folder in SCANNER_FOLDERS.items():
        candidate = scanner_root / folder / "40x" / f"{slide}.ome.tif"
        if candidate.is_file():
            high_files[code] = candidate
    if low_scanners != set(high_files):
        missing = sorted(low_scanners - set(high_files))
        extra = sorted(set(high_files) - low_scanners)
        raise ValueError(f"2x/40x scanner mismatch: missing 40x={missing}; extra 40x={extra}")

    rows = []
    output_dir = tissue_dir / "40x" / "registered"
    for code in SCANNER_FOLDERS:
        if code not in low_scanners:
            continue
        stem = f"{slide}_{code}"
        global_mat = warps / f"{stem}.mat"
        elastic_mat = warps / "D" / f"{stem}.mat"
        if not global_mat.is_file() or (code != "S210" and not elastic_mat.is_file()):
            raise ValueError(f"Missing CODA warp for {stem}")
        if code == "S210" and "tform_python" in load_mat_file(global_mat):
            raise ValueError("S210 MAT appears to be a moving-image transform")
        source = high_files[code]
        with tifffile.TiffFile(source) as tif:
            shape = tif.series[0].shape
        if len(shape) != 3 or shape[-1] != 3:
            raise ValueError(f"Expected RGB OME-TIFF: {source}")
        mppx, mppy = read_tiff_mpp(source)
        if not (0.2 <= mppx <= 0.3 and abs(mppx - mppy) < 0.001):
            raise ValueError(f"Unexpected 40x pixel scale for {code}: {mppx}, {mppy}")
        destination = output_dir / f"{stem}.ome.tif"
        rows.append({
            "slide_id": slide, "scanner": code, "source_40x": str(source),
            "source_bytes": source.stat().st_size, "height": shape[0], "width": shape[1],
            "mpp_x": mppx, "mpp_y": mppy, "transform_name": stem,
            "warps_dir": str(warps), "destination_40x": str(destination),
            "destination_exists": destination.exists(),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slide", required=True)
    parser.add_argument("--scanner-root", required=True, type=Path)
    parser.add_argument("--tissue-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output CSV already exists; choose a new path")
    rows = build_rows(args.slide, args.scanner_root, args.tissue_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Validated {len(rows)} 2x/40x scanner pairs; wrote {args.output}")


if __name__ == "__main__":
    main()
