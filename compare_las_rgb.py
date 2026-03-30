#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from colorize_las import iter_point_chunks, load_las_header, points_from_chunk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare RGB values in two or more colored LAS files against a reference LAS."
    )
    parser.add_argument("--reference", required=True, type=Path, help="Reference colored LAS, for example Bentley output.")
    parser.add_argument("--candidate", required=True, action="append", type=Path, help="Candidate colored LAS to compare.")
    parser.add_argument("--chunk-size", type=int, default=500_000, help="Points per comparison chunk.")
    return parser.parse_args()


def iter_xyz_rgb8(path: Path, chunk_size: int):
    header = load_las_header(path)
    if header.point_format != 3:
        raise ValueError(f"{path} must be RGB LAS point format 3 for this comparison script.")

    rgb_offset = 28
    for _, count, data in iter_point_chunks(path, header, chunk_size):
        x_raw, y_raw, z_raw = points_from_chunk(data, header.point_record_length)
        xyz = np.column_stack(
            (
                x_raw.astype(np.float64) * header.scales[0] + header.offsets[0],
                y_raw.astype(np.float64) * header.scales[1] + header.offsets[1],
                z_raw.astype(np.float64) * header.scales[2] + header.offsets[2],
            )
        )
        arr = np.frombuffer(data, dtype=np.uint8).reshape(count, header.point_record_length)
        rgb16 = arr[:, rgb_offset : rgb_offset + 6].view("<u2").reshape(count, 3)
        rgb8 = (rgb16 // 256).astype(np.uint8)
        yield xyz, rgb8


def compare_pair(reference: Path, candidate: Path, chunk_size: int) -> dict[str, float | int | list[float] | list[int]]:
    ref_header = load_las_header(reference)
    cand_header = load_las_header(candidate)
    if ref_header.point_count != cand_header.point_count:
        raise ValueError(
            f"Point count mismatch: {reference.name} has {ref_header.point_count}, "
            f"{candidate.name} has {cand_header.point_count}."
        )

    stats = {
        "points": 0,
        "coord_equal": 0,
        "coord_max_abs": np.zeros(3, dtype=np.float64),
        "ref_nonblack": 0,
        "cand_nonblack": 0,
        "both_nonblack": 0,
        "mask_equal": 0,
        "exact_rgb_match_all": 0,
        "exact_rgb_match_nonblack": 0,
        "sum_abs_all": np.zeros(3, dtype=np.float64),
        "sum_abs_nonblack": np.zeros(3, dtype=np.float64),
        "sum_sq_all": np.zeros(3, dtype=np.float64),
        "sum_sq_nonblack": np.zeros(3, dtype=np.float64),
        "max_abs_all": np.zeros(3, dtype=np.int64),
    }

    ref_iter = iter_xyz_rgb8(reference, chunk_size)
    cand_iter = iter_xyz_rgb8(candidate, chunk_size)
    for (xyz_ref, rgb_ref), (xyz_cand, rgb_cand) in zip(ref_iter, cand_iter):
        stats["points"] += len(rgb_ref)

        coord_diff = np.abs(xyz_ref - xyz_cand)
        same = np.all(coord_diff <= 1.0e-9, axis=1)
        stats["coord_equal"] += int(np.count_nonzero(same))
        stats["coord_max_abs"] = np.maximum(stats["coord_max_abs"], coord_diff.max(axis=0))

        ref_nonblack = np.any(rgb_ref != 0, axis=1)
        cand_nonblack = np.any(rgb_cand != 0, axis=1)
        both_nonblack = ref_nonblack & cand_nonblack
        stats["ref_nonblack"] += int(np.count_nonzero(ref_nonblack))
        stats["cand_nonblack"] += int(np.count_nonzero(cand_nonblack))
        stats["both_nonblack"] += int(np.count_nonzero(both_nonblack))
        stats["mask_equal"] += int(np.count_nonzero(ref_nonblack == cand_nonblack))

        abs_diff = np.abs(rgb_ref.astype(np.int16) - rgb_cand.astype(np.int16)).astype(np.int64)
        sq_diff = abs_diff.astype(np.float64) ** 2
        exact = np.all(abs_diff == 0, axis=1)
        stats["exact_rgb_match_all"] += int(np.count_nonzero(exact))
        stats["sum_abs_all"] += abs_diff.sum(axis=0)
        stats["sum_sq_all"] += sq_diff.sum(axis=0)
        stats["max_abs_all"] = np.maximum(stats["max_abs_all"], abs_diff.max(axis=0))

        if np.any(both_nonblack):
            abs_both = abs_diff[both_nonblack]
            sq_both = sq_diff[both_nonblack]
            exact_both = np.all(abs_both == 0, axis=1)
            stats["exact_rgb_match_nonblack"] += int(np.count_nonzero(exact_both))
            stats["sum_abs_nonblack"] += abs_both.sum(axis=0)
            stats["sum_sq_nonblack"] += sq_both.sum(axis=0)

    points = max(stats["points"], 1)
    both = max(stats["both_nonblack"], 1)
    return {
        "points": stats["points"],
        "coord_equal": stats["coord_equal"],
        "coord_max_abs": stats["coord_max_abs"].tolist(),
        "ref_nonblack": stats["ref_nonblack"],
        "cand_nonblack": stats["cand_nonblack"],
        "both_nonblack": stats["both_nonblack"],
        "mask_equal_pct": 100.0 * stats["mask_equal"] / points,
        "exact_rgb_match_all_pct": 100.0 * stats["exact_rgb_match_all"] / points,
        "exact_rgb_match_nonblack_pct": 100.0 * stats["exact_rgb_match_nonblack"] / both,
        "mean_abs_all": (stats["sum_abs_all"] / points).tolist(),
        "rmse_all": np.sqrt(stats["sum_sq_all"] / points).tolist(),
        "mean_abs_nonblack": (stats["sum_abs_nonblack"] / both).tolist(),
        "rmse_nonblack": np.sqrt(stats["sum_sq_nonblack"] / both).tolist(),
        "max_abs_all": stats["max_abs_all"].tolist(),
    }


def main() -> None:
    args = parse_args()
    for candidate in args.candidate:
        result = compare_pair(args.reference.resolve(), candidate.resolve(), args.chunk_size)
        print(candidate.name)
        print(f"  points: {result['points']}")
        print(
            f"  coordinate match: {result['coord_equal']} / {result['points']}, "
            f"max abs diff xyz: {np.round(result['coord_max_abs'], 12).tolist()}"
        )
        print(
            f"  nonblack reference/candidate/both: "
            f"{result['ref_nonblack']} / {result['cand_nonblack']} / {result['both_nonblack']}"
        )
        print(f"  mask equal: {result['mask_equal_pct']:.4f}%")
        print(f"  exact RGB match all points: {result['exact_rgb_match_all_pct']:.4f}%")
        print(f"  exact RGB match colored intersection: {result['exact_rgb_match_nonblack_pct']:.4f}%")
        print(f"  mean abs diff all: {np.round(result['mean_abs_all'], 4).tolist()}")
        print(f"  RMSE all: {np.round(result['rmse_all'], 4).tolist()}")
        print(f"  mean abs diff colored intersection: {np.round(result['mean_abs_nonblack'], 4).tolist()}")
        print(f"  RMSE colored intersection: {np.round(result['rmse_nonblack'], 4).tolist()}")
        print(f"  max abs diff: {result['max_abs_all']}")


if __name__ == "__main__":
    main()
