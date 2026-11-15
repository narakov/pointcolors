#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import math
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

HEADER_SIGNATURE = b"LASF"
SUPPORTED_INPUT_FORMATS = {0, 1, 2, 3}
OUTPUT_FORMAT_FOR_INPUT = {0: 2, 1: 3, 2: 2, 3: 3}
STANDARD_POINT_LENGTH = {0: 20, 1: 28, 2: 26, 3: 34}
RGB_OFFSET = {2: 20, 3: 28}
ORTHO_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
WORLD_FILE_EXTENSIONS = (".jgw", ".jpgw", ".wld", ".world", ".tfw")


@dataclass(frozen=True)
class LasHeader:
    version_major: int
    version_minor: int
    header_size: int
    offset_to_point_data: int
    point_format: int
    raw_point_format_byte: int
    point_record_length: int
    point_count: int
    scales: tuple[float, float, float]
    offsets: tuple[float, float, float]
    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]
    header_prefix: bytes

    @property
    def has_rgb(self) -> bool:
        return self.point_format in RGB_OFFSET

    @property
    def output_point_format(self) -> int:
        return OUTPUT_FORMAT_FOR_INPUT[self.point_format]

    @property
    def output_record_length(self) -> int:
        if self.has_rgb:
            return self.point_record_length
        return self.point_record_length + 6


@dataclass(frozen=True)
class Calibration:
    image_size: tuple[int, int]
    principal_point: tuple[float, float, float]
    lever_arm: tuple[float, float, float]
    attitude_corrections_hrp: tuple[float, float, float]
    orientation: str
    margin: int
    lens_model: str
    lens_a: tuple[float, float, float]
    lens_k: tuple[float, float, float]
    lens_p: tuple[float, float]
    lens_r0: float

    @property
    def focal_length_pixels(self) -> float:
        return abs(self.principal_point[2])


@dataclass(frozen=True)
class ImageRecord:
    image_name: str
    xyz: tuple[float, float, float]
    hrp: tuple[float, float, float]


@dataclass(frozen=True)
class CameraPose:
    image_name: str
    image_path: Path
    center: np.ndarray
    axis_x: np.ndarray
    axis_y: np.ndarray
    axis_z: np.ndarray
    footprint_bbox: tuple[float, float, float, float] | None


@dataclass(frozen=True)
class PhotoProjectionOptions:
    projection_model: str
    camera_top: str
    rotation_order: str
    attitude_corrections_sign: int
    roll_sign: int
    pitch_sign: int


@dataclass(frozen=True)
class OrthoTile:
    image_name: str
    image_path: Path
    world_path: Path
    width: int
    height: int
    matrix: np.ndarray
    matrix_inv: np.ndarray
    origin: np.ndarray
    footprint_bbox: tuple[float, float, float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Colorize an uncompressed LAS point cloud by projecting TerraPhoto-style images."
    )
    parser.add_argument("--las", required=True, type=Path, help="Input LAS point cloud.")
    parser.add_argument(
        "--cal", type=Path, default=None, help="TerraPhoto camera calibration (*.cal)."
    )
    parser.add_argument("--iml", type=Path, default=None, help="TerraPhoto image list (*.iml).")
    parser.add_argument(
        "--ortho-dir",
        type=Path,
        default=None,
        help="Directory with georeferenced orthophoto tiles.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Output LAS with RGB.")
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=None,
        help="Directory with source JPG images. Defaults to the IML directory.",
    )
    parser.add_argument(
        "--image",
        dest="images",
        action="append",
        default=[],
        help="Restrict processing to the named image. Can be repeated.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=250_000,
        help="Points per processing chunk. Increase for speed if RAM allows.",
    )
    parser.add_argument(
        "--default-color",
        default="0,0,0",
        help="RGB color for points that were not covered by any image, for example 0,0,0.",
    )
    parser.add_argument(
        "--disable-overlap-filter",
        action="store_true",
        help="Do not prefilter images by approximate footprint overlap with the LAS bounding box.",
    )
    parser.add_argument(
        "--camera-top",
        choices=("forward", "backward", "right", "left"),
        default="forward",
        help="Direction of the image top relative to aircraft axes. Default: forward.",
    )
    parser.add_argument(
        "--projection-model",
        choices=("simple", "matrix"),
        default="simple",
        help="Photo projection model. Default: simple.",
    )
    parser.add_argument(
        "--rotation-order",
        choices=("rxry", "ryrx"),
        default="rxry",
        help="Order of roll/pitch body rotations for photo projection. Default: rxry.",
    )
    parser.add_argument(
        "--attitude-corrections-sign",
        choices=("plus", "minus"),
        default="plus",
        help="How to apply AttitudeCorrections(HRP) from the calibration file. Default: plus.",
    )
    parser.add_argument(
        "--roll-sign",
        choices=("plus", "minus"),
        default="plus",
        help="Sign of roll contribution in simple projection mode. Default: plus.",
    )
    parser.add_argument(
        "--pitch-sign",
        choices=("plus", "minus"),
        default="plus",
        help="Sign of pitch contribution in simple projection mode. Default: plus.",
    )
    return parser.parse_args()


def read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def parse_floats(value: str, expected: int) -> tuple[float, ...]:
    parts = tuple(float(part) for part in value.replace(";", " ").split())
    if len(parts) != expected:
        raise ValueError(f"Expected {expected} numeric values, got {value!r}")
    return parts


