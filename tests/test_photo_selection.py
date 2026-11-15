import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from colorize_las import colorize, load_iml, load_las_header
from photo_selection import (
    DEFAULT_OPTIONS,
    analyze_folder,
    export_plan,
    export_recommended_list,
    image_availability,
    plan_photos,
    resolve_image_paths,
)


def fixture(root, near=True):
    xyz = [(x, y, 0) for x in (-10, 0, 10) for y in (-10, 0, 10)]
    header = bytearray(227)
    header[:4] = b"LASF"
    header[24:26] = bytes([1, 2])
    struct.pack_into("<H", header, 94, 227)
    struct.pack_into("<I", header, 96, 227)
    struct.pack_into("<H", header, 105, 20)
    struct.pack_into("<I", header, 107, len(xyz))
    struct.pack_into("<ddd", header, 131, 0.01, 0.01, 0.01)
    struct.pack_into("<6d", header, 179, 10, -10, 10, -10, 0, 0)
    las = root / "cloud.las"
    with las.open("wb") as stream:
        stream.write(header)
        for x, y, z in xyz:
            record = bytearray(20)
            struct.pack_into("<iii", record, 0, x * 100, y * 100, z * 100)
            stream.write(record)
    cal = root / "camera.cal"
    cal.write_text("[TerraPhoto calibration]\nPrincipalPoint(XoYoZo)=0 0 -100\nImageSize=100 100\n")
    iml = root / "flight.iml"
    iml.write_text(
        "[TerraPhoto image list v5]\n"
        + ("Image=near.jpg\nTime=123\nXyz=0 0 100\nHrp=0 0 0\nCamera=0\n" if near else "")
        + "Image=far.jpg\nTime=456\nXyz=10000 10000 100\nHrp=0 0 0\nCamera=0\n"
    )
    return las, cal, iml


class PhotoSelectionTests(unittest.TestCase):
    def test_metadata_only_selection_and_original_iml_fields(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, cal, iml = fixture(root)
            self.assertEqual(len(load_iml(iml)), 2)  # No blank line between records.
            with patch("PIL.Image.open", side_effect=AssertionError("No photos should be opened")):
                plan = plan_photos(las, cal, iml)
            self.assertEqual([p["image"] for p in plan["photos"]], ["near.jpg"])
            self.assertEqual(plan["sample_coverage_percent"], 100)
            out = export_plan(plan, root / "plan")
            self.assertEqual(
                (out / "photos_needed.txt").read_text(encoding="utf-8-sig").splitlines(),
                ["near.jpg"],
            )
            self.assertEqual(len(load_iml(out / "selected.iml")), 1)
            self.assertIn("Time=123", (out / "selected.iml").read_text())
            self.assertIn("Camera=0", (out / "selected.iml").read_text())
            report = analyze_folder([las], root)
            self.assertEqual(len(report["plans"]), 1)  # Do not re-analyze generated IML.
            export_recommended_list(report, root / "recommended")

    def test_no_overlap_does_not_process_all_photos(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, cal, iml = fixture(root, near=False)
            self.assertEqual(plan_photos(las, cal, iml)["candidate_count"], 0)
            with self.assertRaisesRegex(ValueError, "overlaps the LAS"):
                colorize(
                    las,
                    cal,
                    iml,
                    root / "output.las",
                    root,
                    [],
                    5,
                    np.array([0, 0, 0], dtype=np.uint8),
                    False,
                    DEFAULT_OPTIONS[0],
                )
            self.assertFalse((root / "output.las").exists())

    def test_actual_colorization_uses_downloaded_subset(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, cal, iml = fixture(root)
            photos = root / "download" / "nested"
            photos.mkdir(parents=True)
            Image.new("RGB", (100, 100), (255, 0, 0)).save(
                photos / "near.jpg", quality=100, subsampling=0
            )
            target = root / "rgb.las"
            colorize(
                las,
                cal,
                iml,
                target,
                root / "download",
                [],
                5,
                np.array([0, 0, 0], dtype=np.uint8),
                False,
                DEFAULT_OPTIONS[0],
            )
            header = load_las_header(target)
            self.assertEqual(header.point_count, 9)
            self.assertEqual(header.point_format, 2)
            with target.open("rb") as stream:
                stream.seek(header.offset_to_point_data + 20)
                red, green, blue = struct.unpack("<HHH", stream.read(6))
            self.assertGreater(red, 65000)
            self.assertLess(green, 1000)
            self.assertLess(blue, 1000)

    def test_duplicate_names_from_different_flights_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in ("flight1", "flight2"):
                (root / name).mkdir()
                (root / name / "same.jpg").touch()
            with self.assertRaisesRegex(ValueError, "Multiple photos"):
                resolve_image_paths(["same.jpg"], root)

    def test_photo_check_ignores_nonoverlapping_iml_records_without_decoding_images(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, cal, iml = fixture(root)
            photos = root / "photos" / "nested"
            photos.mkdir(parents=True)
            (photos / "near.jpg").touch()
            with patch("PIL.Image.open", side_effect=AssertionError("Do not decode photos")):
                check = image_availability([las], cal, iml, root / "photos")
            self.assertEqual(check, {"required": 1, "found": 1, "missing": []})


if __name__ == "__main__":
    unittest.main()
