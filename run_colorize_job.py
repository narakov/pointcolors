#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from blend_las_rgb import blend
from colorize_las import PhotoProjectionOptions, colorize


@dataclass(frozen=True)
class JobProfile:
    title: str
    description: str
    projection: PhotoProjectionOptions
    blend_with_standard: bool = False


PROFILES: dict[str, JobProfile] = {
    "standard": JobProfile(
        title="Standard",
        description="One pass. Baseline TerraPhoto-style projection with full coverage.",
        projection=PhotoProjectionOptions(
            projection_model="simple",
            camera_top="forward",
            rotation_order="rxry",
            attitude_corrections_sign=1,
            roll_sign=1,
            pitch_sign=1,
        ),
    ),
    "roll_minus": JobProfile(
        title="Roll Minus",
        description="One pass. Often improves color alignment where the roll sign is inverted.",
        projection=PhotoProjectionOptions(
            projection_model="simple",
            camera_top="forward",
            rotation_order="rxry",
            attitude_corrections_sign=1,
            roll_sign=-1,
            pitch_sign=1,
        ),
    ),
    "enhanced": JobProfile(
        title="Enhanced",
        description="Two passes. Uses Roll Minus as primary and fills black gaps from Standard.",
        projection=PhotoProjectionOptions(
            projection_model="simple",
            camera_top="forward",
            rotation_order="rxry",
            attitude_corrections_sign=1,
            roll_sign=-1,
            pitch_sign=1,
        ),
        blend_with_standard=True,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run RGB point cloud colorization with named processing profiles."
    )
    parser.add_argument(
        "--las",
        required=True,
        action="append",
        type=Path,
        help="Input LAS point cloud. Repeat --las to process multiple clouds in one run.",
    )
    parser.add_argument("--cal", required=True, type=Path)
    parser.add_argument("--iml", required=True, type=Path)
    parser.add_argument("--images-dir", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output LAS path for single-cloud mode.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for multi-cloud mode (optional).",
    )
    parser.add_argument(
        "--output-suffix",
        default="_gui_rgb",
        help="Suffix appended to each LAS stem when output path is generated automatically.",
    )
    parser.add_argument("--profile", choices=tuple(PROFILES.keys()), default="enhanced")
    parser.add_argument("--chunk-size", type=int, default=1_000_000)
    parser.add_argument("--default-color", default="0,0,0")
    parser.add_argument("--preview-dir", type=Path, help="Optional experimental preview stream.")
    return parser.parse_args()


def parse_default_color(value: str) -> np.ndarray:
    parts = re.findall(r"-?\d+", value)
    if len(parts) == 1:
        parts = [parts[0], parts[0], parts[0]]
    elif len(parts) == 2:
        parts = [parts[0], parts[1], "0"]
    elif len(parts) != 3:
        raise ValueError(
            "--default-color must contain one, two, or three values between 0 and 255."
        )
    values = [int(part) for part in parts]
    if any(component < 0 or component > 255 for component in values):
        raise ValueError("--default-color must contain values between 0 and 255.")
    return np.array(values, dtype=np.uint8)


def run_profile(
    las_path: Path,
    cal_path: Path,
    iml_path: Path,
    images_dir: Path,
    output_path: Path,
    profile_name: str,
    chunk_size: int,
    default_color: np.ndarray,
    preview=None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile = PROFILES[profile_name]
    print(f"Profile: {profile.title}")
    print(profile.description)
    progress = {"on_image": preview.update} if preview is not None else {}
    if preview is not None:
        preview.begin(las_path, profile.title, default_color)

    if profile.blend_with_standard:
        with tempfile.TemporaryDirectory(
            prefix="rgbpoint_job_", dir=str(output_path.parent.resolve())
        ) as temp_dir:
            temp_root = Path(temp_dir)
            primary_path = temp_root / "primary_roll_minus.las"
            fallback_path = temp_root / "fallback_standard.las"

            print("")
            print("Running primary pass: Roll Minus")
            if preview is not None:
                preview.set_pass("Pass 1/2 · Roll Minus")
            colorize(
                las_path=las_path,
                cal_path=cal_path,
                iml_path=iml_path,
                output_path=primary_path,
                image_dir=images_dir,
                selected_images=[],
                chunk_size=chunk_size,
                default_color=default_color,
                disable_overlap_filter=False,
                projection_options=profile.projection,
                **progress,
            )

            print("")
            print("Running fallback pass: Standard")
            if preview is not None:
                preview.set_pass("Pass 2/2 · Standard fallback", fallback=True)
            colorize(
                las_path=las_path,
                cal_path=cal_path,
                iml_path=iml_path,
                output_path=fallback_path,
                image_dir=images_dir,
                selected_images=[],
                chunk_size=chunk_size,
                default_color=default_color,
                disable_overlap_filter=False,
                projection_options=PROFILES["standard"].projection,
                **progress,
            )

            print("")
            print("Blending passes into final result")
            blend(primary_path, fallback_path, output_path, chunk_size)
            print(f"Output written to: {output_path}")
            if preview is not None:
                preview.finish(output_path)
        return

    if preview is not None:
        preview.set_pass(profile.title)
    colorize(
        las_path=las_path,
        cal_path=cal_path,
        iml_path=iml_path,
        output_path=output_path,
        image_dir=images_dir,
        selected_images=[],
        chunk_size=chunk_size,
        default_color=default_color,
        disable_overlap_filter=False,
        projection_options=profile.projection,
        **progress,
    )
    if preview is not None:
        preview.finish(output_path)


def resolve_outputs(args: argparse.Namespace, las_paths: list[Path]) -> list[Path]:
    if not las_paths:
        raise ValueError("At least one --las input is required.")

    if args.output is not None and len(las_paths) > 1:
        raise ValueError("Use --output only with a single --las input.")

    output_dir = args.output_dir.resolve() if args.output_dir is not None else None
    suffix = str(args.output_suffix or "").strip()

    if len(las_paths) == 1 and args.output is not None:
        return [args.output.resolve()]

    outputs: list[Path] = []
    for las_path in las_paths:
        target_dir = output_dir if output_dir is not None else las_path.parent
        target_name = f"{las_path.stem}{suffix}.LAS"
        outputs.append((target_dir / target_name).resolve())

    unique_outputs = {str(path).lower() for path in outputs}
    if len(unique_outputs) != len(outputs):
        raise ValueError(
            "Resolved output paths are not unique. "
            "Choose a different --output-dir or --output-suffix."
        )
    return outputs


def main() -> int:
    args = parse_args()
    default_color = parse_default_color(args.default_color)
    las_paths = [path.resolve() for path in args.las]
    output_paths = resolve_outputs(args, las_paths)
    preview = None
    if args.preview_dir is not None:
        from experimental_preview.stream import PreviewPublisher

        preview = PreviewPublisher(args.preview_dir)

    for index, (las_path, output_path) in enumerate(zip(las_paths, output_paths), start=1):
        if las_path == output_path:
            raise ValueError(f"Output LAS must be different from input LAS: {las_path}")

        if len(las_paths) > 1:
            print("")
            print(f"=== Cloud {index}/{len(las_paths)} ===")
            print(f"Input: {las_path}")
            print(f"Output: {output_path}")

        run_profile(
            las_path=las_path,
            cal_path=args.cal.resolve(),
            iml_path=args.iml.resolve(),
            images_dir=args.images_dir.resolve(),
            output_path=output_path,
            profile_name=args.profile,
            chunk_size=args.chunk_size,
            default_color=default_color,
            preview=preview,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
