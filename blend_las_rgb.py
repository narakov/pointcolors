#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from colorize_las import load_las_header


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Blend two RGB LAS files by taking colors from the fallback file where the primary file is black."
    )
    parser.add_argument("--primary", required=True, type=Path, help="Primary RGB LAS file.")
    parser.add_argument("--fallback", required=True, type=Path, help="Fallback RGB LAS file.")
    parser.add_argument("--output", required=True, type=Path, help="Output blended RGB LAS file.")
    parser.add_argument("--chunk-size", type=int, default=500_000, help="Points per processing chunk.")
    return parser.parse_args()


def blend(primary: Path, fallback: Path, output: Path, chunk_size: int) -> None:
    primary_header = load_las_header(primary)
    fallback_header = load_las_header(fallback)

    if primary_header.point_format != 3 or fallback_header.point_format != 3:
        raise ValueError("Both input files must be RGB LAS point format 3.")
    if primary_header.point_count != fallback_header.point_count:
        raise ValueError("Point count mismatch between primary and fallback LAS files.")

    output.parent.mkdir(parents=True, exist_ok=True)
    rgb_offset = 28

    with primary.open("rb") as primary_handle, fallback.open("rb") as fallback_handle, output.open("wb") as output_handle:
        output_handle.write(primary_header.header_prefix)

        primary_handle.seek(primary_header.offset_to_point_data)
        fallback_handle.seek(fallback_header.offset_to_point_data)

        remaining = primary_header.point_count
        while remaining > 0:
            count = min(chunk_size, remaining)
            primary_bytes = primary_handle.read(count * primary_header.point_record_length)
            fallback_bytes = fallback_handle.read(count * fallback_header.point_record_length)

            primary_array = np.frombuffer(primary_bytes, dtype=np.uint8).reshape(count, primary_header.point_record_length).copy()
            fallback_array = np.frombuffer(fallback_bytes, dtype=np.uint8).reshape(count, fallback_header.point_record_length)

            primary_rgb = primary_array[:, rgb_offset : rgb_offset + 6].view("<u2").reshape(count, 3)
            fallback_rgb = fallback_array[:, rgb_offset : rgb_offset + 6].view("<u2").reshape(count, 3)
            missing = np.all(primary_rgb == 0, axis=1)
            primary_array[missing, rgb_offset : rgb_offset + 6] = fallback_array[missing, rgb_offset : rgb_offset + 6]

            output_handle.write(primary_array.tobytes())
            remaining -= count

        tail = primary_handle.read()
        if tail:
            output_handle.write(tail)


def main() -> None:
    args = parse_args()
    blend(
        primary=args.primary.resolve(),
        fallback=args.fallback.resolve(),
        output=args.output.resolve(),
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    main()
