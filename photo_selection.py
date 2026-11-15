"""Choose photographs from LAS/CAL/IML metadata, without opening any photographs."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import numpy as np

from colorize_las import (
    PhotoProjectionOptions,
    approximate_footprint_bbox,
    bboxes_intersect,
    compute_camera_pose,
    load_calibration,
    load_iml,
    load_las_header,
    project_points,
)

DEFAULT_OPTIONS = [
    PhotoProjectionOptions("simple", "forward", "rxry", 1, -1, 1),
    PhotoProjectionOptions("simple", "forward", "rxry", 1, 1, 1),
]


def candidate_poses(records, calibration, header, image_root, options, margin=50.0):
    """Keep a conservative superset across the full LAS height interval.

    A bounding-box intersection is not proof of actual coverage. Unknown footprint
    geometry is retained rather than silently discarding a potentially useful image.
    """
    bounds = (header.min_xyz[0], header.max_xyz[0], header.min_xyz[1], header.max_xyz[1])
    zmin, zmax = header.min_xyz[2], header.max_xyz[2]
    selected = []
    for record in records:
        pose = compute_camera_pose(record, calibration, image_root, (zmin + zmax) / 2, options)
        footprints = [
            approximate_footprint_bbox(
                pose.center, pose.axis_x, pose.axis_y, pose.axis_z, calibration, z
            )
            for z in (zmin, (zmin + zmax) / 2, zmax)
        ]
        if any(fp is None for fp in footprints) or any(
            bboxes_intersect(fp, bounds, margin=margin) for fp in footprints if fp
        ):
            selected.append(pose)
    return selected


def sample_points(path: Path, header, limit=12000):
    if header.point_count < 1:
        raise ValueError("The LAS file contains no points.")
    if (
        path.stat().st_size
        < header.offset_to_point_data + header.point_count * header.point_record_length
    ):
        raise ValueError("The LAS file is truncated: its size does not match its point count.")
    dtype = np.dtype(
        {
            "names": ["x", "y", "z"],
            "formats": ["<i4"] * 3,
            "offsets": [0, 4, 8],
            "itemsize": header.point_record_length,
        }
    )
    data = np.memmap(
        path, dtype=dtype, mode="r", offset=header.offset_to_point_data, shape=(header.point_count,)
    )
    indices = np.linspace(0, header.point_count - 1, min(limit, header.point_count), dtype=np.int64)
    points = np.column_stack(
        [
            data[name][indices].astype(np.float64) * header.scales[i] + header.offsets[i]
            for i, name in enumerate(("x", "y", "z"))
        ]
    )
    del data
    return points


def plan_photos(las_path, cal_path, iml_path, *, sample=None, options=None):
    las_path, cal_path, iml_path = map(lambda p: Path(p).resolve(), (las_path, cal_path, iml_path))
    header, calibration, records = (
        load_las_header(las_path),
        load_calibration(cal_path),
        load_iml(iml_path),
    )
    if not records:
        raise ValueError(f"No Image/Xyz/Hrp records found in IML: {iml_path.name}")
    names = [r.image_name.casefold() for r in records]
    if len(names) != len(set(names)):
        raise ValueError(
            f"Duplicate photo names within IML: {iml_path.name}. The image-to-camera mapping is ambiguous."
        )
    points = sample if sample is not None else sample_points(las_path, header)
    sample_union = np.zeros(len(points), dtype=bool)
    candidates = {}
    for option in options or DEFAULT_OPTIONS:
        for pose in candidate_poses(records, calibration, header, iml_path.parent, option):
            entry = candidates.setdefault(
                pose.image_name, {"image": pose.image_name, "sample_hits": 0}
            )
            _, inside, _, _ = project_points(
                points, pose, calibration, (calibration.image_size[1], calibration.image_size[0], 3)
            )
            entry["sample_hits"] = max(entry["sample_hits"], len(inside))
            sample_union[inside] = True
    ordered = [candidates[r.image_name] for r in records if r.image_name in candidates]
    return {
        "las": str(las_path),
        "cal": str(cal_path),
        "iml": str(iml_path),
        "point_count": header.point_count,
        "bounds_min": header.min_xyz,
        "bounds_max": header.max_xyz,
        "listed_images": len(records),
        "candidate_count": len(ordered),
        "photos": ordered,
        "sample_size": len(points),
        "sample_covered": int(sample_union.sum()),
        "sample_coverage_percent": round(float(sample_union.mean()) * 100, 2),
        "selection": "conservative candidates; 50m margin; full LAS height range; standard and roll-minus projections",
        "note": "Sample coverage is a preliminary estimate; alignment accuracy and visibility are not guaranteed.",
    }


def analyze_folder(las_paths, data_root, cal_path=None, iml_path=None, on_progress=None):
    root = Path(data_root).resolve()
    found_cals, found_imls = [], []
    if not cal_path or not iml_path:
        for folder, directories, files in os.walk(root):
            directories[:] = [
                name
                for name in directories
                if name.lower() not in {".git", ".venv", ".uv-cache", "__pycache__", "photo_plans"}
            ]
            if "selection.json" in files and "selected.iml" in files:
                continue
            for name in files:
                path = Path(folder) / name
                if path.suffix.lower() == ".cal":
                    found_cals.append(path)
                elif path.suffix.lower() == ".iml":
                    found_imls.append(path)
    cals = [Path(cal_path).resolve()] if cal_path else sorted(found_cals)
    imls = [Path(iml_path).resolve()] if iml_path else sorted(found_imls)
    if not cals or not imls:
        raise ValueError(
            "CAL and IML files were not found. Choose a camera metadata folder or specify the files manually."
        )
    plans, errors = [], []
    total = len(las_paths) * len(cals) * len(imls)
    index = 0
    for las in las_paths:
        header = load_las_header(Path(las))
        points = sample_points(Path(las), header)
        for cal in cals:
            for iml in imls:
                index += 1
                if on_progress:
                    on_progress(f"Checking {index}/{total}: {iml.name} · {cal.name}")
                try:
                    plans.append(plan_photos(las, cal, iml, sample=points))
                except (ValueError, KeyError, OSError) as error:
                    errors.append(
                        {"las": str(las), "cal": str(cal), "iml": str(iml), "error": str(error)}
                    )
    plans.sort(key=lambda p: (p["sample_covered"], p["candidate_count"] > 0), reverse=True)
    return {"plans": plans, "errors": errors}


def reduced_iml(source: Path, wanted: set[str]):
    """Retain the original records, including Time, Camera and extra calibration fields."""
    header, blocks, current = [], [], []
    seen = False
    for line in source.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if re.match(r"^\s*Image\s*=", line):
            if current:
                blocks.append(current)
            current = [line]
            seen = True
        elif seen:
            current.append(line)
        else:
            header.append(line)
    if current:
        blocks.append(current)
    kept = [block for block in blocks if block[0].split("=", 1)[1].strip().casefold() in wanted]
    return (
        "\n".join(header).rstrip() + "\n" + "\n\n".join("\n".join(b).strip() for b in kept) + "\n"
    )


def export_plan(plan, output):
    if not plan["candidate_count"]:
        raise ValueError("No overlapping photos: check the coordinate system, IML and calibration.")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    names = [p["image"] for p in plan["photos"]]
    (output / "photos_needed.txt").write_text("\n".join(names) + "\n", encoding="utf-8-sig")
    (output / "selected.iml").write_text(
        reduced_iml(Path(plan["iml"]), {n.casefold() for n in names}), encoding="utf-8"
    )
    (output / "selection.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    calibration_text = (
        (
            "Calibration alternatives:\n"
            + "\n".join(plan["cal_candidates"])
            + "\nCoverage alone cannot identify the correct calibration. Confirm it using the survey records.\n"
        )
        if plan.get("cal_candidates")
        else f"Calibration: {plan['cal']}\n"
    )
    optimization = plan.get("optimization")
    if optimization:
        proof = (
            "Minimum photo count proven in the supplied camera model."
            if optimization["minimum_proven_in_camera_model"]
            else "Reduced photo set; minimum count not proven."
        )
        selection_text = (
            f"Reduced from {plan['original_candidate_count']} candidate photos. {proof}\n"
            f"All LAS points verified. Target joint allowance: {optimization['target_overlap_percent']}%.\n"
            "The allowance uses inset image bounds; it does not guarantee a fixed pairwise footprint overlap area.\n"
            + "\n".join(
                f"{Path(s['cal']).name}: {s['selected_covered']} / {s['points']} points covered; {s['points_in_overlap_percent']}% of covered points lie in overlap zones."
                for s in optimization["scenarios"]
            )
            + "\n"
        )
        if optimization.get("footprint_overlap"):
            overlap = optimization["footprint_overlap"]
            selection_text += (
                f"Footprint overlap at reference height {overlap['reference_height_m']:.2f} m: "
                f"median {overlap['median_percent']}%, range {overlap['min_percent']}–{overlap['max_percent']}%. "
                "Metric: intersection area / smaller footprint area, overlapping pairs only.\n"
            )
    else:
        selection_text = "Full candidate set: boundary photos are retained with a safety margin. Minimum count is not guaranteed.\n"
    instructions = (
        f"Photos to download: {len(names)} of {plan['listed_images']}.\n"
        f"Original IML: {plan['iml']}\n"
        + calibration_text
        + f"Sample coverage: {plan['sample_coverage_percent']}% (preliminary estimate).\n\n"
        "Download only the names listed in photos_needed.txt from your storage.\n"
        "Use photos from the flight corresponding to the original IML.\n"
        "Different flights may reuse JPG names; these photos are not interchangeable.\n"
        "In Point Colors, use selected.iml, the correct CAL and the folder containing downloaded photos.\n"
        + selection_text
        + "Metadata alone cannot verify sharpness, occlusion, alignment accuracy or color quality.\n"
    )
    (output / "README.txt").write_text(instructions, encoding="utf-8-sig")
    return output


def export_recommended_list(report, output):
    """Union the calibration alternatives for the best-ranked flight; don't guess a CAL."""
    if not report["plans"] or not report["plans"][0]["candidate_count"]:
        return None
    first = report["plans"][0]
    alternatives = [
        p for p in report["plans"] if (p["las"], p["iml"]) == (first["las"], first["iml"])
    ]
    names = {row["image"] for p in alternatives for row in p["photos"]}
    plan = dict(first)
    plan["photos"] = [
        {"image": r.image_name} for r in load_iml(Path(first["iml"])) if r.image_name in names
    ]
    plan["candidate_count"] = len(plan["photos"])
    plan["cal"] = ""
    plan["cal_candidates"] = list(dict.fromkeys(p["cal"] for p in alternatives))
    plan["recommendation"] = (
        "Highest sample coverage flight; confirm the flight identity. CAL is not automatically inferred."
    )
    return export_plan(plan, output)


