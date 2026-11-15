# Experimental live preview

Disable it by clearing **Live 3D Preview (experimental)** before a run. The
ordinary processing path does not import this package or write snapshots.
Closing the preview window leaves processing running. Reopen it with
**Open Preview**. Snapshots contain only a 40,000-point sample and are replaced
in a temporary folder, cleaned when the app closes or starts another run.

For the local experiment, `build/last_preview_backup.txt` identifies the code
backup from immediately before this feature. After closing Point Colors, the
following command restores that backup's integration files and removes only
the new preview source and its tests:

```powershell
.venv\Scripts\python.exe build\revert_live_preview.py
```

The rollback first checks all file hashes. It refuses to overwrite any files
changed after this experiment. It preserves LAS inputs, photos, outputs,
application settings, the About background fix, and earlier optimizations.
The local backup and rollback files are excluded from Git.
