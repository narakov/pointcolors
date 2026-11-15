"""Photo-set selection dialog and background optimization controls."""

import json
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


def show_photo_plans(app, report, project_root: Path, app_title: str, output_suffix: str) -> None:
    plans = report["plans"]
    if not any(plan["candidate_count"] for plan in plans):
        messagebox.showwarning(
            app_title,
            "No overlapping photos found. Check the coordinate system and flight metadata.\n"
            + "\n".join(item["error"] for item in report["errors"][:3]),
        )
        return
    dialog = tk.Toplevel(app.root)
    dialog.title("Photo Download List")
    dialog.geometry("1160x700")
    dialog.minsize(960, 580)
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(3, weight=1)
    ttk.Label(
        dialog, text="Select a flight and camera calibration", font=("Segoe UI", 15, "bold")
    ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 6))
    ttk.Label(
        dialog,
        text="Flights are ranked by sample point coverage. Confirm calibration using the survey records.\n"
        "Different flights may reuse JPG names. Download photos belonging to the selected IML.",
        wraplength=1100,
    ).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 8))
    table = ttk.Treeview(
        dialog,
        columns=("las", "iml", "cal", "count", "coverage"),
        show="headings",
        height=8,
        selectmode="browse",
    )
    for key, label, width in [
        ("las", "Point Cloud", 190),
        ("iml", "Image List", 290),
        ("cal", "Calibration", 240),
        ("count", "Photos / Total", 115),
        ("coverage", "Sample Coverage", 135),
    ]:
        table.heading(key, text=label)
        table.column(key, width=width, stretch=True)
    table.grid(row=2, column=0, sticky="ew", padx=16)
    for index, plan in enumerate(plans):
        table.insert(
            "",
            "end",
            iid=str(index),
            values=(
                Path(plan["las"]).name,
                Path(plan["iml"]).name,
                Path(plan["cal"]).name,
                f"{plan['candidate_count']} / {plan['listed_images']}",
                f"{plan['sample_coverage_percent']:.2f}%",
            ),
        )
    preview = ScrolledText(dialog, height=10, font=("Consolas", 10))
    preview.grid(row=3, column=0, sticky="nsew", padx=16, pady=10)
    optimization_frame = ttk.LabelFrame(dialog, text="Photo Set", padding=8)
    optimization_frame.grid(row=4, column=0, sticky="ew", padx=16)
    modes = {
        "Full Candidate Set": None,
        "Minimum Coverage Set": 0,
        "Reduced Set · Joint Allowance ≈5%": 5,
        "Reduced Set · Joint Allowance ≈10%": 10,
    }
    variant = tk.StringVar(value=next(iter(modes)))
    mode_box = ttk.Combobox(
        optimization_frame, textvariable=variant, values=list(modes), state="readonly", width=43
    )
    mode_box.grid(row=0, column=0, sticky="w")
    calculate_button = ttk.Button(optimization_frame, text="Calculate Reduced Set")
    calculate_button.grid(row=0, column=1, padx=10)
    optimization_status = tk.StringVar(
        value="The full set uses footprint bounds. Reduced sets are verified against every LAS point."
    )
    ttk.Label(optimization_frame, textvariable=optimization_status, wraplength=1080).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
    )
    ttk.Label(
        dialog,
        text="5/10% is an image-edge allowance, not a fixed overlap area. Photo quality, occlusion and calibration accuracy require separate checks.",
        wraplength=1100,
    ).grid(row=5, column=0, sticky="w", padx=16)
    controls = ttk.Frame(dialog)
    controls.grid(row=6, column=0, sticky="ew", padx=16, pady=12)
    optimized = {}

    def chosen():
        selection = table.selection()
        return plans[int(selection[0])] if selection else None

    def cache_key(plan):
        return (plan["las"], plan["cal"], plan["iml"], modes[variant.get()])

    def effective_plan():
        plan = chosen()
        if not plan or modes[variant.get()] is None:
            return plan
        return optimized.get(cache_key(plan))

    def refresh(_event=None):
        base = chosen()
        plan = effective_plan()
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        if plan:
            preview.insert(
                "end",
                "\n".join(photo["image"] for photo in plan["photos"])
                or "No overlap with the point cloud.",
            )
            if plan.get("optimization"):
                details = plan["optimization"]
                covered = min(s["coverage_percent"] for s in details["scenarios"])
                optimization_status.set(
                    f"Photos: {plan['candidate_count']} instead of {plan['original_candidate_count']}. All-point coverage verified: {covered:.6f}%. "
                    + (
                        "Minimum count proven in the camera model."
                        if details["minimum_proven_in_camera_model"]
                        else "Reduced set found; minimum count not proven."
                    )
                )
            else:
                optimization_status.set(
                    f"Full set: {plan['candidate_count']} photos. Choose a reduced mode and calculate to reduce the set."
                )
        elif base and modes[variant.get()] is not None:
            preview.insert(
                "end",
                "Calculate this variant first. The LAS will be read; photographs are not required.",
            )
        preview.configure(state="disabled")
        apply_button.configure(
            state="normal"
            if plan and plan["candidate_count"] and not app.analysis_running
            else "disabled"
        )
        calculate_button.configure(
            state="normal"
            if base
            and base["candidate_count"]
            and modes[variant.get()] is not None
            and not app.analysis_running
            else "disabled"
        )

    def calculate():
        plan = chosen()
        percent = modes[variant.get()]
        if not plan or percent is None or app.analysis_running:
            return
        key = cache_key(plan)
        app.analysis_running = True
        app.selection_button.configure(state="disabled")
        app.run_button.configure(state="disabled")
        refresh()

        def progress(text):
            if dialog.winfo_exists():
                optimization_status.set(text)

        def finished(result, error=None):
            app.analysis_running = False
            app.selection_button.configure(state="normal")
            app.run_button.configure(state="normal")
            if not dialog.winfo_exists():
                return
            if error:
                optimization_status.set("Calculation failed: " + str(error))
            else:
                optimized[key] = result
            refresh()

        def work():
            try:
                from photo_optimization import optimize_plan

                result = optimize_plan(
                    plan,
                    percent,
                    on_progress=lambda text: app.analysis_queue.put(
                        ("callback", (progress, (text,)))
                    ),
                )
                app.analysis_queue.put(("callback", (finished, (result,))))
            except Exception as error:
                app.analysis_queue.put(("callback", (finished, (None, str(error)))))

        threading.Thread(target=work, daemon=True).start()

    def apply():
        plan = effective_plan()
        if not plan or not plan["candidate_count"]:
            return
        from photo_selection import export_plan

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = project_root / "photo_plans" / f"{Path(plan['las']).stem}_{stamp}"
        try:
            export_plan(plan, output)
            (output / "all_candidates.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            downloads = output / "photos"
            downloads.mkdir()
            app.las_var.set(plan["las"])
            app.cal_var.set(plan["cal"])
            app.iml_var.set(str(output / "selected.iml"))
            app.images_var.set(str(downloads))
            source = Path(plan["las"])
            app.output_var.set(str(source.with_name(source.stem + output_suffix + ".LAS")))
            app.selection_status_var.set(
                f"List saved: {plan['candidate_count']} photos. Download them to Images Folder, then start colorization."
            )
            app._append_log(f"Download list: {output / 'photos_needed.txt'}")
            app._append_log(f"IML: {plan['iml']} · CAL: {plan['cal']}")
            app._save_state()
            messagebox.showinfo(
                app_title,
                f"Ready: {plan['candidate_count']} photo names.\n\nList: {output / 'photos_needed.txt'}\n\nDownload photos from the selected flight to:\n{downloads}\n\nCAL and the selected IML are filled in. Start colorization after downloading the photos.",
            )
            dialog.destroy()
        except Exception as error:
            messagebox.showerror(app_title, str(error))

    apply_button = ttk.Button(controls, text="Save List and Use This Set", command=apply)
    apply_button.pack(side="left")
    ttk.Button(controls, text="Close", command=dialog.destroy).pack(side="right")
    table.bind("<<TreeviewSelect>>", refresh)
    mode_box.bind("<<ComboboxSelected>>", refresh)
    calculate_button.configure(command=calculate)
    table.selection_set("0")
    refresh()
