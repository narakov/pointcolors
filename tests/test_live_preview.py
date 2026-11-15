import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from test_photo_selection import fixture

from colorize_las import colorize
from experimental_preview.stream import PreviewPublisher, sample_cloud
from experimental_preview.viewer import render_cloud
from photo_selection import DEFAULT_OPTIONS
from run_colorize_job import run_profile


class LivePreviewTests(unittest.TestCase):
    def test_preview_window_can_close_and_reopen(self):
        import tkinter as tk

        import rgbpoint_gui  # Initializes the bundled Tcl runtime.
        from experimental_preview.viewer import PreviewSession

        top_class = tk.Toplevel

        def hidden_top(*args, **kwargs):
            top = top_class(*args, **kwargs)
            top.withdraw()
            return top

        root = rgbpoint_gui.tk.Tk()
        root.withdraw()
        session = None
        try:
            with patch.object(tk, "Toplevel", side_effect=hidden_top):
                session = PreviewSession(root)
                folder = session.folder
                session.viewer.close()
                self.assertTrue(folder.is_dir())
                session.show()
                self.assertFalse(session.viewer.closed)
            session.close()
            self.assertFalse(folder.exists())
        finally:
            if session is not None and session.folder.exists():
                session.close()
            root.destroy()

    def test_enhanced_preview_preserves_output_and_final_frame_matches_las(self):
        with tempfile.TemporaryDirectory() as scratch, contextlib.redirect_stdout(io.StringIO()):
            root = Path(scratch)
            las, cal, iml = fixture(root)
            # Enhanced blending uses GPS-time LAS records (formats 1 -> 3).
            data = las.read_bytes()
            header = bytearray(data[:227])
            header[104] = 1
            struct.pack_into("<H", header, 105, 28)
            las.write_bytes(
                header
                + b"".join(
                    data[start : start + 20] + bytes(8) for start in range(227, len(data), 20)
                )
            )
            Image.new("RGB", (100, 100), (180, 70, 35)).save(root / "near.jpg")
            ordinary, previewed = root / "ordinary.las", root / "previewed.las"
            publisher = PreviewPublisher(root / "stream", interval=0, max_points=5)
            options = ("enhanced", 4, np.zeros(3, dtype=np.uint8))
            run_profile(las, cal, iml, root, ordinary, *options)
            run_profile(las, cal, iml, root, previewed, *options, preview=publisher)
            self.assertEqual(ordinary.read_bytes(), previewed.read_bytes())
            _, _, xyz, rgb = sample_cloud(previewed, 5)
            with np.load(root / "stream/frame.npz", allow_pickle=False) as frame:
                np.testing.assert_array_equal(frame["xyz"], xyz)
                np.testing.assert_array_equal(frame["rgb"], rgb)
                meta = json.loads(str(frame["metadata"]))
            self.assertTrue(meta["finished"])
            self.assertEqual(meta["sample_count"], 5)
            self.assertEqual(meta["point_count"], 9)

    def test_fallback_keeps_primary_colors_while_filling_black_points(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            las, _, _ = fixture(root)
            publisher = PreviewPublisher(root / "stream", interval=0)
            publisher.begin(las, "Enhanced", np.zeros(3, dtype=np.uint8))
            publisher.set_pass("Primary")
            rgb = np.zeros((9, 3), dtype=np.uint8)
            scores = np.full(9, -np.inf)
            rgb[0], scores[0] = [255, 0, 0], 1
            publisher.update(1, 1, "first.jpg", rgb, scores)
            publisher.set_pass("Fallback", fallback=True)
            fallback = np.full((9, 3), [0, 255, 0], dtype=np.uint8)
            publisher.update(1, 1, "second.jpg", fallback, np.ones(9))
            np.testing.assert_array_equal(publisher.rgb[0], [255, 0, 0])
            np.testing.assert_array_equal(publisher.rgb[1:], fallback[1:])

    def test_preview_failure_does_not_abort_colorization(self):
        with tempfile.TemporaryDirectory() as scratch, contextlib.redirect_stdout(io.StringIO()):
            root = Path(scratch)
            las, cal, iml = fixture(root)
            Image.new("RGB", (100, 100), (255, 100, 0)).save(root / "near.jpg")
            output = root / "colored.las"
            with patch(
                "experimental_preview.stream.os.replace", side_effect=OSError("disk unavailable")
            ):
                publisher = PreviewPublisher(root / "stream")
                run_profile(
                    las,
                    cal,
                    iml,
                    root,
                    output,
                    "standard",
                    4,
                    np.zeros(3, dtype=np.uint8),
                    preview=publisher,
                )
            self.assertTrue(output.is_file())
            self.assertTrue(publisher.failed)

    def test_callback_exception_does_not_abort_colorization(self):
        with tempfile.TemporaryDirectory() as scratch, contextlib.redirect_stdout(io.StringIO()):
            root = Path(scratch)
            las, cal, iml = fixture(root)
            Image.new("RGB", (100, 100), (255, 100, 0)).save(root / "near.jpg")
            output = root / "colored.las"

            def broken(*_args):
                raise RuntimeError("preview closed")

            colorize(
                las,
                cal,
                iml,
                output,
                root,
                [],
                4,
                np.zeros(3, dtype=np.uint8),
                False,
                DEFAULT_OPTIONS[0],
                on_image=broken,
            )
            self.assertTrue(output.is_file())

    def test_renderer_orbits_and_preserves_valid_black_colors(self):
        xyz = np.array([[-1, -1, 0], [1, 0, 1], [0, 1, 0]], dtype=float)
        rgb = np.array([[0, 0, 0], [255, 50, 10], [50, 200, 80]], dtype=np.uint8)
        covered = np.ones(3, dtype=bool)
        first = np.array(render_cloud(xyz, rgb, covered, (320, 240)))
        rotated = np.array(render_cloud(xyz, rgb, covered, (320, 240), yaw=1.2))
        self.assertFalse(np.array_equal(first, rotated))
        self.assertTrue(np.any(np.all(first == 0, axis=2)))


if __name__ == "__main__":
    unittest.main()