def parse_ints(value: str, expected: int) -> tuple[int, ...]:
    parts = tuple(int(part) for part in value.replace(";", " ").split())
    if len(parts) != expected:
        raise ValueError(f"Expected {expected} integer values, got {value!r}")
    return parts


def load_calibration(path: Path) -> Calibration:
    values = read_key_value_file(path)
    principal_point = parse_floats(values["PrincipalPoint(XoYoZo)"], 3)
    image_size = parse_ints(values["ImageSize"], 2)
    lever_arm = parse_floats(values.get("LeverArm", "0 0 0"), 3)
    attitude = parse_floats(values.get("AttitudeCorrections(HRP)", "0 0 0"), 3)
    orientation = values.get("Orientation", "TOP").strip().upper()
    margin = int(values.get("Margin", "0"))
    lens_model = values.get("LensModel", "").strip()

    if not lens_model:
        if any(name in values for name in ("LensK0", "LensK1", "LensK2")):
            lens_model = "Balanced"
        elif any(name in values for name in ("LensA3", "LensA5", "LensA7")):
            lens_model = "Function"
        else:
            lens_model = "Balanced"

    lens_a = (
        float(values.get("LensA3", "0")),
        float(values.get("LensA5", "0")),
        float(values.get("LensA7", "0")),
    )
    lens_k = (
        float(values.get("LensK0", "0")),
        float(values.get("LensK1", "0")),
        float(values.get("LensK2", "0")),
    )
    lens_p = (float(values.get("LensP1", "0")), float(values.get("LensP2", "0")))
    lens_r0 = float(values.get("LensR0", "0"))

    return Calibration(
        image_size=image_size,
        principal_point=principal_point,
        lever_arm=lever_arm,
        attitude_corrections_hrp=attitude,
        orientation=orientation,
        margin=margin,
        lens_model=lens_model.upper(),
        lens_a=lens_a,
        lens_k=lens_k,
        lens_p=lens_p,
        lens_r0=lens_r0,
    )


def load_iml(path: Path) -> list[ImageRecord]:
    records: list[ImageRecord] = []
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        if not current:
            return
        if {"Image", "Xyz", "Hrp"} - current.keys():
            current = {}
            return
        records.append(
            ImageRecord(
                image_name=current["Image"].strip(),
                xyz=parse_floats(current["Xyz"], 3),
                hrp=parse_floats(current["Hrp"], 3),
            )
        )
        current = {}

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("["):
                flush()
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            # Some TerraPhoto exports have no blank lines between image records.
            if key.strip() == "Image" and "Image" in current:
                flush()
            current[key.strip()] = value.strip()
    flush()
    return records


def load_las_header(path: Path) -> LasHeader:
    with path.open("rb") as handle:
        minimum_header = handle.read(375)
        if len(minimum_header) < 227:
            raise ValueError("File is too small to be a LAS file.")
        if minimum_header[:4] != HEADER_SIGNATURE:
            raise ValueError("Input file is not a LAS file.")
        version_major = minimum_header[24]
        version_minor = minimum_header[25]
        header_size = struct.unpack_from("<H", minimum_header, 94)[0]
        offset_to_point_data = struct.unpack_from("<I", minimum_header, 96)[0]
        raw_point_format_byte = minimum_header[104]
        if raw_point_format_byte & 0x80:
            raise ValueError(
                "Compressed LAZ data is not supported. Please use an uncompressed LAS."
            )
        point_format = raw_point_format_byte & 0x3F
        if point_format not in SUPPORTED_INPUT_FORMATS:
            raise ValueError(
                f"Point format {point_format} is not supported yet. Supported formats: 0, 1, 2, 3."
            )
        point_record_length = struct.unpack_from("<H", minimum_header, 105)[0]
        legacy_point_count = struct.unpack_from("<I", minimum_header, 107)[0]
        point_count = legacy_point_count
        if version_major > 1 or version_minor >= 4:
            if len(minimum_header) < 255:
                raise ValueError("LAS 1.4 header is truncated.")
            extended_point_count = struct.unpack_from("<Q", minimum_header, 247)[0]
            if extended_point_count:
                point_count = extended_point_count
        scales = struct.unpack_from("<ddd", minimum_header, 131)
        offsets = struct.unpack_from("<ddd", minimum_header, 155)
        max_x, min_x, max_y, min_y, max_z, min_z = struct.unpack_from("<6d", minimum_header, 179)
        handle.seek(0)
        header_prefix = handle.read(offset_to_point_data)

    if point_record_length < STANDARD_POINT_LENGTH[point_format]:
        raise ValueError(
            f"Point record length {point_record_length} is too small for point format {point_format}."
        )

    return LasHeader(
        version_major=version_major,
        version_minor=version_minor,
        header_size=header_size,
        offset_to_point_data=offset_to_point_data,
        point_format=point_format,
        raw_point_format_byte=raw_point_format_byte,
        point_record_length=point_record_length,
        point_count=point_count,
        scales=scales,
        offsets=offsets,
        min_xyz=(min_x, min_y, min_z),
        max_xyz=(max_x, max_y, max_z),
        header_prefix=header_prefix,
    )


def parse_world_file(path: Path) -> tuple[float, float, float, float, float, float]:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if len(lines) != 6:
        raise ValueError(f"World file {path} must contain exactly 6 numeric lines.")
    return tuple(float(line.replace(",", ".")) for line in lines)  # type: ignore[return-value]


