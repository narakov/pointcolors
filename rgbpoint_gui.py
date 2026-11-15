#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

APP_TITLE = "Point Colors — RGB Point Cloud Colorizer"
PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "rgbpoint_gui_state.json"
RUN_LOG_FILE = PROJECT_ROOT / "rgbpoint_gui_run.log"
LAUNCHER_LOG_FILE = PROJECT_ROOT / "rgbpoint_gui_launcher.log"
DEFAULT_CHUNK_SIZE = "1000000"
DEFAULT_COLOR = "0,0,0"
DEFAULT_BATCH_SUFFIX = "_gui_rgb"
DEFAULT_PROFILE = "enhanced"
ABOUT_ORGANIZATION = "Laboratory of Information Modeling Technologies and Machine Learning"
ABOUT_COPYRIGHT = "Copyright © Tikhonov Timofey, laboratory research assistant"


def append_file_line(path: Path, message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def bootstrap_tcl_runtime() -> None:
    py_root = Path(sys.executable).resolve().parent
    dll_dir = py_root / "DLLs"
    path_parts = [str(dll_dir), str(py_root), os.environ.get("PATH", "")]
    os.environ["PATH"] = ";".join(part for part in path_parts if part)

    bundle_tcl = PROJECT_ROOT / ".tcl_bundle" / "tcl8.6"
    bundle_tk = PROJECT_ROOT / ".tcl_bundle" / "tk8.6"
    if bundle_tcl.exists() and bundle_tk.exists():
        os.environ["TCL_LIBRARY"] = str(bundle_tcl)
        os.environ["TK_LIBRARY"] = str(bundle_tk)


bootstrap_tcl_runtime()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


def normalize_default_color(value: str) -> str:
    parts = re.findall(r"-?\d+", value or "")
    if len(parts) == 0:
        values = [0, 0, 0]
    elif len(parts) == 1:
        v = int(parts[0])
        values = [v, v, v]
    elif len(parts) == 2:
        values = [int(parts[0]), int(parts[1]), 0]
    elif len(parts) == 3:
        values = [int(parts[0]), int(parts[1]), int(parts[2])]
    else:
        raise ValueError("Default color must have one, two, or three numeric components.")

    if any(v < 0 or v > 255 for v in values):
        raise ValueError("Default color values must be between 0 and 255.")
    return ",".join(str(v) for v in values)


def parse_las_list(value: str) -> list[str]:
    tokens = re.split(r"[;\r\n]+", value or "")
    return [token.strip() for token in tokens if token.strip()]


class RGBPointGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1200x900")
        self.root.minsize(1020, 760)

        self.process: subprocess.Popen[str] | None = None
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.log_queue: queue.Queue[tuple[str, bool]] = queue.Queue()
        self.last_error = ""
        self.analysis_queue: queue.Queue = queue.Queue()
        self.analysis_running = False
        self._about_dialog: tk.Toplevel | None = None
        self.preview_session = None
        self.preview_enabled_var = tk.BooleanVar(value=False)
        self.data_root_var = tk.StringVar(value=str(PROJECT_ROOT))
        self.selection_status_var = tk.StringVar(
            value="Use CAL and IML above, or leave them empty to search the metadata folder. Photos are not required yet."
        )

        self.las_var = tk.StringVar()
        self.cal_var = tk.StringVar()
        self.iml_var = tk.StringVar()
        self.images_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.chunk_var = tk.StringVar(value=DEFAULT_CHUNK_SIZE)
        self.default_color_var = tk.StringVar(value=DEFAULT_COLOR)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._load_state()
        self.default_color_var.set(normalize_default_color(self.default_color_var.get()))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_process)
        append_file_line(LAUNCHER_LOG_FILE, "Python GUI opened")

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        menu_bar = tk.Menu(self.root, tearoff=False)
        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="About", underline=0, command=self.show_about)
        menu_bar.add_cascade(label="Help", underline=0, menu=help_menu)
        self.root.configure(menu=menu_bar)

        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        title = ttk.Label(
            container, text="RGB Point Cloud Colorizer", font=("Segoe UI", 20, "bold")
        )
        title.grid(row=0, column=0, sticky="w")

        form = ttk.Frame(container, padding=(0, 10, 0, 8))
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Point Cloud (.LAS, one or many)").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=4
        )
        ttk.Entry(form, textvariable=self.las_var).grid(row=0, column=1, sticky="ew", pady=4)
        las_buttons = ttk.Frame(form)
        las_buttons.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=4)
        ttk.Button(las_buttons, text="Browse One", command=self._pick_las_file).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(las_buttons, text="Browse Many", command=self._pick_las_files).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )

        self._add_path_row(form, 1, "Camera Calibration (.cal)", self.cal_var, self._pick_cal_file)
        self._add_path_row(form, 2, "Image List (.iml)", self.iml_var, self._pick_iml_file)

        discovery = ttk.LabelFrame(form, text="Photo Selection Before Download", padding=10)
        discovery.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 12))
        discovery.columnconfigure(1, weight=1)
        ttk.Label(discovery, text="Metadata Folder (.cal / .iml)").grid(row=0, column=0, sticky="w")
        ttk.Entry(discovery, textvariable=self.data_root_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(discovery, text="Browse", command=self._pick_data_root).grid(row=0, column=2)
        self.selection_button = ttk.Button(
            discovery, text="Select Photos", command=self.start_photo_selection
        )
        self.selection_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 4))
        ttk.Label(discovery, textvariable=self.selection_status_var, wraplength=1000).grid(
            row=2, column=0, columnspan=3, sticky="w"
        )

        self._add_path_row(form, 4, "Images Folder", self.images_var, self._pick_images_dir)
        self._add_path_row(
            form, 5, "Output LAS (single mode)", self.output_var, self._pick_output_file
        )
        self._add_path_row(
            form,
            6,
            "Output Folder (batch, optional)",
            self.output_dir_var,
            self._pick_output_dir,
        )

        options = ttk.LabelFrame(container, text="Processing Settings", padding=10)
        options.grid(row=2, column=0, sticky="nsew")
        options.columnconfigure(1, weight=1)
        options.rowconfigure(4, weight=1)

        ttk.Label(options, text="Chunk Size").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.chunk_var, width=12).grid(
            row=0, column=1, sticky="w", padx=(8, 18)
        )

        ttk.Label(options, text="Default Color").grid(row=0, column=2, sticky="w")
        ttk.Entry(options, textvariable=self.default_color_var, width=12).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )

        buttons = ttk.Frame(options)
        buttons.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(12, 8))
        buttons.columnconfigure(4, weight=1)

        self.run_button = ttk.Button(
            buttons, text="Start Colorization", command=self.start_processing
        )
        self.run_button.grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Clear Log", command=self.clear_log).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.preview_button = ttk.Button(
            buttons, text="Open Preview", command=self.show_preview, state="disabled"
        )
        self.preview_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(buttons, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=4, sticky="e"
        )

        self.preview_check = ttk.Checkbutton(
            options, text="Live 3D Preview (experimental)", variable=self.preview_enabled_var
        )
        self.preview_check.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(options, text="Run Log").grid(row=3, column=0, columnspan=4, sticky="w")
        self.log_text = ScrolledText(options, wrap="none", height=18, font=("Consolas", 10))
        self.log_text.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        self.log_text.configure(state="disabled")

    def show_about(self) -> None:
        if self._about_dialog is not None and self._about_dialog.winfo_exists():
            self._about_dialog.lift()
            self._about_dialog.focus_set()
            return

        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        self._about_dialog = dialog
        dialog.title("About Point Colors")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        background = ttk.Style(dialog).lookup("TFrame", "background") or "SystemButtonFace"
        dialog.configure(background=background)

        content = ttk.Frame(dialog, padding=(14, 16, 18, 12))
        content.grid(sticky="nsew")
        content.columnconfigure(1, weight=1)
        icon = tk.Canvas(content, width=42, height=42, highlightthickness=0)
        icon.configure(background=background)
        icon.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 12))
        icon.create_oval(4, 4, 36, 36, fill="#149BDD", outline="")
        icon.create_text(20, 20, text="i", fill="white", font=("Segoe UI", 19, "bold"))
        ttk.Label(
            content,
            text=f"Point Colors — {ABOUT_ORGANIZATION}",
            font=("Segoe UI", 10, "bold"),
            wraplength=435,
            justify="left",
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            content,
            text=ABOUT_COPYRIGHT,
            font=("Segoe UI", 9),
            foreground="#003399",
            wraplength=435,
            justify="left",
        ).grid(row=1, column=1, sticky="w", pady=(12, 16))

        def close(_event=None):
            self._about_dialog = None
            dialog.destroy()

        close_button = ttk.Button(content, text="Close", width=9, command=close)
        close_button.grid(row=2, column=1, sticky="e", pady=(6, 0))
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", close)
        dialog.bind("<Return>", close)
        dialog.update_idletasks()
        width, height = max(540, dialog.winfo_reqwidth()), dialog.winfo_reqheight()
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        if self.root.winfo_viewable():
            dialog.deiconify()
            dialog.grab_set()
            close_button.focus_set()

    def _add_path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_cb,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Browse", command=browse_cb).grid(
            row=row, column=2, sticky="w", padx=(8, 0), pady=4
        )

    def _pick_las_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose LAS point cloud",
            filetypes=[("LAS files", "*.las"), ("All files", "*.*")],
        )
        if path:
            self.las_var.set(path)
            if not self.output_var.get().strip():
                p = Path(path)
                self.output_var.set(str(p.with_name(f"{p.stem}_gui_rgb.LAS")))

    def _pick_data_root(self) -> None:
        path = filedialog.askdirectory(
            title="Choose camera metadata folder (CAL and IML, including subfolders)"
        )
        if path:
            self.data_root_var.set(path)

    def start_photo_selection(self) -> None:
        if self.analysis_running or (self.process and self.process.poll() is None):
            return
        paths = [Path(value) for value in parse_las_list(self.las_var.get())]
        if not paths or any(not path.is_file() for path in paths):
            messagebox.showerror(APP_TITLE, "Choose an existing LAS file in Point Cloud above.")
            return
        cal = Path(self.cal_var.get().strip()) if self.cal_var.get().strip() else None
        iml = Path(self.iml_var.get().strip()) if self.iml_var.get().strip() else None
        for kind, path in (("Camera Calibration", cal), ("Image List", iml)):
            if path is not None and not path.is_file():
                messagebox.showerror(APP_TITLE, f"{kind} file not found:\n{path}")
                return
        # Re-select from the original flight when the form contains an exported subset.
        if iml is not None and iml.name.lower() == "selected.iml":
            selection_file = iml.with_name("selection.json")
            try:
                original = Path(json.loads(selection_file.read_text(encoding="utf-8"))["iml"])
                if original.is_file():
                    iml = original
            except (OSError, ValueError, KeyError, TypeError):
                pass
        root = Path(self.data_root_var.get().strip() or PROJECT_ROOT)
        if not root.is_dir() and not (cal and iml):
            messagebox.showerror(
                APP_TITLE,
                "Choose a metadata folder containing CAL and IML files, or specify both files above.",
            )
            return
        if not root.is_dir():
            root = cal.parent
        self.analysis_running = True
        self.selection_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.selection_status_var.set(
            "Checking camera metadata. Photographs are not needed for this step…"
        )

        def work():
            try:
                from photo_selection import analyze_folder

                report = analyze_folder(
                    paths,
                    root,
                    cal_path=cal,
                    iml_path=iml,
                    on_progress=lambda text: self.analysis_queue.put(("progress", text)),
                )
                self.analysis_queue.put(("complete", report))
            except Exception as error:
                self.analysis_queue.put(("error", str(error)))

        threading.Thread(target=work, daemon=True).start()

    def _show_photo_plans(self, report) -> None:
        from photo_selection_dialog import show_photo_plans

        show_photo_plans(self, report, PROJECT_ROOT, APP_TITLE, DEFAULT_BATCH_SUFFIX)

    def _pick_las_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose one or more LAS point clouds",
            filetypes=[("LAS files", "*.las"), ("All files", "*.*")],
        )
        if not paths:
            return

        normalized = [str(Path(path)) for path in paths]
        self.las_var.set("; ".join(normalized))
        if len(normalized) == 1 and not self.output_var.get().strip():
            p = Path(normalized[0])
            self.output_var.set(str(p.with_name(f"{p.stem}_gui_rgb.LAS")))

    def _pick_cal_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose camera calibration",
            filetypes=[("CAL files", "*.cal"), ("All files", "*.*")],
        )
        if path:
            self.cal_var.set(path)

    def _pick_iml_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose image list",
            filetypes=[("IML files", "*.iml"), ("All files", "*.*")],
        )
        if path:
            self.iml_var.set(path)

    def _pick_images_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose images folder")
        if path:
            self.images_var.set(path)

    def _pick_output_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save output LAS as",
            defaultextension=".las",
            filetypes=[("LAS files", "*.las"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _pick_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder for batch mode")
        if path:
            self.output_dir_var.set(path)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        append_file_line(RUN_LOG_FILE, message)

    def clear_log(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _validate_inputs(self) -> dict[str, object] | None:
        las_paths = [Path(value) for value in parse_las_list(self.las_var.get())]
        if not las_paths:
            messagebox.showerror(APP_TITLE, "Choose at least one LAS point cloud.")
            return None

        missing_las = [str(path) for path in las_paths if not path.exists()]
        if missing_las:
            preview = "\n".join(missing_las[:5])
            suffix = "\n..." if len(missing_las) > 5 else ""
            messagebox.showerror(APP_TITLE, f"LAS file(s) not found:\n\n{preview}{suffix}")
            return None

        required_common = {
            "Camera calibration (.cal)": self.cal_var.get().strip(),
            "Image list (.iml)": self.iml_var.get().strip(),
            "Images folder": self.images_var.get().strip(),
        }
        missing_common = [label for label, value in required_common.items() if not value]
        if missing_common:
            messagebox.showerror(APP_TITLE, "Please fill fields:\n\n" + "\n".join(missing_common))
            return None

        for label, value in required_common.items():
            if not Path(value).exists():
                messagebox.showerror(APP_TITLE, f"{label} not found:\n{value}")
                return None

        try:
            from photo_selection import image_availability

            images_folder = Path(required_common["Images folder"])
            if not images_folder.is_dir():
                raise ValueError("Images Folder must be a folder containing the downloaded photos.")
            availability = image_availability(
                las_paths,
                required_common["Camera calibration (.cal)"],
                required_common["Image list (.iml)"],
                images_folder,
            )
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror(APP_TITLE, f"Photo check failed:\n\n{error}")
            return None
        if availability["missing"]:
            names = "\n".join(availability["missing"][:5])
            messagebox.showerror(
                APP_TITLE,
                f"Found {availability['found']} of {availability['required']} required photos in Images Folder.\n\n"
                f"Folder:\n{images_folder}\n\n"
                "Photo selection prepares a list; it does not download images. "
                "Choose the folder containing your downloaded photos, or download the missing photos first.\n\n"
                f"Missing photos (first 5):\n{names}",
            )
            return None

        try:
            chunk = int(self.chunk_var.get().strip())
            if chunk <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "Chunk Size must be a positive integer.")
            return None

        try:
            default_color = normalize_default_color(self.default_color_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return None
        self.default_color_var.set(default_color)

        resolved_las = [str(path.resolve()) for path in las_paths]
        output_single = self.output_var.get().strip()
        output_dir_raw = self.output_dir_var.get().strip()
        resolved_output_dir = str(Path(output_dir_raw).resolve()) if output_dir_raw else ""

        if len(resolved_las) == 1:
            source_path = Path(resolved_las[0])
            if output_single:
                resolved_output = str(Path(output_single).resolve())
            else:
                resolved_output = str(
                    source_path.with_name(f"{source_path.stem}{DEFAULT_BATCH_SUFFIX}.LAS")
                )
            if resolved_las[0].lower() == resolved_output.lower():
                messagebox.showerror(APP_TITLE, "Output LAS must be different from source LAS.")
                return None
        else:
            stems: dict[str, str] = {}
            for las_path in resolved_las:
                base = Path(las_path).stem.lower()
                if resolved_output_dir and base in stems:
                    messagebox.showerror(
                        APP_TITLE,
                        "Batch output collision: two LAS files share the same name.\n"
                        "Choose different files or leave Output Folder empty.",
                    )
                    return None
                stems[base] = las_path
            resolved_output = ""

        return {
            "las_list": resolved_las,
            "cal": str(Path(required_common["Camera calibration (.cal)"]).resolve()),
            "iml": str(Path(required_common["Image list (.iml)"]).resolve()),
            "images_dir": str(Path(required_common["Images folder"]).resolve()),
            "output": resolved_output,
            "output_dir": resolved_output_dir,
            "profile": DEFAULT_PROFILE,
            "chunk_size": str(chunk),
            "default_color": default_color,
        }

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state="disabled" if running else "normal")
        self.selection_button.configure(state="disabled" if running else "normal")
        self.preview_check.configure(state="disabled" if running else "normal")
        self.status_var.set("Running" if running else "Ready")

    def show_preview(self) -> None:
        if self.preview_session is not None:
            self.preview_session.show()

    def _reader_thread(self, stream, is_err: bool) -> None:
        try:
            for line in iter(stream.readline, ""):
                text = line.rstrip("\r\n")
                if text:
                    self.log_queue.put((text, is_err))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def start_processing(self) -> None:
        if self.analysis_running or (self.process and self.process.poll() is None):
            return

        cfg = self._validate_inputs()
        if cfg is None:
            return

        self._save_state()
        self.clear_log()
        self.last_error = ""
        las_list = [str(path) for path in cfg["las_list"]]  # type: ignore[index]
        self._append_log(f"Profile: {cfg['profile']}")
        self._append_log(f"LAS files: {len(las_list)}")
        if len(las_list) == 1:
            self._append_log(f"LAS: {las_list[0]}")
            if cfg["output"]:
                self._append_log(f"Output: {cfg['output']}")
        else:
            preview = las_list[:5]
            for index, las_path in enumerate(preview, start=1):
                self._append_log(f"  {index}. {las_path}")
            if len(las_list) > 5:
                self._append_log(f"  ... and {len(las_list) - 5} more")
            if cfg["output_dir"]:
                self._append_log(f"Batch output folder: {cfg['output_dir']}")
            else:
                self._append_log("Batch output folder: same folder as each input LAS")
        self._append_log(f"Images: {cfg['images_dir']}")
        self._append_log("")

        args = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "run_colorize_job.py"),
            "--cal",
            str(cfg["cal"]),
            "--iml",
            str(cfg["iml"]),
            "--images-dir",
            str(cfg["images_dir"]),
            "--profile",
            str(cfg["profile"]),
            "--chunk-size",
            str(cfg["chunk_size"]),
            "--default-color",
            str(cfg["default_color"]),
        ]
        for las_path in las_list:
            args.extend(["--las", las_path])
        if cfg["output"]:
            args.extend(["--output", str(cfg["output"])])
        if cfg["output_dir"]:
            args.extend(["--output-dir", str(cfg["output_dir"])])
        args.extend(["--output-suffix", DEFAULT_BATCH_SUFFIX])

        if self.preview_session is not None:
            self.preview_session.close()
            self.preview_session = None
        self.preview_button.configure(state="disabled")
        if self.preview_enabled_var.get():
            try:
                from experimental_preview.viewer import PreviewSession

                self.preview_session = PreviewSession(self.root)
                args.extend(["--preview-dir", str(self.preview_session.folder)])
                self.preview_button.configure(state="normal")
                self._append_log("Experimental live preview: up to 40,000 sampled points")
            except Exception as error:
                self._append_log(f"Live preview unavailable; processing continues: {error}")

        try:
            self.process = subprocess.Popen(
                args,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
        except Exception as exc:
            if self.preview_session is not None:
                self.preview_session.viewer.processing_ended(1)
            self._append_log(f"ERROR: failed to start process: {exc}")
            messagebox.showerror(APP_TITLE, f"Failed to start backend:\n{exc}")
            return

        self._set_running(True)
        self.stdout_thread = threading.Thread(
            target=self._reader_thread,
            args=(self.process.stdout, False),
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._reader_thread,
            args=(self.process.stderr, True),
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _poll_process(self) -> None:
        while not self.analysis_queue.empty():
            kind, value = self.analysis_queue.get_nowait()
            if kind == "callback":
                callback, arguments = value
                callback(*arguments)
                continue
            if kind == "progress":
                self.selection_status_var.set(value)
            else:
                self.analysis_running = False
                self.selection_button.configure(state="normal")
                self.run_button.configure(state="normal")
                if kind == "complete":
                    self.selection_status_var.set(
                        "Metadata check complete. Choose a set in the Photo Download List window."
                    )
                    self._show_photo_plans(value)
                else:
                    self.selection_status_var.set("Photo selection failed: " + str(value))
                    messagebox.showerror(APP_TITLE, str(value))
        while True:
            try:
                line, is_err = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if is_err:
                self.last_error = line
                self._append_log("ERROR: " + line)
            else:
                self._append_log(line)

        if self.process and self.process.poll() is not None:
            exit_code = self.process.returncode
            if self.preview_session is not None:
                self.preview_session.viewer.processing_ended(exit_code)
            self.process = None
            self._set_running(False)
            self.status_var.set("Finished" if exit_code == 0 else "Run failed")
            self._append_log("")
            self._append_log(f"Process exit code: {exit_code}")

            if exit_code == 0:
                messagebox.showinfo(APP_TITLE, "Processing finished successfully.")
            else:
                details = self.last_error or "See run log for details."
                messagebox.showerror(APP_TITLE, f"Processing failed:\n\n{details}")

        self.root.after(120, self._poll_process)

    def _save_state(self) -> None:
        data = {
            "data_root": self.data_root_var.get(),
            "las": self.las_var.get(),
            "cal": self.cal_var.get(),
            "iml": self.iml_var.get(),
            "images_dir": self.images_var.get(),
            "output": self.output_var.get(),
            "output_dir": self.output_dir_var.get(),
            "chunk_size": self.chunk_var.get(),
            "default_color": self.default_color_var.get(),
        }
        try:
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        self.las_var.set(str(data.get("las", "")))
        self.data_root_var.set(str(data.get("data_root", PROJECT_ROOT)))
        self.cal_var.set(str(data.get("cal", "")))
        self.iml_var.set(str(data.get("iml", "")))
        self.images_var.set(str(data.get("images_dir", "")))
        self.output_var.set(str(data.get("output", "")))
        self.output_dir_var.set(str(data.get("output_dir", "")))
        self.chunk_var.set(str(data.get("chunk_size", DEFAULT_CHUNK_SIZE)))
        self.default_color_var.set(str(data.get("default_color", DEFAULT_COLOR)))

    def _on_close(self) -> None:
        self._save_state()
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_TITLE, "Processing is running. Stop and close?"):
                return
            try:
                self.process.terminate()
            except Exception:
                pass
        if self.preview_session is not None:
            self.preview_session.close()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--smoke-test", action="store_true", help="Create and destroy GUI root.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        root = tk.Tk()
    except Exception as exc:
        append_file_line(LAUNCHER_LOG_FILE, f"Tk startup failed: {exc}")
        print(f"ERROR: GUI startup failed: {exc}", file=sys.stderr)
        return 1

    if args.smoke_test:
        root.update_idletasks()
        root.destroy()
        print("gui ok")
        return 0

    app = RGBPointGuiApp(root)
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
