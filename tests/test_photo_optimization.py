import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from test_photo_selection import fixture

from colorize_las import project_points
from photo_optimization import (
    CoverageModel,
    add_patterns,
    intersect_polygons,
    optimize_plan,
    polygon_area,
    solve_cover,
)
from photo_selection import plan_photos


class OptimizationTests(unittest.TestCase):
    def test_binary_cover_proves_known_minimum(self):
        # One of the first two photos plus the last photo is required.
        raw = np.array([[1, 1, 0], [1, 1, 0], [0, 0, 1]], dtype=bool)
        patterns = set()
        add_patterns(patterns, raw, raw)
        selected, proof = solve_cover(patterns, 3)
        self.assertEqual(len(selected), 2)
        self.assertTrue(proof["optimal"])
        self.assertEqual(proof["lower_bound"], 2)
        self.assertTrue(raw[:, selected].any(axis=1).all())

    def test_pixel_masks_match_actual_colorizer(self):
        with tempfile.TemporaryDirectory() as scratch:
            paths = fixture(Path(scratch))
            plan = plan_photos(*paths)
            model = CoverageModel(plan)
            points = np.random.default_rng(3).uniform([-80, -80, -5], [80, 80, 5], (1000, 3))
            raw, _ = model.masks(points, 0)
            expected = np.zeros(1000, dtype=bool)
            _, cal, entries = model.scenarios[0]
            for pose, _ in entries[0]:
                _, ids, _, _ = project_points(points, pose, cal, (100, 100, 3))
                expected[ids] = True
            np.testing.assert_array_equal(raw[:, 0], expected)

    def test_full_las_verified_without_loading_photos(self):
        with tempfile.TemporaryDirectory() as scratch:
            paths = fixture(Path(scratch))
            plan = plan_photos(*paths)
            with patch("PIL.Image.open", side_effect=AssertionError("No photo access")):
                result = optimize_plan(plan, 10)
            self.assertEqual(result["candidate_count"], 1)
            self.assertTrue(result["optimization"]["full_las_verified"])
            self.assertTrue(result["optimization"]["minimum_proven_in_camera_model"])
            self.assertEqual(result["optimization"]["scenarios"][0]["selected_covered"], 9)

    def test_edge_allowance_relaxes_only_when_no_inset_candidate_exists(self):
        raw = np.array([[1, 0], [1, 1]], dtype=bool)
        inner = np.array([[0, 0], [0, 1]], dtype=bool)
        patterns = set()
        add_patterns(patterns, raw, inner)
        selected, _ = solve_cover(patterns, 2)
        self.assertEqual(set(selected), {0, 1})

    def test_intersection_percentage_for_known_rectangles(self):
        a = np.array([[0, 0], [10, 0], [10, 10], [0, 10]])
        b = a + [9, 0]
        self.assertAlmostEqual(polygon_area(intersect_polygons(a, b)) / polygon_area(a) * 100, 10)


if __name__ == "__main__":
    unittest.main()