def world_file_for_image(image_path: Path) -> Path | None:
    base = image_path.with_suffix("")
    candidates = [base.with_suffix(ext) for ext in WORLD_FILE_EXTENSIONS]
    suffixes = {image_path.suffix.lower()}
    if image_path.suffix:
        suffixes.add(image_path.suffix)
    for suffix in suffixes:
        candidates.append(image_path.with_suffix(suffix + "w"))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate
    return None


def build_ortho_tile(image_path: Path, world_path: Path) -> OrthoTile:
    with Image.open(image_path) as image:
        width, height = image.size

    a, d, b, e, c, f = parse_world_file(world_path)
    matrix = np.array([[a, b], [d, e]], dtype=np.float64)
    det = float(np.linalg.det(matrix))
    if abs(det) < 1.0e-12:
        raise ValueError(f"World file {world_path} has a singular affine transform.")

    matrix_inv = np.linalg.inv(matrix)
    origin = np.array([c, f], dtype=np.float64)
    corners = np.array(
        [
            [0.0, 0.0],
            [width - 1.0, 0.0],
            [width - 1.0, height - 1.0],
            [0.0, height - 1.0],
        ],
        dtype=np.float64,
    )
    world_xy = corners @ matrix.T + origin
    bbox = (
        float(np.min(world_xy[:, 0])),
        float(np.max(world_xy[:, 0])),
        float(np.min(world_xy[:, 1])),
        float(np.max(world_xy[:, 1])),
    )

    return OrthoTile(
        image_name=image_path.name,
        image_path=image_path,
        world_path=world_path,
        width=width,
        height=height,
        matrix=matrix,
        matrix_inv=matrix_inv,
        origin=origin,
        footprint_bbox=bbox,
    )


def load_ortho_tiles(
    ortho_dir: Path, selected_images: set[str]
) -> tuple[list[OrthoTile], list[str]]:
    tiles: list[OrthoTile] = []
    missing_world: list[str] = []
    for image_path in sorted(ortho_dir.iterdir()):
        if not image_path.is_file():
            continue
        if image_path.suffix.lower() not in ORTHO_IMAGE_EXTENSIONS:
            continue
        if selected_images and image_path.name.lower() not in selected_images:
            continue
        world_path = world_file_for_image(image_path)
        if world_path is None:
            missing_world.append(image_path.name)
            continue
        tiles.append(build_ortho_tile(image_path, world_path))
    return tiles, missing_world


def normalize(vector: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(vector)
    if length == 0:
        raise ValueError("Zero-length vector.")
    return vector / length


def reject(vector: np.ndarray, normal: np.ndarray) -> np.ndarray:
    return vector - np.dot(vector, normal) * normal


def compute_camera_pose(
    record: ImageRecord,
    calibration: Calibration,
    image_dir: Path,
    ground_z: float,
    projection_options: PhotoProjectionOptions,
) -> CameraPose:
    if calibration.orientation != "TOP":
        raise ValueError(f"Camera orientation {calibration.orientation!r} is not supported yet.")

    correction_sign = projection_options.attitude_corrections_sign
    heading_deg = record.hrp[0] + correction_sign * calibration.attitude_corrections_hrp[0]
    roll_deg = (
        record.hrp[1] + correction_sign * calibration.attitude_corrections_hrp[1]
    ) * projection_options.roll_sign
    pitch_deg = (
        record.hrp[2] + correction_sign * calibration.attitude_corrections_hrp[2]
    ) * projection_options.pitch_sign

    heading = math.radians(heading_deg)
    roll = math.radians(roll_deg)
    pitch = math.radians(pitch_deg)

    forward = np.array([math.sin(heading), math.cos(heading), 0.0], dtype=np.float64)
    right = np.array([math.cos(heading), -math.sin(heading), 0.0], dtype=np.float64)
    down = np.array([0.0, 0.0, -1.0], dtype=np.float64)

    if projection_options.projection_model == "simple":
        look = normalize(down + math.tan(pitch) * forward + math.tan(roll) * right)
        top_vector_map = {
            "forward": forward,
            "backward": -forward,
            "right": right,
            "left": -right,
        }
        image_up = reject(top_vector_map[projection_options.camera_top], look)
        if np.linalg.norm(image_up) < 1.0e-9:
            image_up = reject(np.array([0.0, 1.0, 0.0], dtype=np.float64), look)
        image_up = normalize(image_up)
        axis_y = -image_up
        axis_x = normalize(np.cross(axis_y, look))
        axis_y = normalize(np.cross(look, axis_x))

        lever_x, lever_y, lever_z = calibration.lever_arm
        camera_center = (
            np.array(record.xyz, dtype=np.float64)
            + lever_x * forward
            + lever_y * right
            + lever_z * down
        )
    else:
        level_body_axes = np.column_stack((forward, right, down))
        rotation_x = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, math.cos(roll), -math.sin(roll)],
                [0.0, math.sin(roll), math.cos(roll)],
            ],
            dtype=np.float64,
        )
        rotation_y = np.array(
            [
                [math.cos(pitch), 0.0, math.sin(pitch)],
                [0.0, 1.0, 0.0],
                [-math.sin(pitch), 0.0, math.cos(pitch)],
            ],
            dtype=np.float64,
        )
        if projection_options.rotation_order == "rxry":
            body_axes = level_body_axes @ (rotation_x @ rotation_y)
        else:
            body_axes = level_body_axes @ (rotation_y @ rotation_x)

        body_forward = body_axes[:, 0]
        body_right = body_axes[:, 1]
        look = normalize(body_axes[:, 2])

        top_vector_map = {
            "forward": body_forward,
            "backward": -body_forward,
            "right": body_right,
            "left": -body_right,
        }
        image_up = normalize(top_vector_map[projection_options.camera_top])
        axis_y = -image_up
        axis_x = normalize(np.cross(axis_y, look))
        axis_y = normalize(np.cross(look, axis_x))

        camera_center = np.array(record.xyz, dtype=np.float64) + body_axes @ np.array(
            calibration.lever_arm, dtype=np.float64
        )

    footprint = approximate_footprint_bbox(
        camera_center=camera_center,
        axis_x=axis_x,
        axis_y=axis_y,
        axis_z=look,
        calibration=calibration,
        ground_z=ground_z,
    )

    return CameraPose(
        image_name=record.image_name,
        image_path=image_dir / record.image_name,
        center=camera_center,
        axis_x=axis_x,
        axis_y=axis_y,
        axis_z=look,
        footprint_bbox=footprint,
    )


