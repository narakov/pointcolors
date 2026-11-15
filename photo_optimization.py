"""Minimum-cardinality photo cover, with full LAS verification and witness cuts.

The guarantee concerns point-to-image projection in the supplied camera model.
Occlusions, photo content and calibration correctness cannot be inferred here.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csc_array

from colorize_las import (
    compute_camera_pose,
    corrected_to_raw_pixels,
    iter_point_chunks,
    load_calibration,
    load_iml,
    load_las_header,
    points_from_chunk,
    projection_bounds,
    projection_candidate_indices,
)
from photo_selection import DEFAULT_OPTIONS, export_plan, sample_points


def polygon_area(polygon):
    polygon = np.asarray(polygon)
    if len(polygon) < 3:
        return 0.0
    # Translate first to reduce cancellation at projected map coordinates.
    q = polygon - polygon[0]
    return (
        abs(float(np.dot(q[:, 0], np.roll(q[:, 1], -1)) - np.dot(q[:, 1], np.roll(q[:, 0], -1))))
        / 2
    )


def intersect_polygons(subject, clip):
    from scipy.spatial import ConvexHull

    subject = [np.asarray(p, dtype=float) for p in subject]
    clip = np.asarray(clip, dtype=float)
    clip = clip[ConvexHull(clip).vertices]

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    for i, a in enumerate(clip):
        edge = clip[(i + 1) % len(clip)] - a
        original, subject = subject, []
        if not original:
            break
        previous = original[-1]
        d_previous = cross(edge, previous - a)
        for point in original:
            d = cross(edge, point - a)
            inside, was_inside = d >= -1e-9, d_previous >= -1e-9
            if inside != was_inside:
                subject.append(previous + (point - previous) * (d_previous / (d_previous - d)))
            if inside:
                subject.append(point)
            previous, d_previous = point, d
    return np.asarray(subject)


def ground_footprint(pose, calibration, height):
    width, h = calibration.image_size
    corners = np.array([[0, 0], [width - 1, 0], [width - 1, h - 1], [0, h - 1]], dtype=float)
    xy = corners - [
        width / 2 + calibration.principal_point[0],
        h / 2 + calibration.principal_point[1],
    ]
    rays = (
        xy[:, :1] * pose.axis_x
        + xy[:, 1:] * pose.axis_y
        + calibration.focal_length_pixels * pose.axis_z
    )
    if np.any(np.abs(rays[:, 2]) < 1e-12):
        return None
    t = (height - pose.center[2]) / rays[:, 2]
    if np.any(t <= 0):
        return None
    return (pose.center + t[:, None] * rays)[:, :2]


def overlap_summary(model, selected, ground_z):
    _, calibration, entries = model.scenarios[0]
    polygons = [ground_footprint(entries[i][0][0], calibration, ground_z) for i in selected]
    values = []
    for i, a in enumerate(polygons):
        if a is None or polygon_area(a) == 0:
            continue
        for b in polygons[i + 1 :]:
            if b is None or polygon_area(b) == 0:
                continue
            intersection = polygon_area(intersect_polygons(a, b))
            if intersection > 1e-6:
                values.append(100 * intersection / min(polygon_area(a), polygon_area(b)))
    return {
        "reference_height_m": ground_z,
        "cal": model.scenarios[0][0],
        "projection": "roll-minus",
        "metric": "intersection area / smaller full image ground footprint; overlapping pairs only",
        "overlapping_pairs": len(values),
        "min_percent": round(min(values), 3) if values else 0,
        "median_percent": round(float(np.median(values)), 3) if values else 0,
        "max_percent": round(max(values), 3) if values else 0,
        "note": "Approximate planar footprints at reference height; this is not a fixed pairwise overlap guarantee.",
    }


class CoverageModel:
    def __init__(self, plan):
        self.header = load_las_header(Path(plan["las"]))
        self.names = [p["image"] for p in plan["photos"]]
        self.scenarios = []
        cal_paths = plan.get("cal_candidates") or [plan["cal"]]
        records = {r.image_name: r for r in load_iml(Path(plan["iml"]))}
        for path in cal_paths:
            cal = load_calibration(Path(path))
            entries = []
            for name in self.names:
                poses = []
                for options in DEFAULT_OPTIONS:
                    pose = compute_camera_pose(
                        records[name],
                        cal,
                        Path(plan["iml"]).parent,
                        sum((self.header.min_xyz[2], self.header.max_xyz[2])) / 2,
                        options,
                    )
                    box = projection_bounds(
                        pose, cal, self.header.min_xyz[2], self.header.max_xyz[2]
                    )
                    poses.append((pose, box))
                entries.append(poses)
            self.scenarios.append((str(path), cal, entries))

    def masks(self, points, scenario, overlap_percent=0, selected=None):
        _, cal, entries = self.scenarios[scenario]
        indices = list(range(len(entries))) if selected is None else list(selected)
        raw = np.zeros((len(points), len(indices)), dtype=bool)
        inner = np.zeros_like(raw)
        width, height = cal.image_size
        # Two adjoining equally sized axis-aligned frames need p/2 inset on
        # each side for a p-wide shared strip. Rotated/irregular frames differ.
        xpad, ypad = width * overlap_percent / 200, height * overlap_percent / 200
        for column, index in enumerate(indices):
            for pose, box in entries[index]:
                ids = projection_candidate_indices(
                    points, box, self.header.min_xyz[2], self.header.max_xyz[2]
                )
                if ids is None:
                    ids = np.arange(len(points))
                if not len(ids):
                    continue
                relative = points[ids] - pose.center
                depth = relative @ pose.axis_z
                front = depth > 0
                ids, relative, depth = ids[front], relative[front], depth[front]
                if not len(ids):
                    continue
                x = (
                    cal.focal_length_pixels * (relative @ pose.axis_x) / depth
                    + cal.principal_point[0]
                )
                y = (
                    cal.focal_length_pixels * (relative @ pose.axis_y) / depth
                    + cal.principal_point[1]
                )
                x, y = corrected_to_raw_pixels(cal, x, y)
                x, y = x + width / 2, y + height / 2
                valid = (
                    (x >= cal.margin)
                    & (x <= width - 1 - cal.margin)
                    & (y >= cal.margin)
                    & (y <= height - 1 - cal.margin)
                )
                safe = (
                    valid
                    & (x >= cal.margin + xpad)
                    & (x <= width - 1 - cal.margin - xpad)
                    & (y >= cal.margin + ypad)
                    & (y <= height - 1 - cal.margin - ypad)
                )
                raw[ids[valid], column] = True
                inner[ids[safe], column] = True
        return raw, inner


def add_patterns(patterns, raw, inner):
    if not len(raw):
        return
    use_inner = inner.any(axis=1)
    requirements = np.where(use_inner[:, None], inner, raw)
    requirements = requirements[requirements.any(axis=1)]
    if len(requirements):
        packed = np.unique(np.packbits(requirements, axis=1), axis=0)
        patterns.update(row.tobytes() for row in packed)


def solve_cover(patterns, n_images, time_limit=60):
    if not patterns:
        raise ValueError("The original photo set does not cover any points.")
    packed = np.frombuffer(b"".join(sorted(patterns)), dtype=np.uint8).reshape(len(patterns), -1)
    matrix = np.unpackbits(packed, axis=1)[:, :n_images].astype(bool)
    result = milp(
        c=np.ones(n_images),
        integrality=np.ones(n_images),
        bounds=Bounds(0, 1),
        constraints=LinearConstraint(csc_array(matrix.astype(float)), 1, np.inf),
        options={"time_limit": time_limit, "mip_rel_gap": 0.0},
    )
    if result.x is None:
        # A safe feasible incumbent if the exact solver has no solution yet.
        remaining = np.ones(len(matrix), dtype=bool)
        selected = []
        while remaining.any():
            index = int(matrix[remaining].sum(axis=0).argmax())
            selected.append(index)
            remaining &= ~matrix[:, index]
    else:
        selected = np.flatnonzero(result.x > 0.5).tolist()
    if not matrix[:, selected].any(axis=1).all():
        raise RuntimeError(
            "The solver returned a set that does not satisfy the coverage requirements."
        )
    lower = getattr(result, "mip_dual_bound", None)
    return selected, {
        "solver_status": int(result.status),
        "optimal": bool(result.success),
        "lower_bound": math.ceil(float(lower) - 1e-7)
        if lower is not None and math.isfinite(lower)
        else None,
        "solver_message": str(result.message),
    }


def verify_full(model, selected, overlap_percent, patterns, on_progress):
    header = model.header
    stats = [
        {
            "cal": scenario[0],
            "points": 0,
            "selected_covered": 0,
            "max_set_uncovered": 0,
            "constraint_violations": 0,
            "margin_relaxed_points": 0,
            "points_in_overlap": 0,
        }
        for scenario in model.scenarios
    ]
    for start, count, data in iter_point_chunks(Path(model.plan_las), header, 250000):
        coordinates = points_from_chunk(data, header.point_record_length)
        points = np.column_stack(
            [coordinates[i].astype(float) * header.scales[i] + header.offsets[i] for i in range(3)]
        )
        for scenario, row in enumerate(stats):
            raw, inner = model.masks(points, scenario, overlap_percent, selected)
            raw_any = raw.any(axis=1)
            row["points"] += count
            row["selected_covered"] += int(raw_any.sum())
            row["points_in_overlap"] += int((raw.sum(axis=1) >= 2).sum())
            uncertain = ~inner.any(axis=1)
            if uncertain.any():
                all_raw, all_inner = model.masks(points[uncertain], scenario, overlap_percent)
                has_safe = all_inner.any(axis=1)
                has_raw = all_raw.any(axis=1)
                requirements = np.where(has_safe[:, None], all_inner, all_raw)
                violated = has_raw & ~requirements[:, selected].any(axis=1)
                row["constraint_violations"] += int(violated.sum())
                row["max_set_uncovered"] += int((~has_raw).sum())
                row["margin_relaxed_points"] += int((has_raw & ~has_safe).sum())
                if violated.any():
                    add_patterns(patterns, all_raw[violated], all_inner[violated])
        if on_progress and (
            start == 0 or (start // 250000) % 8 == 0 or start + count == header.point_count
        ):
            on_progress(f"Checking all points: {start + count:,} / {header.point_count:,}")
    for row in stats:
        row["coverage_percent"] = round(100 * row["selected_covered"] / row["points"], 6)
        row["points_in_overlap_percent"] = round(
            100 * row["points_in_overlap"] / max(1, row["selected_covered"]), 3
        )
    return stats


def optimize_plan(plan, overlap_percent=0, on_progress=None, time_limit=60, max_rounds=8):
    if overlap_percent not in (0, 5, 10):
        raise ValueError("Choose a joint allowance of 0, 5 or 10%.")
    started = time.monotonic()
    model = CoverageModel(plan)
    model.plan_las = plan["las"]
    points = sample_points(Path(plan["las"]), model.header, limit=30000)
    patterns = set()
    for scenario in range(len(model.scenarios)):
        raw, inner = model.masks(points, scenario, overlap_percent)
        add_patterns(patterns, raw, inner)
    for iteration in range(max_rounds):
        if on_progress:
            on_progress(f"Selecting photos (round {iteration + 1})…")
        selected, solver = solve_cover(patterns, len(model.names), time_limit=time_limit)
        if on_progress:
            on_progress(f"{len(selected)} photos selected. Checking every LAS point…")
        stats = verify_full(model, selected, overlap_percent, patterns, on_progress)
        if not any(row["constraint_violations"] for row in stats):
            result = dict(plan)
            wanted = {model.names[i] for i in selected}
            result["photos"] = [p for p in plan["photos"] if p["image"] in wanted]
            result["candidate_count"] = len(wanted)
            result["original_candidate_count"] = len(model.names)
            result["optimization"] = {
                "method": "binary set cover with full-LAS witness refinement",
                "minimum_proven_in_camera_model": solver["optimal"],
                "target_overlap_percent": overlap_percent,
                "edge_inset_percent_per_side": overlap_percent / 2,
                "iterations": iteration + 1,
                "constraint_patterns": len(patterns),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "full_las_verified": True,
                "scenarios": stats,
                **solver,
                "overlap_note": "5/10% is a target joint safety allowance using inset image bounds, not a guarantee of pairwise footprint intersection area. Edge allowance is relaxed only where all candidates lack inset coverage.",
                "scope": "All LAS points coverable by the original candidate set, for each supplied CAL and either Enhanced projection. No occlusion or photo-quality guarantee.",
            }
            result["optimization"]["footprint_overlap"] = overlap_summary(
                model, selected, float(np.median(points[:, 2]))
            )
            return result
    raise RuntimeError(
        "Full coverage could not be verified within the refinement limit. Use the full set. The unverified reduced set was not exported."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Minimum photo set with full LAS point verification"
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overlap", type=int, choices=[0, 5, 10], default=0)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    result = optimize_plan(plan, args.overlap, on_progress=lambda line: print(line, flush=True))
    export_plan(result, args.output_dir)
    print(json.dumps(result["optimization"], ensure_ascii=False), flush=True)
    print(f"PHOTOS={result['candidate_count']}", flush=True)


if __name__ == "__main__":
    main()
