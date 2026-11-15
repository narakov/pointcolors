"""Small, replaceable preview snapshots; never modifies processing arrays."""

import json
import os
import time
from pathlib import Path

import numpy as np

from colorize_las import RGB_OFFSET, load_las_header


def sample_cloud(path, max_points=40000):
    header = load_las_header(path)
    if not header.point_count:
        raise ValueError("The cloud has no points to preview.")
    indices = np.linspace(
        0, header.point_count - 1, min(max_points, header.point_count), dtype=np.int64
    )
    records = np.memmap(
        path,
        mode="r",
        dtype=np.uint8,
        offset=header.offset_to_point_data,
        shape=(header.point_count, header.point_record_length),
    )
    try:
        sampled = records[indices].copy()
    finally:
        records._mmap.close()
    raw = sampled[:, :12].copy().view("<i4").reshape(-1, 3)
    xyz = raw.astype(np.float64) * header.scales + header.offsets
    rgb = None
    if header.has_rgb:
        offset = RGB_OFFSET[header.point_format]
        rgb = sampled[:, offset : offset + 6].copy().view("<u2").reshape(-1, 3) / 257
        rgb = np.rint(rgb).clip(0, 255).astype(np.uint8)
    return header, indices, xyz, rgb


class PreviewPublisher:
    def __init__(self, folder, interval=0.8, max_points=40000):
        self.folder = Path(folder)
        self.interval = interval
        self.max_points = max_points
        self.last_write = 0.0
        self.sequence = 0
        self.failed = False

    def begin(self, las_path, profile, default_color):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            header, self.indices, self.xyz, rgb = sample_cloud(las_path, self.max_points)
            self.rgb = np.tile(default_color, (len(self.indices), 1)).astype(np.uint8)
            self.input_rgb = rgb
            self.covered = np.zeros(len(self.indices), dtype=bool)
            self.primary = None
            self.fallback = False
            self.meta = {
                "cloud": Path(las_path).name,
                "point_count": header.point_count,
                "sample_count": len(self.indices),
                "profile": profile,
                "stage": "Preparing",
                "image_index": 0,
                "image_count": 0,
                "image_name": "",
                "finished": False,
            }
            self._write()
        except (OSError, ValueError) as error:
            self._disable(error)

    def set_pass(self, title, fallback=False):
        if self.failed:
            return
        self.fallback = fallback
        if fallback:
            self.primary = self.rgb.copy()
        self.meta.update(stage=title, image_index=0, image_count=0, image_name="")
        self._write()

    def update(self, image_index, image_count, image_name, colors, scores):
        if self.failed:
            return
        now = time.monotonic()
        if now - self.last_write < self.interval and image_index != image_count:
            return
        rgb = np.asarray(colors[self.indices]).copy()
        covered = np.isfinite(scores[self.indices])
        if self.input_rgb is not None:
            rgb[~covered] = self.input_rgb[~covered]
        if self.fallback:
            use_fallback = np.all(self.primary == 0, axis=1)
            rgb[~use_fallback] = self.primary[~use_fallback]
            covered = self.covered | covered
        self.rgb, self.covered = rgb, covered
        self.meta.update(
            image_index=image_index,
            image_count=image_count,
            image_name=image_name,
        )
        self._write()

    def finish(self, output_path):
        if self.failed:
            return
        try:
            _, _, self.xyz, self.rgb = sample_cloud(output_path, self.max_points)
            self.meta.update(stage="Finished", finished=True, output=str(output_path))
            self._write()
        except (OSError, ValueError) as error:
            self._disable(error)

    def _disable(self, error):
        self.failed = True
        print(f"Live preview disabled: {error}")

    def _write(self):
        self.sequence += 1
        self.meta["sequence"] = self.sequence
        self.meta["covered_sample"] = int(np.count_nonzero(self.covered))
        staging = self.folder / "frame.writing"
        try:
            with staging.open("wb") as stream:
                np.savez(
                    stream,
                    xyz=self.xyz,
                    rgb=self.rgb,
                    covered=self.covered,
                    metadata=np.array(json.dumps(self.meta)),
                )
            os.replace(staging, self.folder / "frame.npz")
            self.last_write = time.monotonic()
        except PermissionError:
            # A Windows viewer may briefly hold the previous frame open.
            pass
        except OSError as error:
            self._disable(error)