def approximate_footprint_bbox(
    camera_center: np.ndarray,
    axis_x: np.ndarray,
    axis_y: np.ndarray,
    axis_z: np.ndarray,
    calibration: Calibration,
    ground_z: float,
) -> tuple[float, float, float, float] | None:
    if abs(axis_z[2]) < 1.0e-9:
        return None

    focal = calibration.focal_length_pixels
    width, height = calibration.image_size
    cx = width / 2.0 + calibration.principal_point[0]
    cy = height / 2.0 + calibration.principal_point[1]
    corners = np.array(
        [
            [0.0, 0.0],
            [width - 1.0, 0.0],
            [width - 1.0, height - 1.0],
            [0.0, height - 1.0],
        ],
        dtype=np.float64,
    )
    image_xy = corners - np.array([cx, cy], dtype=np.float64)
    rays = (
        image_xy[:, 0:1] * axis_x.reshape(1, 3)
        + image_xy[:, 1:2] * axis_y.reshape(1, 3)
        + focal * axis_z.reshape(1, 3)
    )
    t = (ground_z - camera_center[2]) / rays[:, 2]
    valid = t > 0
    if not np.any(valid):
        return None
    intersections = camera_center.reshape(1, 3) + t[valid].reshape(-1, 1) * rays[valid]
    min_x = float(np.min(intersections[:, 0]))
    max_x = float(np.max(intersections[:, 0]))
    min_y = float(np.min(intersections[:, 1]))
    max_y = float(np.max(intersections[:, 1]))
    return (min_x, max_x, min_y, max_y)


def bboxes_intersect(
    bbox_xy: tuple[float, float, float, float],
    other_xy: tuple[float, float, float, float],
    margin: float = 0.0,
) -> bool:
    min_x, max_x, min_y, max_y = bbox_xy
    other_min_x, other_max_x, other_min_y, other_max_y = other_xy
    return not (
        max_x < other_min_x - margin
        or min_x > other_max_x + margin
        or max_y < other_min_y - margin
        or min_y > other_max_y + margin
    )


def projection_bounds(
    pose: CameraPose,
    calibration: Calibration,
    z_min: float,
    z_max: float,
    image_size: tuple[int, int] | None = None,
) -> tuple[float, float, float, float] | None:
    """Conservative XY bounds for a distortion-free, downward-looking camera."""
    if image_size is not None and image_size != calibration.image_size:
        return None
    if calibration.margin < 0:
        return None
    if any((*calibration.lens_a, *calibration.lens_k, *calibration.lens_p)):
        return None
    if pose.center[2] <= z_max:
        return None
    width, height = calibration.image_size
    corners = (
        np.array(
            [
                [-width / 2, -height / 2],
                [width / 2, -height / 2],
                [width / 2, height / 2],
                [-width / 2, height / 2],
            ]
        )
        - calibration.principal_point[:2]
    )
    ray_z = (
        corners[:, 0] * pose.axis_x[2]
        + corners[:, 1] * pose.axis_y[2]
        + calibration.focal_length_pixels * pose.axis_z[2]
    )
    if np.any(ray_z >= 0):
        return None
    boxes = [
        approximate_footprint_bbox(
            pose.center, pose.axis_x, pose.axis_y, pose.axis_z, calibration, z
        )
        for z in (z_min, z_max)
    ]
    if any(box is None for box in boxes):
        return None
    return (
        min(box[0] for box in boxes) - 1,
        max(box[1] for box in boxes) + 1,
        min(box[2] for box in boxes) - 1,
        max(box[3] for box in boxes) + 1,
    )


def projection_candidate_indices(points, bounds, z_min, z_max):
    if bounds is None:
        return None
    inside_xy = (
        (points[:, 0] >= bounds[0])
        & (points[:, 0] <= bounds[1])
        & (points[:, 1] >= bounds[2])
        & (points[:, 1] <= bounds[3])
    )
    outside_height = (points[:, 2] < z_min) | (points[:, 2] > z_max)
    return np.flatnonzero(inside_xy | outside_height)


