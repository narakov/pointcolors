import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_photo_selection import fixture

import rgbpoint_gui as gui


def dispose_root(root):
    root.update_idletasks()
    for event in root.tk.call("after", "info"):
        root.after_cancel(event)
    root.destroy()


class GuiSelectionTests(unittest.TestCase):
    def test_selection_uses_shared_files_and_original_flight(self):
        from photo_selection import analyze_folder, export_plan, plan_photos

        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            las, cal, original_iml = fixture(folder)
            exported = export_plan(plan_photos(las, cal, original_iml), folder / "subset")
            with (
                patch.object(gui, "STATE_FILE", folder / "state.json"),
                patch.object(gui, "LAUNCHER_LOG_FILE", folder / "launcher.log"),
                patch.object(gui, "RUN_LOG_FILE", folder / "run.log"),
            ):
                root = tk.Tk()
                root.withdraw()
                try:
                    app = gui.RGBPointGuiApp(root)
                    app.las_var.set(str(las))
                    app.cal_var.set(str(cal))
                    app.iml_var.set(str(exported / "selected.iml"))
                    # Explicit CAL/IML work even when the optional search folder is absent.
                    app.data_root_var.set(str(folder / "no-search-folder"))
                    with (
                        patch("photo_selection.analyze_folder", wraps=analyze_folder) as analyze,
                        patch.object(app, "_show_photo_plans") as show,
                        patch.object(gui.messagebox, "showerror") as error,
                    ):
                        app.start_photo_selection()
                        deadline = time.monotonic() + 10
                        while app.analysis_running and time.monotonic() < deadline:
                            root.update()
                            time.sleep(0.01)
                        error.assert_not_called()
                        analyze.assert_called_once()
                        self.assertEqual(analyze.call_args.kwargs["cal_path"], cal)
                        self.assertEqual(analyze.call_args.kwargs["iml_path"], original_iml)
                        show.assert_called_once()
                        self.assertEqual(show.call_args.args[0]["plans"][0]["listed_images"], 2)
                finally:
                    dispose_root(root)

    def test_optimized_variant_is_exported_and_applied(self):
        from tkinter import ttk

        from colorize_las import load_iml
        from photo_selection import plan_photos

        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            las, cal, iml = fixture(folder)
            with iml.open("a") as handle:
                handle.write("Image=second_near.jpg\nXyz=1 0 100\nHrp=0 0 0\n")
            plan = plan_photos(las, cal, iml)
            self.assertEqual(plan["candidate_count"], 2)
            downloaded = folder / "downloaded"
            (downloaded / "nested").mkdir(parents=True)
            for name in ("near.jpg", "second_near.jpg"):
                Image.new("RGB", (100, 100), (200, 100, 50)).save(downloaded / "nested" / name)
            top_class = tk.Toplevel

            def hidden_top(*args, **kwargs):
                top = top_class(*args, **kwargs)
                top.withdraw()
                return top

            with (
                patch.object(gui, "PROJECT_ROOT", folder),
                patch.object(gui, "STATE_FILE", folder / "state.json"),
                patch.object(gui, "LAUNCHER_LOG_FILE", folder / "launcher.log"),
                patch.object(gui, "RUN_LOG_FILE", folder / "run.log"),
                patch.object(gui.messagebox, "showinfo"),
                patch.object(gui.messagebox, "showerror") as error,
            ):
                root = tk.Tk()
                root.withdraw()
                try:
                    app = gui.RGBPointGuiApp(root)
                    app.images_var.set(str(downloaded))
                    with patch.object(gui.tk, "Toplevel", side_effect=hidden_top):
                        app._show_photo_plans({"plans": [plan], "errors": []})
                    dialog = next(w for w in root.winfo_children() if isinstance(w, top_class))

                    def descend(widget):
                        result = []
                        for child in widget.winfo_children():
                            result.append(child)
                            result.extend(descend(child))
                        return result

                    widgets = descend(dialog)
                    combo = next(w for w in widgets if isinstance(w, ttk.Combobox))
                    combo.set("Minimum Coverage Set")
                    combo.event_generate("<<ComboboxSelected>>")
                    root.update()
                    calculate = next(
                        w
                        for w in widgets
                        if isinstance(w, ttk.Button) and w.cget("text") == "Calculate Reduced Set"
                    )
                    calculate.invoke()
                    deadline = time.monotonic() + 10
                    while app.analysis_running and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                    self.assertFalse(app.analysis_running)
                    apply = next(
                        w
                        for w in widgets
                        if isinstance(w, ttk.Button)
                        and w.cget("text") == "Save List and Use This Set"
                    )
                    self.assertNotIn("disabled", apply.state())
                    apply.invoke()
                    error.assert_not_called()
                    self.assertEqual(len(load_iml(Path(app.iml_var.get()))), 1)
                    self.assertEqual(Path(app.images_var.get()), downloaded)
                    self.assertIn("Found 1 of 1", app.selection_status_var.get())
                    self.assertIsNotNone(app._validate_inputs())
                    with patch.object(gui.tk, "Toplevel", side_effect=hidden_top):
                        app._show_photo_plans({"plans": [plan], "errors": []})
                    full_dialog = next(w for w in root.winfo_children() if isinstance(w, top_class))
                    full_widgets = descend(full_dialog)
                    full_mode = next(w for w in full_widgets if isinstance(w, ttk.Combobox))
                    self.assertEqual(full_mode.get(), "Full Candidate Set")
                    full_apply = next(
                        w
                        for w in full_widgets
                        if isinstance(w, ttk.Button)
                        and w.cget("text") == "Save List and Use This Set"
                    )
                    full_apply.invoke()
                    error.assert_not_called()
                    self.assertEqual(len(load_iml(Path(app.iml_var.get()))), 2)
                    self.assertEqual(Path(app.images_var.get()), downloaded)
                    self.assertIsNotNone(app._validate_inputs())
                finally:
                    dispose_root(root)

    def test_background_metadata_selection_without_photos(self):
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            las, _, _ = fixture(folder)
            with (
                patch.object(gui, "STATE_FILE", folder / "state.json"),
                patch.object(gui, "LAUNCHER_LOG_FILE", folder / "launcher.log"),
                patch.object(gui, "RUN_LOG_FILE", folder / "run.log"),
            ):
                root = tk.Tk()
                root.withdraw()  # No window is displayed during this check.
                try:
                    app = gui.RGBPointGuiApp(root)
                    app.las_var.set(str(las))
                    app.data_root_var.set(str(folder))
                    with (
                        patch.object(app, "_show_photo_plans") as show,
                        patch.object(gui.messagebox, "showerror") as error,
                    ):
                        app.start_photo_selection()
                        self.assertTrue(app.analysis_running)
                        self.assertIn("disabled", app.selection_button.state())
                        deadline = time.monotonic() + 10
                        while app.analysis_running and time.monotonic() < deadline:
                            root.update()
                            time.sleep(0.01)
                        self.assertFalse(app.analysis_running)
                        error.assert_not_called()
                        show.assert_called_once()
                        self.assertEqual(show.call_args.args[0]["plans"][0]["candidate_count"], 1)
                        self.assertNotIn("disabled", app.run_button.state())
                finally:
                    dispose_root(root)

    def test_missing_photos_are_reported_before_starting_backend(self):
        from tkinter import ttk

        from photo_selection import plan_photos

        for existing_photos in (0, 1):
            with (
                self.subTest(existing_photos=existing_photos),
                tempfile.TemporaryDirectory() as scratch,
            ):
                folder = Path(scratch)
                las, cal, iml = fixture(folder)
                with iml.open("a") as handle:
                    handle.write("Image=second_near.jpg\nXyz=1 0 100\nHrp=0 0 0\n")
                plan = plan_photos(las, cal, iml)
                top_class = tk.Toplevel

                def hidden_top(*args, **kwargs):
                    top = top_class(*args, **kwargs)
                    top.withdraw()
                    return top

                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)

                with (
                    patch.object(gui, "PROJECT_ROOT", folder),
                    patch.object(gui, "STATE_FILE", folder / "state.json"),
                    patch.object(gui, "LAUNCHER_LOG_FILE", folder / "launcher.log"),
                    patch.object(gui, "RUN_LOG_FILE", folder / "run.log"),
                    patch.object(gui.messagebox, "showinfo"),
                    patch.object(gui.messagebox, "showerror") as error,
                    patch.object(gui.subprocess, "Popen") as launch,
                ):
                    root = tk.Tk()
                    root.withdraw()
                    try:
                        app = gui.RGBPointGuiApp(root)
                        if existing_photos:
                            photos = folder / "downloaded"
                            photos.mkdir()
                            Image.new("RGB", (100, 100), (200, 100, 50)).save(photos / "near.jpg")
                            app.images_var.set(str(photos))
                        with patch.object(gui.tk, "Toplevel", side_effect=hidden_top):
                            app._show_photo_plans({"plans": [plan], "errors": []})
                        apply = next(
                            widget
                            for widget in descendants(root)
                            if isinstance(widget, ttk.Button)
                            and widget.cget("text") == "Save List and Use This Set"
                        )
                        apply.invoke()
                        error.assert_not_called()
                        self.assertTrue(Path(app.images_var.get()).is_dir())
                        app.start_processing()
                        launch.assert_not_called()
                        error.assert_called_once()
                        self.assertIn(f"Found {existing_photos} of 2", error.call_args.args[1])
                        self.assertIn("does not download", error.call_args.args[1])
                    finally:
                        dispose_root(root)


if __name__ == "__main__":
    unittest.main()
