"""Simple, reusable validation of WSI, 2x and 40x downsampling outputs.

The script validates one scanner folder at a time. It uses an ID text file,
checks file presence, opens each present TIFF, checks dimensions and MPP when
available, checks the basic 40x pyramid structure, and reports unfinished or
invalid files. It does not modify images and does not decode every pixel.

Example:
    python validate_downsampling.py --scanner-path "D:\\Scanner types\\Hamamatsu_S210_40x"
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import tifffile


RAW_EXTENSIONS = {".ndpi", ".ndp", ".svs", ".scn", ".mrxs", ".qptiff",
                  ".tif", ".tiff", ".czi", ".vsi", ".dcm", ".isyntax", ".i2syntax"}
TARGET_MPP = {"2x": 5.0, "40x": 0.25}


def canonical(value: str) -> str:
    """Normalize an ID or filename for matching without changing the ID text."""
    name = Path(value.strip()).name.lower()
    if name.endswith(".part"):
        name = name[:-5]
    for suffix in (".ome.tiff", ".ome.tif", ".tiff", ".tif", ".ndpi", ".ndp",
                   ".svs", ".scn", ".mrxs", ".qptiff", ".czi", ".vsi", ".dcm",
                   ".isyntax", ".i2syntax"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "", name)


def read_ids(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if not value or value.casefold() in {"deidentified id short", "id", "slide id"}:
            continue
        ids.append(value)
    duplicates = [key for key, count in Counter(canonical(x) for x in ids).items() if count > 1]
    if duplicates:
        raise ValueError(f"Duplicate IDs in {path}: {duplicates[:5]}")
    return ids


def index_files(folder: Path, kind: str) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not folder.is_dir():
        return index
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if kind == "WSI" and path.suffix.lower() not in RAW_EXTENSIONS:
            continue
        if kind in {"2x", "40x"} and not path.name.lower().removesuffix(".part").endswith((".tif", ".tiff")):
            continue
        index.setdefault(canonical(path.name), []).append(path)
    return index


def read_mpp(path: Path) -> tuple[float, float] | None:
    """Read MPP from OME metadata or TIFF resolution tags, if present."""
    with tifffile.TiffFile(path) as tif:
        if tif.ome_metadata:
            pixels = ET.fromstring(tif.ome_metadata).find(".//{*}Pixels")
            units = {"µm": 1.0, "um": 1.0, "nm": 0.001, "mm": 1000.0, "m": 1e6}
            if pixels is not None and all(f"PhysicalSize{axis}" in pixels.attrib for axis in "XY"):
                values = []
                for axis in "XY":
                    unit = pixels.get(f"PhysicalSize{axis}Unit", "µm")
                    if unit not in units:
                        return None
                    values.append(float(pixels.get(f"PhysicalSize{axis}")) * units[unit])
                return values[0], values[1]
        tags = tif.pages[0].tags
        unit = tags.get("ResolutionUnit")
        factor = {2: 25400.0, 3: 10000.0}.get(int(unit.value) if unit else 1)
        if factor and "XResolution" in tags and "YResolution" in tags:
            def numeric(tag):
                value = tag.value
                return float(value[0]) / float(value[1]) if isinstance(value, tuple) else float(value)
            return factor / numeric(tags["XResolution"]), factor / numeric(tags["YResolution"])
    return None


def validate_output(path: Path, resolution: str) -> dict:
    result = {"status": "FAIL", "reason": "", "path": str(path), "filename": path.name,
              "width": None, "height": None, "mpp_x": None, "mpp_y": None, "levels": None}
    if path.name.casefold().endswith(".part"):
        result.update(status="INCOMPLETE", reason="Temporary .part file")
        return result
    try:
        with tifffile.TiffFile(path) as tif:
            main = tif.pages[0]
            result["width"], result["height"] = main.imagewidth, main.imagelength
            if result["width"] <= 0 or result["height"] <= 0:
                raise ValueError("Invalid image dimensions")
            mpp = read_mpp(path)
            if mpp:
                result["mpp_x"], result["mpp_y"] = mpp
                target = TARGET_MPP[resolution]
                if not all(math.isclose(value, target, rel_tol=1e-3, abs_tol=1e-4) for value in mpp):
                    raise ValueError(f"MPP {mpp[0]:g} x {mpp[1]:g}; expected {target:g} um/pixel")
            if resolution == "40x":
                if not tif.is_ome or not main.pages:
                    raise ValueError("Missing OME metadata or pyramid levels")
                previous = (main.imagewidth, main.imagelength)
                for level in main.pages:
                    dimensions = (level.imagewidth, level.imagelength)
                    if not all(0 < current < prior for current, prior in zip(dimensions, previous)):
                        raise ValueError("Pyramid dimensions are not strictly decreasing")
                    previous = dimensions
            levels = 1 + len(main.pages or [])
            result["levels"] = levels
        if mpp is None:
            result.update(status="REVIEW", reason="File opens and structure is valid, but MPP metadata is unavailable")
        else:
            result.update(status="PASS", reason="File opens; dimensions, MPP and structure passed")
    except Exception as exc:
        result.update(status="FAIL", reason=f"{type(exc).__name__}: {exc}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner-path", type=Path, required=True)
    parser.add_argument("--id-list", type=Path,
                        default=Path(__file__).with_name("list_IDshort.txt"))
    parser.add_argument("--output-dir", type=Path,
                        help="Folder for missing.txt, failed_review.txt and summary.json")
    args = parser.parse_args()
    if not args.scanner_path.is_dir():
        parser.error(f"Scanner folder does not exist: {args.scanner_path}")
    output_dir = args.output_dir or args.scanner_path / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    ids = read_ids(args.id_list)
    indexes = {kind: index_files(args.scanner_path if kind == "WSI" else args.scanner_path / kind, kind)
               for kind in ("WSI", "2x", "40x")}
    results = []
    for slide_id in ids:
        names = {canonical(slide_id)}
        for kind in ("WSI", "2x", "40x"):
            matches = [path for name in names for path in indexes[kind].get(name, [])]
            if not matches:
                results.append({"id": slide_id, "kind": kind, "status": "MISSING", "reason": "No matching file"})
            elif len(matches) > 1:
                results.append({"id": slide_id, "kind": kind, "status": "REVIEW",
                                "reason": f"Multiple matching files ({len(matches)})",
                                "path": "; ".join(str(path) for path in matches)})
            else:
                result = validate_output(matches[0], kind) if kind != "WSI" else {
                    "status": "EXISTS", "reason": "Original WSI exists", "path": str(matches[0])}
                result.update(id=slide_id, kind=kind)
                results.append(result)
    missing = [f"{r['id']}\t{r['kind']}\t{r.get('reason', '')}" for r in results if r["status"] == "MISSING"]
    failed = [f"{r['id']}\t{r['kind']}\t{r['status']}\t{r.get('reason', '')}" for r in results
              if r["status"] in {"FAIL", "REVIEW", "INCOMPLETE"}]
    (output_dir / "missing.txt").write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    (output_dir / "failed_review.txt").write_text("\n".join(failed) + ("\n" if failed else ""), encoding="utf-8")
    report = {"scanner_path": str(args.scanner_path), "id_list": str(args.id_list), "results": results}
    (output_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    counts = Counter(r["status"] for r in results)
    print(f"Scanner: {args.scanner_path.name}")
    print(f"IDs: {len(ids)} | checks: {len(results)}")
    print(" | ".join(f"{status}: {counts.get(status, 0)}" for status in ("EXISTS", "PASS", "MISSING", "FAIL", "REVIEW", "INCOMPLETE")))
    print(f"Missing list: {output_dir / 'missing.txt'}")
    print(f"Failed/review list: {output_dir / 'failed_review.txt'}")


if __name__ == "__main__":
    main()