def distortion_shift(
    calibration: Calibration, rx: np.ndarray, ry: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    d2 = rx * rx + ry * ry
    d4 = d2 * d2
    radial = np.zeros_like(rx)
    if calibration.lens_model == "BALANCED":
        k0, k1, k2 = calibration.lens_k
        radial = -(k0 + k1 * d2 + k2 * d4)
    elif calibration.lens_model == "FUNCTION":
        a3, a5, a7 = calibration.lens_a
        d6 = d4 * d2
        radial = -(a3 * d2 + a5 * d4 + a7 * d6)
    elif calibration.lens_model in {"ZERO", "ZERO RADIUS FUNCTION", "ZERO_RADIUS_FUNCTION"}:
        a3, a5, _ = calibration.lens_a
        r2 = calibration.lens_r0 * calibration.lens_r0
        r4 = r2 * r2
        radial = -((a3 * (d2 - r2)) + (a5 * (d4 - r4)))
    dx = rx * radial
    dy = ry * radial
    p1, p2 = calibration.lens_p
    if p1 != 0.0 or p2 != 0.0:
        dx = dx + p1 * (d2 + 2.0 * rx * rx) + 2.0 * p2 * rx * ry
        dy = dy + p2 * (d2 + 2.0 * ry * ry) + 2.0 * p1 * rx * ry
    return dx, dy


def corrected_to_raw_pixels(
    calibration: Calibration, x: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    if not any(
        abs(value) > 0.0
        for value in (
            *calibration.lens_a,
            *calibration.lens_k,
            *calibration.lens_p,
            calibration.lens_r0,
        )
    ):
        return x, y

    raw_x = x.copy()
    raw_y = y.copy()
    for _ in range(5):
        dx, dy = distortion_shift(calibration, raw_x, raw_y)
        raw_x = x - dx
        raw_y = y - dy
    return raw_x, raw_y


def iter_point_chunks(
    path: Path,
    header: LasHeader,
    chunk_size: int,
) -> Iterable[tuple[int, int, bytes]]:
    with path.open("rb") as handle:
        handle.seek(header.offset_to_point_data)
        start_index = 0
        while start_index < header.point_count:
            count = min(chunk_size, header.point_count - start_index)
            data = handle.read(count * header.point_record_length)
            if len(data) != count * header.point_record_length:
                raise ValueError("Unexpected end of LAS point data.")
            yield start_index, count, data
            start_index += count


def points_from_chunk(data: bytes, record_length: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    point_dtype = np.dtype(
        {
            "names": ["x", "y", "z"],
            "formats": ["<i4", "<i4", "<i4"],
            "offsets": [0, 4, 8],
            "itemsize": record_length,
        }
    )
    view = np.frombuffer(data, dtype=point_dtype)
    return view["x"], view["y"], view["z"]


class LasCoordinates:
    """Reuse decoded coordinates when the cloud fits the memory budget."""

    def __init__(self, path, header, chunk_size, cache_bytes=512 * 1024 * 1024):
        self.path = path
        self.header = header
        self.chunk_size = chunk_size
        self.cache_enabled = header.point_count * 3 * 8 <= cache_bytes
        self._blocks = None

    def __iter__(self):
        if self._blocks is not None:
            yield from self._blocks
            return
        blocks = []
        header = self.header
        for start, _, data in iter_point_chunks(self.path, header, self.chunk_size):
            raw = points_from_chunk(data, header.point_record_length)
            xyz = np.column_stack(
                tuple(
                    axis.astype(np.float64) * scale + offset
                    for axis, scale, offset in zip(raw, header.scales, header.offsets)
                )
            )
            xyz.flags.writeable = False
            if self.cache_enabled:
                blocks.append((start, xyz))
            yield start, xyz
        if self.cache_enabled:
            self._blocks = blocks


def rgb16_bytes(colors_u8: np.ndarray) -> np.ndarray:
    colors_u16 = colors_u8.astype("<u2") * 257
    return colors_u16.view(np.uint8).reshape(colors_u8.shape[0], 6)


def read_tail_bytes(path: Path, header: LasHeader) -> bytes:
    point_data_end = header.offset_to_point_data + header.point_count * header.point_record_length
    with path.open("rb") as handle:
        handle.seek(point_data_end)
        return handle.read()


def rewrite_header_for_output(header: LasHeader) -> bytes:
    output = bytearray(header.header_prefix)
    output[104] = (header.raw_point_format_byte & 0xC0) | header.output_point_format
    struct.pack_into("<H", output, 105, header.output_record_length)
    if header.version_major > 1 or header.version_minor >= 4:
        struct.pack_into("<Q", output, 247, header.point_count)
    else:
        struct.pack_into("<I", output, 107, header.point_count)
    return bytes(output)


def project_points(
    points_xyz: np.ndarray,
    pose: CameraPose,
    calibration: Calibration,
    image_shape: tuple[int, int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    relative = points_xyz - pose.center.reshape(1, 3)
    cam_x = relative @ pose.axis_x
    cam_y = relative @ pose.axis_y
    cam_z = relative @ pose.axis_z

    visible = cam_z > 0.0
    if not np.any(visible):
        empty = np.empty(0, dtype=np.float64)
        empty_i = np.empty(0, dtype=np.int64)
        return visible, empty_i, empty_i, empty

    focal = calibration.focal_length_pixels
    corrected_x = focal * cam_x[visible] / cam_z[visible] + calibration.principal_point[0]
    corrected_y = focal * cam_y[visible] / cam_z[visible] + calibration.principal_point[1]
    raw_x, raw_y = corrected_to_raw_pixels(calibration, corrected_x, corrected_y)

    height, width = image_shape[0], image_shape[1]
    pixel_x = raw_x + (width / 2.0)
    pixel_y = raw_y + (height / 2.0)

    margin = calibration.margin
    inside_visible = (
        (pixel_x >= margin)
        & (pixel_x <= (width - 1 - margin))
        & (pixel_y >= margin)
        & (pixel_y <= (height - 1 - margin))
    )
    visible_indices = np.flatnonzero(visible)
    inside_indices = visible_indices[inside_visible]
    if inside_indices.size == 0:
        empty = np.empty(0, dtype=np.float64)
        empty_i = np.empty(0, dtype=np.int64)
        return visible, empty_i, empty_i, empty

    sample_x = np.rint(pixel_x[inside_visible]).astype(np.int64)
    sample_y = np.rint(pixel_y[inside_visible]).astype(np.int64)
    radial_score = cam_z[inside_indices] / np.linalg.norm(relative[inside_indices], axis=1)
    return visible, inside_indices, np.stack((sample_x, sample_y), axis=1), radial_score


def project_points_to_ortho(
    points_xy: np.ndarray, tile: OrthoTile
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pixel_xy = (points_xy - tile.origin.reshape(1, 2)) @ tile.matrix_inv.T
    pixel_x = pixel_xy[:, 0]
    pixel_y = pixel_xy[:, 1]
    inside = (
        (pixel_x >= 0.0)
        & (pixel_x <= (tile.width - 1))
        & (pixel_y >= 0.0)
        & (pixel_y <= (tile.height - 1))
    )
    inside_indices = np.flatnonzero(inside)
    if inside_indices.size == 0:
        empty_i = np.empty(0, dtype=np.int64)
        empty_f = np.empty(0, dtype=np.float64)
        return empty_i, np.empty((0, 2), dtype=np.int64), empty_f

    pixel_x_inside = pixel_x[inside]
    pixel_y_inside = pixel_y[inside]
    sample_x = np.rint(pixel_x_inside).astype(np.int64)
    sample_y = np.rint(pixel_y_inside).astype(np.int64)
    edge_score = np.minimum.reduce(
        [
            pixel_x_inside,
            pixel_y_inside,
            (tile.width - 1) - pixel_x_inside,
            (tile.height - 1) - pixel_y_inside,
        ]
    )
    return inside_indices, np.stack((sample_x, sample_y), axis=1), edge_score


def parse_default_color(value: str) -> np.ndarray:
    parts = re.findall(r"-?\d+", value)
    if len(parts) == 1:
        parts = [parts[0], parts[0], parts[0]]
    elif len(parts) == 2:
        parts = [parts[0], parts[1], "0"]
    elif len(parts) != 3:
        raise ValueError("--default-color must be in R,G,B format.")
    values = [int(part) for part in parts]
    if any(component < 0 or component > 255 for component in values):
        raise ValueError("--default-color values must be between 0 and 255.")
    return np.array(values, dtype=np.uint8)


def write_output_las(
    las_path: Path,
    output_path: Path,
    header: LasHeader,
    chunk_size: int,
    scores: np.ndarray,
    colors: np.ndarray,
    tail_bytes: bytes,
) -> None:
    output_header = rewrite_header_for_output(header)

    with output_path.open("wb") as out_handle:
        out_handle.write(output_header)

        for start_index, count, chunk_bytes in iter_point_chunks(las_path, header, chunk_size):
            in_array = np.frombuffer(chunk_bytes, dtype=np.uint8).reshape(
                count, header.point_record_length
            )
            out_array: np.ndarray

            chunk_colors = colors[start_index : start_index + count]
            chunk_color_bytes = rgb16_bytes(chunk_colors)

            if header.has_rgb:
                out_array = in_array.copy()
                rgb_offset = RGB_OFFSET[header.point_format]
                color_mask = np.isfinite(scores[start_index : start_index + count])
                if np.any(color_mask):
                    out_array[color_mask, rgb_offset : rgb_offset + 6] = chunk_color_bytes[
                        color_mask
                    ]
            else:
                output_length = header.output_record_length
                standard_length = STANDARD_POINT_LENGTH[header.point_format]
                out_array = np.zeros((count, output_length), dtype=np.uint8)
                out_array[:, :standard_length] = in_array[:, :standard_length]
                out_array[:, standard_length : standard_length + 6] = chunk_color_bytes
                out_array[:, standard_length + 6 :] = in_array[:, standard_length:]

            out_handle.write(out_array.tobytes())

        if tail_bytes:
            out_handle.write(tail_bytes)


def colorize(
    las_path: Path,
    cal_path: Path,
    iml_path: Path,
    output_path: Path,
    image_dir: Path | None,
    selected_images: list[str],
    chunk_size: int,
    default_color: np.ndarray,
    disable_overlap_filter: bool,
    projection_options: PhotoProjectionOptions,
    on_image=None,
) -> None:
    if las_path.resolve() == output_path.resolve():
        raise ValueError("Output LAS must be different from the input LAS.")

    calibration = load_calibration(cal_path)
    image_records = load_iml(iml_path)
    header = load_las_header(las_path)
    tail_bytes = read_tail_bytes(las_path, header)

    selected = {name.lower() for name in selected_images}
    if selected:
        image_records = [
            record for record in image_records if record.image_name.lower() in selected
        ]
    if not image_records:
        raise ValueError("No image records matched the provided filters.")

    image_root = image_dir if image_dir is not None else iml_path.parent
    ground_z = (header.min_xyz[2] + header.max_xyz[2]) / 2.0
    from photo_selection import candidate_poses, resolve_image_paths

    if disable_overlap_filter:
        candidates = [
            compute_camera_pose(record, calibration, image_root, ground_z, projection_options)
            for record in image_records
        ]
    else:
        candidates = candidate_poses(
            image_records, calibration, header, image_root, projection_options
        )
        if not candidates:
            raise ValueError(
                "No IML photo overlaps the LAS. Check the coordinate system, flight and CAL."
            )
    paths = resolve_image_paths([pose.image_name for pose in candidates], image_root)
    existing_poses = [
        replace(pose, image_path=paths[pose.image_name])
        for pose in candidates
        if pose.image_name in paths
    ]
    missing_images = [pose.image_name for pose in candidates if pose.image_name not in paths]

    if not existing_poses:
        raise ValueError(
            "None of the image files from the IML list were found on disk. "
            f"IML: {iml_path.name}. "
            f"Images folder: {image_root}. "
            "This usually means the selected IML file and the images folder belong to different dates or flight blocks."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(tempfile.mkdtemp(prefix="rgbpoint_", dir=str(output_path.parent.resolve())))
    scores: np.memmap | None = None
    colors: np.memmap | None = None
    try:
        scores = np.memmap(
            tmp_root / "scores.bin",
            mode="w+",
            dtype=np.float32,
            shape=(header.point_count,),
        )
        colors = np.memmap(
            tmp_root / "colors.bin",
            mode="w+",
            dtype=np.uint8,
            shape=(header.point_count, 3),
        )
        scores[:] = -np.inf
        colors[:] = default_color
        scores.flush()
        colors.flush()

        print(f"LAS: {las_path}")
        print(f"Points: {header.point_count:,}")
        print(f"Input point format: {header.point_format}")
        print(f"Output point format: {header.output_point_format}")
        print(f"Images listed in IML: {len(image_records)}")
        print(f"Images found on disk: {len(existing_poses)}")
        if missing_images:
            preview = ", ".join(missing_images[:5])
            suffix = " ..." if len(missing_images) > 5 else ""
            print(f"Missing image files: {len(missing_images)} ({preview}{suffix})")
        print(f"Images selected for projection: {len(existing_poses)}")
        coordinates = LasCoordinates(las_path, header, chunk_size)

        for image_index, pose in enumerate(existing_poses, start=1):
            with Image.open(pose.image_path) as image:
                image_array = np.asarray(image.convert("RGB"))

            print(f"[{image_index}/{len(existing_poses)}] Projecting {pose.image_name}")
            bounds = projection_bounds(
                pose,
                calibration,
                header.min_xyz[2],
                header.max_xyz[2],
                (image_array.shape[1], image_array.shape[0]),
            )

            for start_index, xyz in coordinates:
                candidates = projection_candidate_indices(
                    xyz, bounds, header.min_xyz[2], header.max_xyz[2]
                )
                if candidates is not None:
                    if not len(candidates):
                        continue
                    xyz = xyz[candidates]
                _, inside_indices, sample_xy, radial_score = project_points(
                    xyz, pose, calibration, image_array.shape
                )
                if inside_indices.size == 0:
                    continue
                if candidates is not None:
                    inside_indices = candidates[inside_indices]
                absolute_indices = start_index + inside_indices
                current_scores = scores[absolute_indices]
                better = radial_score > current_scores
                if not np.any(better):
                    continue

                better_indices = absolute_indices[better]
                better_xy = sample_xy[better]
                sampled_colors = image_array[better_xy[:, 1], better_xy[:, 0]]

                scores[better_indices] = radial_score[better].astype(np.float32)
                colors[better_indices] = sampled_colors

            if on_image is not None:
                try:
                    on_image(image_index, len(existing_poses), pose.image_name, colors, scores)
                except Exception as error:
                    print(f"Live preview disabled: {error}")
                    on_image = None

        scores.flush()
        colors.flush()

        colored_points = int(np.count_nonzero(np.isfinite(scores)))
        print(f"Points colored from imagery: {colored_points:,}")
        write_output_las(
            las_path=las_path,
            output_path=output_path,
            header=header,
            chunk_size=chunk_size,
            scores=scores,
            colors=colors,
            tail_bytes=tail_bytes,
        )
        print(f"Output written to: {output_path}")
    finally:
        if scores is not None:
            scores.flush()
            scores._mmap.close()
        if colors is not None:
            colors.flush()
            colors._mmap.close()
        del scores
        del colors
        gc.collect()
        shutil.rmtree(tmp_root, ignore_errors=True)


def colorize_orthos(
    las_path: Path,
    ortho_dir: Path,
    output_path: Path,
    selected_images: list[str],
    chunk_size: int,
    default_color: np.ndarray,
    disable_overlap_filter: bool,
) -> None:
    if las_path.resolve() == output_path.resolve():
        raise ValueError("Output LAS must be different from the input LAS.")

    header = load_las_header(las_path)
    tail_bytes = read_tail_bytes(las_path, header)
    selected = {name.lower() for name in selected_images}
    tiles, missing_world = load_ortho_tiles(ortho_dir, selected)
    if not tiles:
        message = "No georeferenced orthophoto tiles were found."
        if missing_world:
            preview = ", ".join(missing_world[:5])
            suffix = " ..." if len(missing_world) > 5 else ""
            message += f" Missing world files for: {preview}{suffix}"
        raise ValueError(message)

    las_bbox_xy = (header.min_xyz[0], header.max_xyz[0], header.min_xyz[1], header.max_xyz[1])
    if disable_overlap_filter:
        tiles_to_use = tiles
    else:
        filtered = [
            tile for tile in tiles if bboxes_intersect(tile.footprint_bbox, las_bbox_xy, margin=1.0)
        ]
        tiles_to_use = filtered if filtered else tiles

    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(tempfile.mkdtemp(prefix="rgbpoint_", dir=str(output_path.parent.resolve())))
    scores: np.memmap | None = None
    colors: np.memmap | None = None
    try:
        scores = np.memmap(
            tmp_root / "scores.bin",
            mode="w+",
            dtype=np.float32,
            shape=(header.point_count,),
        )
        colors = np.memmap(
            tmp_root / "colors.bin",
            mode="w+",
            dtype=np.uint8,
            shape=(header.point_count, 3),
        )
        scores[:] = -np.inf
        colors[:] = default_color
        scores.flush()
        colors.flush()

        print(f"LAS: {las_path}")
        print(f"Points: {header.point_count:,}")
        print(f"Input point format: {header.point_format}")
        print(f"Output point format: {header.output_point_format}")
        print(f"Orthophoto tiles found: {len(tiles)}")
        if missing_world:
            preview = ", ".join(missing_world[:5])
            suffix = " ..." if len(missing_world) > 5 else ""
            print(f"Missing world files: {len(missing_world)} ({preview}{suffix})")
        print(f"Orthophoto tiles selected: {len(tiles_to_use)}")

        if not disable_overlap_filter and not any(
            bboxes_intersect(tile.footprint_bbox, las_bbox_xy, margin=1.0) for tile in tiles
        ):
            print(
                "Warning: no orthophoto footprint overlaps the LAS bounding box with the current inputs. "
                "Processing will continue, but the cloud and orthophotos may belong to different areas."
            )

        for tile_index, tile in enumerate(tiles_to_use, start=1):
            with Image.open(tile.image_path) as image:
                image_array = np.asarray(image.convert("RGB"))

            print(f"[{tile_index}/{len(tiles_to_use)}] Sampling {tile.image_name}")

            for start_index, count, chunk_bytes in iter_point_chunks(las_path, header, chunk_size):
                x_raw, y_raw, _ = points_from_chunk(chunk_bytes, header.point_record_length)
                xy = np.column_stack(
                    (
                        x_raw.astype(np.float64) * header.scales[0] + header.offsets[0],
                        y_raw.astype(np.float64) * header.scales[1] + header.offsets[1],
                    )
                )

                inside_indices, sample_xy, edge_score = project_points_to_ortho(xy, tile)
                if inside_indices.size == 0:
                    continue

                absolute_indices = start_index + inside_indices
                current_scores = scores[absolute_indices]
                better = edge_score > current_scores
                if not np.any(better):
                    continue

                better_indices = absolute_indices[better]
                better_xy = sample_xy[better]
                sampled_colors = image_array[better_xy[:, 1], better_xy[:, 0]]

                scores[better_indices] = edge_score[better].astype(np.float32)
                colors[better_indices] = sampled_colors

        scores.flush()
        colors.flush()

        colored_points = int(np.count_nonzero(np.isfinite(scores)))
        print(f"Points colored from orthophotos: {colored_points:,}")
        write_output_las(
            las_path=las_path,
            output_path=output_path,
            header=header,
            chunk_size=chunk_size,
            scores=scores,
            colors=colors,
            tail_bytes=tail_bytes,
        )
        print(f"Output written to: {output_path}")
    finally:
        if scores is not None:
            scores.flush()
            scores._mmap.close()
        if colors is not None:
            colors.flush()
            colors._mmap.close()
        del scores
        del colors
        gc.collect()
        shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> None:
    args = parse_args()
    default_color = parse_default_color(args.default_color)
    projection_options = PhotoProjectionOptions(
        projection_model=args.projection_model,
        camera_top=args.camera_top,
        rotation_order=args.rotation_order,
        attitude_corrections_sign=1 if args.attitude_corrections_sign == "plus" else -1,
        roll_sign=1 if args.roll_sign == "plus" else -1,
        pitch_sign=1 if args.pitch_sign == "plus" else -1,
    )
    if args.ortho_dir is not None:
        colorize_orthos(
            las_path=args.las.resolve(),
            ortho_dir=args.ortho_dir.resolve(),
            output_path=args.output.resolve(),
            selected_images=args.images,
            chunk_size=args.chunk_size,
            default_color=default_color,
            disable_overlap_filter=args.disable_overlap_filter,
        )
        return

    if args.cal is None or args.iml is None:
        raise ValueError(
            "Provide either --ortho-dir for orthophotos or both --cal and --iml for photo projection."
        )

    colorize(
        las_path=args.las.resolve(),
        cal_path=args.cal.resolve(),
        iml_path=args.iml.resolve(),
        output_path=args.output.resolve(),
        image_dir=args.images_dir.resolve() if args.images_dir is not None else None,
        selected_images=args.images,
        chunk_size=args.chunk_size,
        default_color=default_color,
        disable_overlap_filter=args.disable_overlap_filter,
        projection_options=projection_options,
    )


if __name__ == "__main__":
    main()
