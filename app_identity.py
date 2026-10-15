"""Application branding; the GitHub repository and stable installation IDs remain SIPANTAU."""
import logging
from pathlib import Path
import sys
import tkinter as tk

APP_NAME = 'Service Host: System Observation'
EXECUTABLE_STEM = 'SystemObservation'
EXECUTABLE_NAME = EXECUTABLE_STEM + '.exe'
PORTABLE_NAME = EXECUTABLE_STEM + '-portable.zip'
EXECUTABLE_ASSETS = (EXECUTABLE_NAME, 'SIPANTAU.exe')
PORTABLE_ASSETS = (PORTABLE_NAME, 'SIPANTAU-portable.zip')


def apply_window_identity(window, title=None):
    window.title(APP_NAME if title is None else title + ' — ' + APP_NAME)
    # _MEIPASS points to bundled assets for both PyInstaller formats.
    directory = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    try:
        window.iconbitmap(str(directory/'assets'/'app.ico'))
    except (tk.TclError, OSError):
        # Some development Tk installations cannot load Windows ICO files.
        logging.debug('Application icon is unavailable on this Tk platform.')