def resolve_image_paths(names, image_root):
    """Resolve downloaded subsets, preserving explicit paths and refusing ambiguity."""
    root = Path(image_root).resolve()
    resolved, missing = {}, []
    for name in names:
        direct = (root / name).resolve()
        if not direct.is_relative_to(root):
            raise ValueError(f"Photo path is outside the images folder: {name}")
        if direct.is_file():
            resolved[name] = direct
        else:
            missing.append(name)
    if missing:
        needed = {Path(name).name.casefold() for name in missing}
        index = {}
        for path in root.rglob("*"):
            if path.is_file() and path.name.casefold() in needed:
                index.setdefault(path.name.casefold(), []).append(path)
        for name in missing:
            hits = index.get(Path(name).name.casefold(), [])
            if len(hits) > 1:
                raise ValueError(
                    f"Multiple photos named {name} were found. Choose the folder for one flight."
                )
            if hits:
                resolved[name] = hits[0]
    return resolved


def image_availability(las_paths, cal_path, iml_path, image_root, options=None):
    """Check the photos needed by the active camera models, without decoding JPGs."""
    calibration = load_calibration(Path(cal_path))
    records = load_iml(Path(iml_path))
    if not records:
        raise ValueError("The Image List contains no valid camera records.")
    required = set()
    for las_path in las_paths:
        header = load_las_header(Path(las_path))
        for option in options or DEFAULT_OPTIONS:
            required.update(
                pose.image_name
                for pose in candidate_poses(records, calibration, header, Path(image_root), option)
            )
    names = list(
        dict.fromkeys(record.image_name for record in records if record.image_name in required)
    )
    if not names:
        raise ValueError(
            "No IML photo overlaps the LAS. Check the coordinate system, flight and CAL."
        )
    paths = resolve_image_paths(names, image_root)
    return {
        "required": len(names),
        "found": len(paths),
        "missing": [name for name in names if name not in paths],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Select photos from metadata without downloading JPG files"
    )
    parser.add_argument("--las", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = analyze_folder([args.las], args.data_root, on_progress=print)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "all_candidates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for index, plan in enumerate(report["plans"], 1):
        print(
            plan["iml"],
            Path(plan["cal"]).name,
            plan["candidate_count"],
            plan["sample_coverage_percent"],
        )
        if plan["candidate_count"]:
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", Path(plan["cal"]).stem)
            export_plan(plan, args.output_dir / f"{index:02d}_{Path(plan['iml']).stem}_{safe}")
    export_recommended_list(report, args.output_dir / "recommended_download")


if __name__ == "__main__":
    main()
