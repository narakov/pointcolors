# Point Colors

Desktop tools for assigning RGB colors to LAS point clouds using TerraPhoto
camera calibration (`.cal`) and image lists (`.iml`), or georeferenced orthophotos.
The application interface, progress messages and newly exported instructions are in English.

## Desktop workflow

The main window follows the order in which the inputs are used:

1. **Point Cloud** — choose one or more uncompressed LAS files.
2. **Camera Calibration** and **Image List** — specify CAL and IML files if known.
3. **Photo Selection Before Download** — select photos using the inputs above.
   If CAL or IML is empty, the application searches **Metadata Folder** and its
   subfolders for the missing metadata. Photographs are not required for selection.
4. **Images Folder** — choose the folder containing the downloaded photographs.
5. **Output LAS** or **Output Folder** — choose where colored clouds will be written.
6. Review processing settings and click **Start Colorization**.

If you already have all source photographs and know the CAL/IML files, you can
proceed directly to colorization without running photo selection.

## Installation and launch on Windows

Use 64-bit Windows and a full 64-bit Python 3.12, 3.13 or 3.14 installation
with Tcl/Tk enabled. Python 3.12 is the recommended version. If Python is not
installed, get it from [python.org](https://www.python.org/downloads/windows/)
first; setup reports a clear error if a compatible interpreter is missing.

1. Clone this repository, or pull its latest changes.
2. Double-click **`Setup Windows.bat`** inside the repository folder.
3. Wait for setup to finish. It opens the application and creates the
   **Point Colors** shortcut on your desktop with the RGB Points icon.
4. Use that shortcut for later launches; setup does not run again when you
   open the application.

Setup creates or reuses the local `.venv`, installs the pinned libraries from
`requirements.txt`, checks the graphical interface and updates the shortcut.
It installs libraries only into the application's environment. Internet access
is required to download missing libraries. Setup messages are saved in
`pointcolors_setup.log`, which is excluded from Git.

The shortcut launches the application directly without a console window.
`run_gui.bat` also launches the prepared environment; `run_gui_debug.bat` keeps
a console open for troubleshooting. Keep the repository folder in place:
the shortcut refers to it, and `.venv` must be created separately on each computer.

To update:

```powershell
git pull origin main
```

Close the application before updating. Rerun **Setup Windows.bat** when
requirements change or the shortcut needs refreshing. Repeating setup is safe:
it reuses compatible environments and preserves your cloud files and settings.

Manual setup with uv is also available:

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe rgbpoint_gui.py
```

Create or refresh the desktop shortcut after preparing the environment:

```powershell
.venv\Scripts\python.exe app_identity.py --create-shortcut
```

The RGB Points icon is shared by the shortcut, application windows and Windows
taskbar. Artwork is stored in `assets/`, including a multi-size Windows ICO.

For unattended verification, `setup_windows.ps1` accepts `-NoLaunch` and
`-NoShortcut`. Use `-Python "C:\path\to\python.exe"` to select a base interpreter
when creating a new environment. These options are also accepted by the batch launcher.

## Select photos before downloading

Choose a LAS file, supply CAL/IML or a metadata search folder, then click
**Select Photos**. No JPG files are opened or downloaded during selection.
The tool uses the LAS bounds, camera positions and orientations, calibration
and a sample of up to 12,000 LAS points to rank compatible flights.

Choose the flight and calibration in **Photo Download List**. Available sets:

- **Full Candidate Set** — conservative footprint intersection, with a 50 m margin
  and the full LAS height range.
- **Minimum Coverage Set** — minimizes the number of photos while preserving
  point coverage in the supplied camera model.
- **Reduced Set · Joint Allowance ~5% / ~10%** — uses inset image bounds to
  provide additional safety at image edges.

For a reduced set, click **Calculate Reduced Set**. The optimization starts
with a sample, then verifies every LAS point. Missing coverage constraints are
added and the calculation repeats. A minimum is claimed only when the solver
proves optimality and full verification succeeds. Otherwise a verified feasible
set is identified as reduced without a proven minimum. Unverified sets are not exported.

The 5/10% allowance means 2.5/5% inset on each image side. It does not guarantee
a fixed overlap area between rotated or irregular image footprints. At points
where no candidate has inset coverage, the full image area is used; these
exceptions are recorded in the report. Footprint overlap statistics are also saved.

Click **Save List and Use This Set** to export:

- `photos_needed.txt` — photo names to download from your storage;
- `selected.iml` — an IML for the chosen set preserving the original record fields;
- `selection.json` — selection parameters and coverage results;
- `README.txt` — instructions in English;
- `photos/` — the target folder for downloaded photographs.

The CAL and selected IML are filled into the main window. An existing
**Images Folder** is preserved; if none was selected, a new `photos/` folder is
prepared. The dialog reports how many selected photos were found there. If the
photos are already downloaded, use their existing folder without copying them.
Photo selection does not download images. Before colorization, the application
checks the required photo files and lists missing names instead of starting an
incomplete run. Download missing photos or choose the correct folder first.
If you select photos
again from an exported set, its `selection.json` is used to find the original
flight IML when that file is still available.

Different flights may reuse JPG names. Always download photographs belonging
to the selected flight. Coverage alone cannot identify the correct calibration.
Subfolders are supported, but duplicate matching names in different subfolders
require choosing a specific flight folder.

Coverage concerns point-to-image projection. It does not establish image sharpness,
correct calibration, accurate alignment, or visibility through occluding objects.

## Command-line examples

Metadata-only selection:

```powershell
.venv\Scripts\python.exe photo_selection.py --las cloud.las --data-root . --output-dir photo_plans\cloud
```

Optimization (`--overlap` accepts `0`, `5` or `10`):

```powershell
.venv\Scripts\python.exe photo_optimization.py --plan photo_plans\cloud\recommended_download\selection.json --output-dir photo_plans\cloud\minimum --overlap 0
```

Colorization using a named profile:

```powershell
.venv\Scripts\python.exe run_colorize_job.py --las cloud.las --cal camera.cal --iml selected.iml --images-dir photos --output cloud_rgb.las --profile enhanced
```

Profiles: `standard`, `roll_minus`, and `enhanced`. Enhanced combines a Roll
Minus pass with a Standard fallback. Repeat `--las` for batch processing and
use `--output-dir` instead of `--output`.

The same colorization engine processes both reduced and full photo sets. It
skips points outside conservative camera bounds when the calibration permits
this, and reuses decoded coordinates when they fit a 512 MiB cache. Larger
clouds are streamed in chunks. These optimizations do not reduce the selected
photo set or change the color selection rule.

Orthophotos require an accompanying world file for each tile:

```powershell
.venv\Scripts\python.exe colorize_las.py --las cloud.las --ortho-dir ortho_tiles --output cloud_rgb.las
```

Use `--help` with each script for projection options, chunk size and other controls.

## Experimental live preview

Enable **Live 3D Preview (experimental)** before starting colorization. A separate
window displays a uniform sample of up to 40,000 points, updated about once a
second after completed photos. Drag to orbit, use the mouse wheel to zoom, and
right-drag to pan. **Reset View** restores the initial camera. **Open Preview**
reopens a closed preview window during or after processing.

The full LAS is still processed. Enhanced preserves the primary pass colors
while displaying fallback updates. The final preview reads colors from the
saved output. Closing the preview does not stop processing. Preview failures
are isolated from colorization. The preview adds some CPU and disk activity.

This feature is off by default. Clear its checkbox to run without preview
imports, sampling or snapshot writes. Its implementation is isolated in
`experimental_preview/`; only optional callbacks connect it to the engine.
For the local trial, a pre-feature backup and guarded rollback tool are stored
under `build/`; see `experimental_preview/README.md` for removal instructions.

## Main files

- `rgbpoint_gui.py` — desktop interface.
- `app_identity.py` — application icons and Windows desktop shortcut.
- `Setup Windows.bat` / `setup_windows.ps1` — first-run Windows setup.
- `photo_selection_dialog.py` — photo-set selection and optimization controls.
- `experimental_preview/` — optional viewer and temporary snapshot stream.
- `photo_selection.py` — metadata discovery and photo download lists.
- `photo_optimization.py` — minimum photo cover with full LAS verification.
- `colorize_las.py` — photo projection and orthophoto colorization.
- `run_colorize_job.py` — processing profiles and batch runs.
- `blend_las_rgb.py` — fill black RGB values from a fallback colored LAS.
- `compare_las_rgb.py` — compare coordinates and colors against a reference LAS.

GUI state and logs are stored next to the application. Input LAS files are never
used as output targets. Supported input LAS point formats are 0, 1, 2 and 3;
compressed LAZ is not supported.

## Checks

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe rgbpoint_gui.py --smoke-test
```
