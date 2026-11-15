"""Lightweight orbitable 3D sample viewer using the existing NumPy/Pillow stack."""

import json
import math
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

BACKGROUND = (18, 25, 38)


def render_cloud(xyz, rgb, covered, size, yaw=-0.25, elevation=0.85, zoom=1.0, pan=(0, 0)):
    width, height = size
    image = Image.new("RGB", size, BACKGROUND)
    if not len(xyz):
        return image
    center = (xyz.min(axis=0) + xyz.max(axis=0)) / 2
    points = xyz - center
    cy, sy = math.cos(yaw), math.sin(yaw)
    ce, se = math.cos(elevation), math.sin(elevation)
    right = np.array([cy, sy, 0])
    up = np.array([-sy * se, cy * se, ce])
    depth = np.array([sy * ce, -cy * ce, se])
    # Fixed world-space scale keeps zoom stable while orbiting.
    extent = max(float(np.linalg.norm(np.ptp(xyz, axis=0))), 1e-9)
    scale = min(width, height) * 0.88 / extent * zoom
    px = np.rint(points @ right * scale + width / 2 + pan[0]).astype(np.int64)
    py = np.rint(-points @ up * scale + height / 2 + pan[1]).astype(np.int64)
    visible = (px >= 1) & (px < width - 1) & (py >= 1) & (py < height - 1)
    ids = np.flatnonzero(visible)
    ids = ids[np.argsort(points[ids] @ depth)[::-1]]
    # Keep the closest point at each pixel, including valid black RGB samples.
    _, unique = np.unique(py[ids] * width + px[ids], return_index=True)
    ids = ids[unique]
    colors = np.where(covered[ids, None], rgb[ids], (105, 123, 147)).astype(np.uint8)
    raster = np.asarray(image).copy()
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        raster[py[ids] + dy, px[ids] + dx] = colors
    image = Image.fromarray(raster)
    draw = ImageDraw.Draw(image)
    origin = np.array([54, height - 55])
    for axis, color, label in zip(np.eye(3), ("#fa7d78", "#7cd9a5", "#79baff"), "XYZ"):
        end = origin + np.array([axis @ right, -(axis @ up)]) * 30
        draw.line((*origin, *end), fill=color, width=2)
        draw.text(tuple(end + [3, -5]), label, fill=color)
    return image


