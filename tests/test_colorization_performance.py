import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from test_photo_selection import fixture

import colorize_las as engine
from photo_selection import DEFAULT_OPTIONS, plan_photos


class ColorizationPerformanceTests(unittest.TestCase):
    def test_coordinate_cache_matches_streaming_and_respects_budget(self):
        with tempfile.TemporaryDirectory() as scratch:
            las, _, _ = fixture(Path(scratch))
            header = engine.load_las_header(las)
            for budget, reads in ((0, 2), (1024, 1)):
                coordinates = engine.LasCoordinates(las, header, 4, cache_bytes=budget)
                with patch.object(
                    engine, "iter_point_chunks", wraps=engine.iter_point_chunks
                ) as reader:
                    first, second = list(coordinates), list(coordinates)
                self.assertEqual(reader.call_count, reads)
                self.assertEqual([start for start, _ in first], [0, 4, 8])
                np.testing.assert_array_equal(
                    np.concatenate([xyz for _, xyz in first]),
                    [(x, y, 0) for x in (-10, 0, 10) for y in (-10, 0, 10)],
                )
                for (_, a), (_, b) in zip(first, second):
                    np.testing.assert_array_equal(a, b)
                    self.assertFalse(a.flags.writeable)

    def test_bounds_preserve_visible_points_with_tilt_and_outside_header_heights(self):
        with tempfile.TemporaryDirectory() as scratch:
            las, cal_path, iml = fixture(Path(scratch))
            calibration = engine.load_calibration(cal_path)
            record = engine.load_iml(iml)[0]
            points = np.random.default_rng(7).uniform([-200, -200, -20], [200, 200, 20], (10000, 3))
            for hrp in ((0, 0, 0), (35, 15, -10), (120, -25, 20)):
                pose = engine.compute_camera_pose(
                    replace(record, hrp=hrp), calibration, Path(scratch), 0, DEFAULT_OPTIONS[0]
                )
                bounds = engine.projection_bounds(pose, calibration, -5, 5, (100, 100))
                self.assertIsNotNone(bounds)
                candidates = engine.projection_candidate_indices(points, bounds, -5, 5)
                _, expected, xy, score = engine.project_points(
                    points, pose, calibration, (100, 100, 3)
                )
                _, actual, subset_xy, subset_score = engine.project_points(
                    points[candidates], pose, calibration, (100, 100, 3)
                )
                np.testing.assert_array_equal(candidates[actual], expected)
                np.testing.assert_array_equal(subset_xy, xy)
                np.testing.assert_allclose(subset_score, score, rtol=1e-14)
                self.assertIsNone(engine.projection_bounds(pose, calibration, -5, 5, (200, 200)))
                distorted = replace(calibration, lens_k=(0.001, 0, 0))
                self.assertIsNone(engine.projection_bounds(pose, distorted, -5, 5))

    def test_all_selected_photos_remain_available_and_output_matches_unculled_projection(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, cal, iml = fixture(root)
            with iml.open("a") as handle:
                handle.write("Image=side.jpg\nXyz=25 0 100\nHrp=0 0 0\nCamera=0\n")
            gradient = np.zeros((100, 100, 3), dtype=np.uint8)
            gradient[:, :, 0] = np.arange(100)
            gradient[:, :, 1] = np.arange(100)[:, None]
            for name, offset in (("near.jpg", 0), ("side.jpg", 120)):
                Image.fromarray(gradient + offset).save(root / name)
            plan = plan_photos(las, cal, iml)
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual({row["image"] for row in plan["photos"]}, {"near.jpg", "side.jpg"})
            output, reference = root / "optimized.las", root / "unculled.las"
            args = (las, cal, iml)
            options = (root, [], 4, np.zeros(3, dtype=np.uint8), False, DEFAULT_OPTIONS[0])
            engine.colorize(*args, output, *options)
            with patch.object(engine, "projection_bounds", return_value=None):
                engine.colorize(*args, reference, *options)
            self.assertEqual(output.read_bytes(), reference.read_bytes())


if __name__ == "__main__":
    unittest.main()
