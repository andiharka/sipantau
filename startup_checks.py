"""A packaging smoke check that never installs startup tasks or activates controls."""
import json
from pathlib import Path
import sys


def smoke_test(report_path):
    report = {'ok': False}
    window = None
    try:
        # Literal imports also ensure PyInstaller collects these optional code paths.
        import tkinter as tk
        import keyboard
        import mss
        import requests
        import psutil
        from PIL import Image
        import windows_autorun
        import windows_runtime
        import remote_commands
        import screenshot_codec
        import updater
        from version import VERSION, BUILD_FORMAT
        from app_identity import APP_NAME
        directory = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        icon = directory / 'assets' / 'app.ico'
        if not icon.is_file():
            raise RuntimeError('Bundled app.ico is missing')
        window = tk.Tk()
        window.withdraw()
        window.title(APP_NAME)
        window.iconbitmap(str(icon))
        tk.Label(window, text=APP_NAME).pack()
        window.update_idletasks()
        report.update(ok=True, version=VERSION, build_format=BUILD_FORMAT,
                      frozen=bool(getattr(sys, 'frozen', False)))
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if window is not None:
            window.destroy()
    Path(report_path).write_text(json.dumps(report) + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1
