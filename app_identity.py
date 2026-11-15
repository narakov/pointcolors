"""Application artwork and Windows shell identity."""

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent
APP_ID = "narakov.PointColors.Desktop"
ICON_PATH = ROOT / "assets" / "pointcolors.ico"
PNG_PATH = ROOT / "assets" / "pointcolors.png"


def configure_process_identity():
    if sys.platform != "win32":
        return False
    try:
        function = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_long
        return function(APP_ID) >= 0
    except (OSError, AttributeError):
        return False


def apply_window_icon(window):
    import tkinter as tk

    try:
        if sys.platform == "win32" and ICON_PATH.is_file():
            window.iconbitmap(str(ICON_PATH))
            window.iconbitmap(default=str(ICON_PATH))
        elif PNG_PATH.is_file():
            image = tk.PhotoImage(master=window, file=str(PNG_PATH))
            window.iconphoto(True, image)
            window._pointcolors_icon = image
        else:
            return False
        return True
    except (tk.TclError, OSError):
        return False


def set_shortcut_identity(path):
    """Match a shortcut to the process identity for Windows taskbar grouping."""

    class GUID(ctypes.Structure):
        _fields_ = [
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        ]

    class PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    class Value(ctypes.Union):
        _fields_ = [
            ("string", ctypes.c_wchar_p),
            ("pointers", ctypes.c_void_p * 2),
            ("integer", ctypes.c_int64),
        ]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("value", Value)]

    def guid(value):
        return GUID.from_buffer_copy(UUID(value).bytes_le)

    def check(result):
        if result < 0:
            raise OSError(f"Windows shell operation failed: HRESULT 0x{result & 0xFFFFFFFF:08X}")

    ole32, shell32 = ctypes.WinDLL("ole32"), ctypes.WinDLL("shell32")
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.c_long
    check(ole32.CoInitialize(None))
    store = ctypes.c_void_p()
    try:
        get_store = shell32.SHGetPropertyStoreFromParsingName
        get_store.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_store.restype = ctypes.c_long
        iid = guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
        check(get_store(str(Path(path).resolve()), None, 2, ctypes.byref(iid), ctypes.byref(store)))
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        def method(slot, *types):
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(vtable[slot])

        key = PropertyKey(guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)
        value = PropVariant()
        value.vt = 31  # VT_LPWSTR; the Python string remains alive through Commit.
        value.value.string = APP_ID
        check(
            method(6, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(
                store, ctypes.byref(key), ctypes.byref(value)
            )
        )
        check(method(7)(store))

        # Read back the property instead of assuming the shortcut was updated.
        actual = PropVariant()
        check(
            method(5, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(
                store, ctypes.byref(key), ctypes.byref(actual)
            )
        )
        try:
            if actual.vt != 31 or actual.value.string != APP_ID:
                raise OSError("Windows did not retain the shortcut application identity.")
        finally:
            ole32.PropVariantClear(ctypes.byref(actual))
    finally:
        if store.value:
            table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(store)
        ole32.CoUninitialize()


def create_desktop_shortcut():
    if sys.platform != "win32":
        raise OSError("Desktop shortcut creation is available on Windows.")
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file() or not ICON_PATH.is_file():
        raise OSError("Prepare the Python environment and application icon first.")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise OSError("Windows PowerShell was not found.")
    script = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$path = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Point Colors.lnk'
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $env:POINTCOLORS_SHORTCUT_PYTHON
$shortcut.Arguments = '"' + (Join-Path $env:POINTCOLORS_SHORTCUT_ROOT 'rgbpoint_gui.py') + '"'
$shortcut.WorkingDirectory = $env:POINTCOLORS_SHORTCUT_ROOT
$shortcut.IconLocation = (Join-Path $env:POINTCOLORS_SHORTCUT_ROOT 'assets\\pointcolors.ico') + ',0'
$shortcut.Description = 'Point Colors - RGB point cloud colorizer'
$shortcut.Save()
[Console]::WriteLine($path)
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        env={
            **os.environ,
            "POINTCOLORS_SHORTCUT_PYTHON": str(pythonw),
            "POINTCOLORS_SHORTCUT_ROOT": str(ROOT),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    path = Path(result.stdout.strip())
    set_shortcut_identity(path)
    notify = ctypes.WinDLL("shell32").SHChangeNotify
    notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    notify(0x2000, 0x5, ctypes.cast(ctypes.c_wchar_p(str(path)), ctypes.c_void_p), None)
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-shortcut", action="store_true")
    args = parser.parse_args()
    if args.create_shortcut:
        print(create_desktop_shortcut())
