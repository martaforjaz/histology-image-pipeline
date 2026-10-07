"""Validate WSI/2x/40x coverage using IDs from Slide_record.xlsm.

Read-only validation. The source workbook and all images remain unchanged.
P1000 is excluded by default because its conversion is still running.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
import tifffile

RAW_EXTENSIONS = {'.ndpi', '.ndp', '.svs', '.scn', '.mrxs', '.qptiff', '.tif', '.tiff',
                  '.czi', '.vsi', '.dcm', '.isyntax', '.i2syntax'}


def read_tiff_mpp(path: Path) -> tuple[float, float]:
    """Read X/Y micrometres per pixel from OME XML or calibrated TIFF tags."""
    with tifffile.TiffFile(path) as tif:
        if tif.ome_metadata:
            pixels = ET.fromstring(tif.ome_metadata).find('.//{*}Pixels')
            units = {'µm': 1, 'um': 1, 'nm': .001, 'mm': 1000, 'm': 1e6}
            if pixels is not None and all('PhysicalSize' + axis in pixels.attrib for axis in 'XY'):
                values = []
                for axis in 'XY':
                    unit = pixels.get('PhysicalSize' + axis + 'Unit', 'µm')
                    if unit not in units:
                        raise ValueError(f'Unsupported OME physical-size unit: {unit}')
                    values.append(float(pixels.get('PhysicalSize' + axis)) * units[unit])
                return values[0], values[1]
        tags = tif.pages[0].tags
        unit = tags.get('ResolutionUnit')
        factor = {2: 25400.0, 3: 10000.0}.get(int(unit.value) if unit else 1)
        if factor and 'XResolution' in tags and 'YResolution' in tags:
            def numeric(tag):
                value = tag.value
                return float(value[0]) / float(value[1]) if isinstance(value, tuple) else float(value)
            return factor / numeric(tags['XResolution']), factor / numeric(tags['YResolution'])
    raise ValueError(f'No physical pixel spacing in {path}')


SCANNER_COLUMNS = {
    "Hamamatsu_S210_40x": "Hamamatsu_S210_40x",
    "Hamamatsu_S360_40x": "Hamamatsu_S360_40x",
    "Leica Aperio GT 180_40x": "Leica",
    "Olympus VS200_40x": "Olympus VS200_40x",
    "Pramana_40x": "Pramana",
    "Roche Ventana_40x": "Roche Ventana_40x",
    "Zeiss_40x": "Zeiss_40x",
}
TARGET_MPP = {"2x": 5.0, "40x": 0.25}


def canonical(value: str) -> str:
    name = Path(str(value)).name.lower().strip()
    if name.endswith(".part"):
        name = name[:-5]
    for suffix in (".ome.tiff", ".ome.tif", ".tiff", ".tif", ".ndpi", ".ndp",
                   ".svs", ".mrxs", ".czi", ".vsi", ".dcm", ".isyntax", ".i2syntax"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9]+", "", name)


def read_slide_record(path: Path) -> tuple[list[dict], dict[str, int]]:
    wb = load_workbook(path, read_only=True, data_only=True, keep_vba=True)
    ws = wb["Sheet1"]
    headers = {str(ws.cell(1, c).value).strip(): c for c in range(1, ws.max_column + 1)
               if ws.cell(1, c).value is not None}
    required = ["slide label", "tissue type", "deidentified ID full", "deidentified ID short"]
    missing = [h for h in required if h not in headers]
    if missing:
        raise ValueError(f"Slide_record.xlsm is missing columns: {missing}")
    rows = []
    for r in range(2, ws.max_row + 1):
        short_id = ws.cell(r, headers["deidentified ID short"]).value
        if not short_id:
            continue
        item = {
            "source_row": r,
            "slide_label": ws.cell(r, headers["slide label"]).value,
            "tissue_type": ws.cell(r, headers["tissue type"]).value,
            "full_id": ws.cell(r, headers["deidentified ID full"]).value,
            "slide_id": str(short_id).strip(),
            "scanner_names": {},
        }
        for folder, header in SCANNER_COLUMNS.items():
            item["scanner_names"][folder] = ws.cell(r, headers[header]).value if header in headers else None
        rows.append(item)
    wb.close()
    if len({x["slide_id"].casefold() for x in rows}) != len(rows):
        raise ValueError("Duplicate deidentified short IDs in Slide_record.xlsm")
    return rows, headers


def inventory_folder(folder: Path, kind: str) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    if not folder.is_dir():
        return result
    for p in folder.iterdir():
        if not p.is_file():
            continue
        low = p.name.lower().removesuffix(".part")
        if kind == "WSI":
            if p.suffix.lower() not in RAW_EXTENSIONS:
                continue
        elif not low.endswith((".tif", ".tiff")):
            continue
        result.setdefault(canonical(p.name), []).append(p)
    return result


def candidates(row: dict, scanner: str) -> set[str]:
    values = [row["slide_id"], row.get("full_id"), row["scanner_names"].get(scanner)]
    return {canonical(v) for v in values if v}


def match(index: dict[str, list[Path]], names: set[str]) -> list[Path]:
    found = []
    for name in names:
        found.extend(index.get(name, []))
    return sorted(set(found))


def page_storage_ok(page, file_size: int) -> None:
    if page.dtype != np.dtype("uint8") or page.samplesperpixel != 3 or page.planarconfig != 1:
        raise ValueError("Expected 8-bit interleaved RGB")
    if page.imagewidth <= 0 or page.imagelength <= 0:
        raise ValueError("Invalid image dimensions")
    for offset, count in zip(page.dataoffsets, page.databytecounts):
        if offset < 8 or count <= 0 or offset + count > file_size:
            raise ValueError("Pixel block points outside the file")


def decode_samples(page, path: Path) -> None:
    tiled = page.is_tiled
    across = math.ceil(page.imagewidth / page.tilewidth) if tiled else 1
    down = math.ceil(page.imagelength / (page.tilelength if tiled else page.rowsperstrip))
    # Decode one central block: enough to confirm that stored pixels can be
    # opened, while keeping the dataset-wide check fast over network storage.
    indexes = [(down // 2) * across + across // 2]
    with path.open("rb", buffering=16 << 20) as handle:
        if (not tiled and page.compression == 1 and page.predictor == 1 and
                page.photometric == 2):
            # Large uncompressed 2x images may store many rows in one strip.
            # Reading a few rows directly validates distant regions without
            # allocating and decoding the entire (sometimes >64 MB) strip.
            width = min(256, page.imagewidth)
            for y in [page.imagelength // 2]:
                strip, row = divmod(y, page.rowsperstrip)
                for x in [max(0, (page.imagewidth-width)//2)]:
                    handle.seek(page.dataoffsets[strip] + (row * page.imagewidth + x) * 3)
                    data = handle.read(width * 3)
                    if len(data) != width * 3:
                        raise ValueError("Unexpected end of sampled pixel data")
            return
        for i in indexes:
            if i >= len(page.dataoffsets):
                raise ValueError("Stored pixel-block count does not match dimensions")
            count = page.databytecounts[i]
            if count > 64 << 20:
                raise RuntimeError("A sampled compressed block exceeds 64 MB")
            handle.seek(page.dataoffsets[i])
            data = handle.read(count)
            if len(data) != count:
                raise ValueError("Unexpected end of sampled pixel data")
            segment, _, _ = page.decode(data, i, jpegtables=page.jpegtables)
            if segment is None or not segment.size:
                raise ValueError("Sampled pixel block could not be decoded")


def validate_output(path: Path, resolution: str) -> dict:
    result = {"status": "FAIL", "reason": "", "path": str(path), "filename": path.name,
              "width": None, "height": None, "mpp_x": None, "mpp_y": None,
              "levels": None, "size_bytes": None}
    if path.name.lower().endswith(".part"):
        result.update(status="INCOMPLETE", reason="Temporary .part file; conversion did not finalize")
        return result
    try:
        notes = []
        before = path.stat()
        result["size_bytes"] = before.st_size
        if before.st_size <= 0:
            raise ValueError("Empty file")
        with path.open("rb", buffering=16 << 20) as handle, tifffile.TiffFile(handle) as tif:
            main = tif.pages[0]
            result["width"], result["height"] = main.imagewidth, main.imagelength
            target = TARGET_MPP[resolution]
            try:
                mx, my = read_tiff_mpp(path)
                result["mpp_x"], result["mpp_y"] = mx, my
                if not all(math.isclose(v, target, rel_tol=1e-3, abs_tol=1e-4) for v in (mx, my)):
                    raise ValueError(f"MPP is {mx:g} x {my:g}; expected {target:g} um/pixel")
            except ValueError as exc:
                if "No physical pixel spacing" not in str(exc):
                    raise
                notes.append(f"MPP metadata missing; expected {target:g} um/pixel")
            if tif.ome_metadata:
                pixels = ET.fromstring(tif.ome_metadata).find(".//{*}Pixels")
                if pixels is None or (int(pixels.get("SizeX", "0")), int(pixels.get("SizeY", "0"))) != (main.imagewidth, main.imagelength):
                    raise ValueError("OME dimensions disagree with TIFF dimensions")
            if resolution == "40x" and (not tif.is_ome or not main.pages):
                raise ValueError("40x output lacks OME metadata or pyramid levels")
            levels = [main] + list(main.pages or [])
            result["levels"] = len(levels)
            previous = (main.imagewidth, main.imagelength)
            for i, page in enumerate(levels):
                if page.imagewidth <= 0 or page.imagelength <= 0:
                    raise ValueError("Invalid image or pyramid dimensions")
                if i:
                    dims = (page.imagewidth, page.imagelength)
                    if not all(0 < a < b for a, b in zip(dims, previous)):
                        raise ValueError("Pyramid levels are not ordered from large to small")
                    previous = dims
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("File changed during validation")
        if notes:
            result.update(status="REVIEW", reason="; ".join(notes) + "; file opens and dimensions/pyramid are valid")
        else:
            result.update(status="PASS", reason="File opens; MPP, dimensions and pyramid structure passed")
    except RuntimeError as exc:
        result.update(status="REVIEW", reason=str(exc))
    except Exception as exc:
        result.update(status="FAIL", reason=f"{type(exc).__name__}: {exc}")
    return result


def validate_isolated(path: Path, resolution: str, timeout: float) -> dict:
    """Run one decoder in a child process so a slow/corrupt file cannot stall the batch."""
    with tempfile.TemporaryDirectory(prefix="slide-record-validation-") as tmp:
        request = Path(tmp) / "request.json"
        response = Path(tmp) / "response.json"
        request.write_text(json.dumps({"path": str(path), "resolution": resolution}), encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "--worker", str(request), str(response)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                process.kill()
            process.communicate(timeout=15)
            return {"status": "REVIEW", "reason": f"Validation exceeded {timeout:g} seconds",
                    "path": str(path), "filename": path.name, "width": None, "height": None,
                    "mpp_x": None, "mpp_y": None, "levels": None, "size_bytes": path.stat().st_size}
        if process.returncode != 0 or not response.exists():
            return {"status": "REVIEW", "reason": f"Validation worker failed: {(stderr or '').strip()[-500:]}",
                    "path": str(path), "filename": path.name, "width": None, "height": None,
                    "mpp_x": None, "mpp_y": None, "levels": None, "size_bytes": None}
        return json.loads(response.read_text(encoding="utf-8"))


def write_excel(report: dict, output: Path) -> None:
    """Write a colour-coded ID matrix plus a detailed audit sheet."""
    if len(report["scanners"]) != 1:
        raise ValueError("Excel export currently expects one --scanner per run")
    scanner = report["scanners"][0]
    keyed = {(r["slide_id"], r["kind"]): r for r in report["results"]}
    fills = {"PASS": "C6EFCE", "EXISTS": "C6EFCE", "REVIEW": "FFEB9C",
             "MISSING": "FFC7CE", "FAIL": "FFC7CE", "INCOMPLETE": "FFC7CE",
             "PROBLEM": "FFC7CE"}
    wb = Workbook()
    ws = wb.active
    ws.title = "Validation"
    ws.sheet_view.showGridLines = False
    ws.append(["Downsampling validation"])
    ws.append(["Scanner", scanner, "Slide IDs", len(report["slides"]), "Checked (UTC)", report["checked_at"]])
    ws.append([])
    ws.append(["Slide ID", "Tissue type", "Original WSI", "2x", "40x", "Overall"])
    for slide in report["slides"]:
        statuses = [keyed.get((slide["slide_id"], kind), {}).get("status", "MISSING")
                    for kind in ("WSI", "2x", "40x")]
        overall = ("PROBLEM" if any(s in {"MISSING", "FAIL", "INCOMPLETE"} for s in statuses)
                   else "REVIEW" if "REVIEW" in statuses else "PASS")
        ws.append([slide["slide_id"], slide.get("tissue_type") or "", *statuses, overall])
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="163A59")
        cell.font = Font(color="FFFFFF", bold=True, size=16)
    for cell in ws[4]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(color="FFFFFF", bold=True)
    for row in range(5, ws.max_row + 1):
        for col in range(3, 7):
            status = str(ws.cell(row, col).value)
            ws.cell(row, col).fill = PatternFill("solid", fgColor=fills.get(status, "FFFFFF"))
            ws.cell(row, col).alignment = Alignment(horizontal="center")
    for col, width in zip("ABCDEF", (28, 24, 17, 17, 17, 17)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "C5"
    ws.auto_filter.ref = f"A4:F{ws.max_row}"

    detail = wb.create_sheet("Details")
    headers = ["Slide ID", "Slide label", "Tissue type", "Type", "Status", "Reason",
               "Width", "Height", "MPP X", "MPP Y", "Pyramid levels", "File name", "Path"]
    detail.append(headers)
    for r in sorted(report["results"], key=lambda x: (x["slide_id"].casefold(), x["kind"])):
        detail.append([r["slide_id"], r.get("slide_label"), r.get("tissue_type"), r["kind"],
                       r["status"], r.get("reason"), r.get("width"), r.get("height"),
                       r.get("mpp_x"), r.get("mpp_y"), r.get("levels"), r.get("filename"), r.get("path")])
    for cell in detail[1]:
        cell.fill = PatternFill("solid", fgColor="243746")
        cell.font = Font(color="FFFFFF", bold=True)
    for row in range(2, detail.max_row + 1):
        status = str(detail.cell(row, 5).value)
        detail.cell(row, 5).fill = PatternFill("solid", fgColor=fills.get(status, "FFFFFF"))
        detail.cell(row, 6).alignment = Alignment(wrap_text=True, vertical="top")
    for col, width in zip("ABCDEFGHIJKLM", (28, 24, 24, 12, 12, 55, 14, 14, 12, 12, 16, 38, 75)):
        detail.column_dimensions[col].width = width
    detail.freeze_panes = "F2"
    detail.auto_filter.ref = detail.dimensions
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.xlsx")
    wb.save(temporary)
    temporary.replace(output)
    load_workbook(output, read_only=True).close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slide-record", type=Path)
    parser.add_argument("--scanner-root", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-excel", type=Path, help="Optional colour-coded .xlsx report")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--scanner", action="append", help="Validate only this scanner folder name (repeatable)")
    parser.add_argument("--worker", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        request, response = map(Path, args.worker)
        payload = json.loads(request.read_text(encoding="utf-8"))
        response.write_text(json.dumps(validate_output(Path(payload["path"]), payload["resolution"])), encoding="utf-8")
        return
    if not all((args.slide_record, args.scanner_root, args.output_json)):
        parser.error("--slide-record, --scanner-root and --output-json are required")
    rows, _ = read_slide_record(args.slide_record)
    scanners = [p for p in sorted(args.scanner_root.iterdir())
                if p.is_dir() and p.name in SCANNER_COLUMNS and "p1000" not in p.name.lower()]
    if args.scanner:
        wanted = {name.casefold() for name in args.scanner}
        scanners = [p for p in scanners if p.name.casefold() in wanted]
        missing_scanners = wanted - {p.name.casefold() for p in scanners}
        if missing_scanners:
            parser.error("Unknown or unavailable scanner(s): " + ", ".join(sorted(missing_scanners)))
    inventories = {}
    for scanner in scanners:
        inventories[scanner.name] = {
            "WSI": inventory_folder(scanner, "WSI"),
            "2x": inventory_folder(scanner / "2x", "2x"),
            "40x": inventory_folder(scanner / "40x", "40x"),
        }
    report = {"complete": False, "checked_at": datetime.now(timezone.utc).isoformat(),
              "slide_record": str(args.slide_record), "scanner_root": str(args.scanner_root),
              "p1000_excluded": True, "slides": rows, "scanners": [s.name for s in scanners],
              "results": []}
    jobs = []
    for row in rows:
        for scanner in scanners:
            names = candidates(row, scanner.name)
            raw = match(inventories[scanner.name]["WSI"], names)
            base = {"slide_id": row["slide_id"], "scanner": scanner.name,
                    "slide_label": row.get("slide_label"), "tissue_type": row.get("tissue_type")}
            if not raw:
                report["results"].append({**base, "kind": "WSI", "status": "MISSING", "reason": "Original WSI not found", "path": ""})
            elif len(raw) > 1:
                report["results"].append({**base, "kind": "WSI", "status": "REVIEW", "reason": f"Multiple matching originals ({len(raw)})", "path": "; ".join(map(str, raw))})
            else:
                try:
                    size = raw[0].stat().st_size
                    status, reason = ("EXISTS", "Original WSI exists") if size > 0 else ("FAIL", "Original WSI is empty")
                except OSError as exc:
                    status, reason = "FAIL", f"Cannot access original WSI: {exc}"
                report["results"].append({**base, "kind": "WSI", "status": status, "reason": reason, "path": str(raw[0])})
            for resolution in ("2x", "40x"):
                paths = match(inventories[scanner.name][resolution], names)
                final = [p for p in paths if not p.name.lower().endswith(".part")]
                parts = [p for p in paths if p.name.lower().endswith(".part")]
                if len(final) > 1:
                    report["results"].append({**base, "kind": resolution, "status": "REVIEW",
                        "reason": f"Multiple matching completed outputs ({len(final)})", "path": "; ".join(map(str, final))})
                elif len(final) == 1:
                    jobs.append((base, resolution, final[0]))
                elif parts:
                    report["results"].append({**base, "kind": resolution, "status": "INCOMPLETE",
                        "reason": "Only a temporary .part output exists", "path": "; ".join(map(str, parts))})
                else:
                    report["results"].append({**base, "kind": resolution, "status": "MISSING",
                        "reason": f"{resolution} output not found", "path": ""})
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Slides: {len(rows)}; scanners: {len(scanners)}; output files to validate: {len(jobs)}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(validate_isolated, path, resolution, args.timeout): (base, resolution)
                   for base, resolution, path in jobs}
        for future in as_completed(futures):
            base, resolution = futures[future]
            report["results"].append({**base, "kind": resolution, **future.result()})
            if len(report["results"]) % 25 == 0:
                args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"[{len(report['results'])}/{len(rows)*len(scanners)*3}] {base['scanner']} | {base['slide_id']} | {resolution} | {report['results'][-1]['status']}", flush=True)
    # Cross-resolution physical-field check for pairs that otherwise passed.
    keyed = {(r["slide_id"], r["scanner"], r["kind"]): r for r in report["results"]}
    for row in rows:
        for scanner in scanners:
            r2 = keyed.get((row["slide_id"], scanner.name, "2x"))
            r40 = keyed.get((row["slide_id"], scanner.name, "40x"))
            if r2 and r40 and r2.get("status") in ("PASS", "REVIEW") and r40.get("status") in ("PASS", "REVIEW"):
                mpp2 = (r2.get("mpp_x") or TARGET_MPP["2x"], r2.get("mpp_y") or TARGET_MPP["2x"])
                mpp40 = (r40.get("mpp_x") or TARGET_MPP["40x"], r40.get("mpp_y") or TARGET_MPP["40x"])
                extent2 = (r2["width"] * mpp2[0], r2["height"] * mpp2[1])
                extent40 = (r40["width"] * mpp40[0], r40["height"] * mpp40[1])
                errors = [abs(a-b) / max(a,b) for a,b in zip(extent2, extent40)]
                if max(errors) > 0.005:
                    reason = f"2x and 40x physical extents differ by up to {max(errors)*100:.2f}%"
                    r2.update(status="FAIL", reason=reason)
                    r40.update(status="FAIL", reason=reason)
                else:
                    r2["reason"] += "; physical field agrees with 40x"
                    r40["reason"] += "; physical field agrees with 2x"
    report["results"].sort(key=lambda r: (r["slide_id"].casefold(), r["scanner"], {"WSI":0,"40x":1,"2x":2}[r["kind"]]))
    report["complete"] = True
    args.output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.output_excel:
        write_excel(report, args.output_excel)
        print(f"Excel: {args.output_excel}", flush=True)
    print(f"Complete: {args.output_json}", flush=True)


if __name__ == "__main__":
    main()