class PreviewWindow:
    def __init__(self, root, folder):
        self.folder = Path(folder)
        self.window = tk.Toplevel(root)
        self.window.title("Live 3D Preview — Experimental")
        self.window.geometry("1040x760")
        self.window.minsize(640, 480)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        self.closed = False
        self.stamp = None
        self.frame = None
        self.timer = None
        self.draw_timer = None
        self.photo = None
        self.drag = None
        self.run_ended = None
        self.reset_view()

        toolbar = ttk.Frame(self.window, padding=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="Live 3D Preview", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Button(toolbar, text="Reset View", command=self.reset_view).pack(side="right")
        self.canvas = tk.Canvas(self.window, background="#121926", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.info = tk.StringVar(value="Waiting for colorization to start…")
        self.detail = tk.StringVar(
            value="A uniform sample of up to 40,000 points; the full LAS is processed."
        )
        footer = ttk.Frame(self.window, padding=10)
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.info, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(footer, textvariable=self.detail, wraplength=970).pack(anchor="w", pady=(4, 0))
        self.bar = ttk.Progressbar(footer, maximum=100)
        self.bar.pack(fill="x", pady=(7, 5))
        ttk.Label(
            footer,
            text="Drag: orbit · Mouse wheel: zoom · Right-drag: pan · Gray: awaiting imagery · Closing this window keeps processing",
            wraplength=970,
        ).pack(anchor="w")
        self.canvas.bind("<Configure>", self.request_draw)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.focus_set())
        self.canvas.bind("<ButtonPress-1>", lambda event: self.start_drag(event, "orbit"))
        self.canvas.bind("<B1-Motion>", self.move_drag)
        self.canvas.bind("<ButtonPress-3>", lambda event: self.start_drag(event, "pan"))
        self.canvas.bind("<B3-Motion>", self.move_drag)
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def reset_view(self):
        self.yaw, self.elevation, self.zoom, self.pan = -0.25, 0.85, 1.0, [0, 0]
        if hasattr(self, "canvas"):
            self.request_draw()

    def start_drag(self, event, mode):
        self.drag = (event.x, event.y, mode)

    def move_drag(self, event):
        if self.drag is None:
            return
        x, y, mode = self.drag
        if mode == "orbit":
            self.yaw += (event.x - x) * 0.008
            self.elevation = np.clip(self.elevation + (event.y - y) * 0.008, -1.55, 1.55)
        else:
            self.pan[0] += event.x - x
            self.pan[1] += event.y - y
        self.drag = (event.x, event.y, mode)
        self.request_draw()

    def scroll(self, event):
        self.zoom = float(np.clip(self.zoom * (1.15 if event.delta > 0 else 1 / 1.15), 0.2, 30))
        self.request_draw()

    def request_draw(self, _event=None):
        if not self.closed and self.draw_timer is None:
            self.draw_timer = self.window.after(30, self.draw)

    def draw(self):
        self.draw_timer = None
        if self.closed or self.frame is None:
            return
        width, height = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        xyz, rgb, covered, _ = self.frame
        rendered = render_cloud(
            xyz, rgb, covered, (width, height), self.yaw, self.elevation, self.zoom, self.pan
        )
        self.photo = ImageTk.PhotoImage(rendered, master=self.window)
        self.canvas.itemconfigure(self.image_item, image=self.photo)

    def poll(self):
        if self.closed:
            return
        try:
            path = self.folder / "frame.npz"
            stamp = path.stat().st_mtime_ns
            if stamp != self.stamp:
                with np.load(path, allow_pickle=False) as data:
                    self.frame = (
                        data["xyz"],
                        data["rgb"],
                        data["covered"],
                        json.loads(str(data["metadata"])),
                    )
                self.stamp = stamp
                meta = self.frame[3]
                count, total = meta["image_index"], meta["image_count"]
                stage = meta["stage"]
                progress = count / total if total else 0
                if stage.startswith("Pass 1/2"):
                    progress *= 0.5
                elif stage.startswith("Pass 2/2"):
                    progress = 0.5 + progress * 0.5
                self.bar["value"] = 100 if meta["finished"] else progress * 98
                self.info.set(f"{stage} · Photo {count}/{total} · {meta['cloud']}")
                percent = 100 * meta["covered_sample"] / meta["sample_count"]
                self.detail.set(
                    f"{meta['sample_count']:,} preview points / {meta['point_count']:,} total · "
                    f"Sample colored: {percent:.1f}% · {meta['image_name']}"
                )
                self.request_draw()
        except (OSError, ValueError, KeyError):
            pass
        if self.run_ended is not None and self.run_ended != 0:
            self.info.set("Processing stopped or failed — last available preview")
        elif self.run_ended == 0 and (self.frame is None or not self.frame[3]["finished"]):
            self.info.set("Processing finished — final preview frame unavailable")
        self.timer = self.window.after(350, self.poll)

    def processing_ended(self, code):
        self.run_ended = code

    def close(self):
        self.closed = True
        for timer in (self.timer, self.draw_timer):
            if timer is not None:
                self.window.after_cancel(timer)
        self.window.destroy()


class PreviewSession:
    """Owns one temporary stream; a closed window can be reopened during a run."""

    def __init__(self, root):
        import tempfile

        self.root = root
        self.folder = Path(tempfile.mkdtemp(prefix="pointcolors_preview_"))
        self.viewer = None
        self.show()

    def show(self):
        if self.viewer is None or self.viewer.closed:
            self.viewer = PreviewWindow(self.root, self.folder)
        else:
            self.viewer.window.lift()

    def close(self):
        import shutil

        if self.viewer is not None and not self.viewer.closed:
            self.viewer.close()
        shutil.rmtree(self.folder, ignore_errors=True)
