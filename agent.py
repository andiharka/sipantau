import os
import sys
import json
import copy
import time
import uuid
import socket
import threading
import queue
import re
import math
from client_config import ConfigurationError, read_config, configuration_summary
from control_runtime import SessionClock, utc
import logging
from logging.handlers import RotatingFileHandler
from client_logging import NetworkLog
from version import VERSION, BUILD_FORMAT
from app_identity import APP_NAME, PORTABLE_ASSETS, apply_window_identity
import subprocess
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timezone, timedelta
import requests
import urllib3
import psutil

# Run before config/log/state loading. CI only creates a hidden Tk window and
# checks bundled dependencies; it must never hook keys, lock, poll or register tasks.
if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--smoke-test':
    from startup_checks import smoke_test
    sys.exit(smoke_test(sys.argv[2]) if len(sys.argv) == 3 else 2)

# 1. Config Loader
CONFIG_ERROR = None
CONFIG_LOADED_AT = None


def load_config():
    global CONFIG_ERROR, CONFIG_LOADED_AT
    directory = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    path = os.path.join(directory, "config.json")
    CONFIG_ERROR = None
    CONFIG_LOADED_AT = None
    try:
        data = read_config(path)
        CONFIG_LOADED_AT = datetime.now().astimezone().strftime("%d/%m/%Y %H:%M:%S %Z")
        return data, path
    except ConfigurationError as error:
        CONFIG_ERROR = str(error)
        # Retained only for safe imports; startup displays a warning and exits before locking.
        return {"api_key": "", "server_url": "", "master_password": None}, path


config, config_path = load_config()
state_path = os.path.join(os.path.dirname(config_path), "state.json")

# 2. Setup Logging
log_path = os.path.join(os.path.dirname(config_path), "agent.log")
logging.basicConfig(
    handlers=[RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                 encoding="utf-8", delay=True)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

NETWORK_LOG = NetworkLog()
LAST_HEARTBEAT = None
LOCAL_ADOPTION = None
BOOT_ID = uuid.uuid4().hex
COMMAND_MANAGER = None
LOCAL_CONTROL_EPOCH = 0
AUTORUN_STATUS = 'Belum diperiksa'
HOST_ACTION_LOCK = threading.Lock()
UPDATE_CHECK_LOCK = threading.Lock()

# 3. Dynamic Hosts Path (NT vs Unix development fallback)
if os.name == 'nt':
    hosts_path = r"C:\Windows\System32\drivers\etc\hosts"
else:
    # Development fallback for macOS/Linux to test locally
    hosts_path = os.path.join(os.path.dirname(config_path), "hosts_mock")
    if not os.path.exists(hosts_path):
        with open(hosts_path, "w") as f:
            f.write("# Local Development Host Database\n127.0.0.1 localhost\n")

# Import keyboard conditionally for Windows shortcut locking
if os.name == 'nt':
    try:
        import keyboard
    except ImportError:
        keyboard = None
else:
    keyboard = None

# GUI Global Control State
root = None
lock_window = None
is_locked = False
lock_health_error = None
gui_lock = threading.Lock()
active_notifications = []
notification_lock = threading.Lock()
timer_window = None

# Local Persistent State Management
STATE_LOCK = threading.RLock()
SESSION_CLOCK = SessionClock()
GUI_COMMANDS = queue.Queue()
SAVE_LOCK = threading.Lock()
HOSTS_LOCK = threading.RLock()
local_state = {
    "is_locked": True,
    "from_when": None,
    "duration": None,
    "control_version": None,
    "session_id": None,
    "expires_at": None,
    "server_offset_seconds": 0,
    "applied_control_version": None,
    "attempted_control_version": None,
    "control_error": None,
    "blocked_apps": [],
    "blocked_sites": [],
    "pending_actions": []
}

def load_state():
    global local_state
    if os.path.exists(state_path):
        try:
            with open(state_path, "r") as f:
                loaded = json.load(f)
                with STATE_LOCK:
                    if not isinstance(loaded, dict) or not isinstance(loaded.get("is_locked"), bool):
                        raise ValueError("Invalid cached lock state")
                    local_state.update(loaded)
                    # Older clients stored bare action names. They have no server
                    # version, so replaying them could undo a newer staff command.
                    pending = local_state.get("pending_actions", [])
                    if not isinstance(pending, list):
                        pending = []
                    local_state["pending_actions"] = [
                        action for action in pending
                        if isinstance(action, dict)
                        and action.get("action") in ("force-lock", "force-unlock", "start-session")
                        and (action.get("expected_version") is None or
                             (isinstance(action.get("expected_version"), int)
                              and not isinstance(action.get("expected_version"), bool)))
                        and isinstance(action.get("id"), str)
                    ][-1:]
            with STATE_LOCK:
                local_state["applied_control_version"] = None
                local_state["attempted_control_version"] = None
                local_state["control_error"] = None
                if not local_state["is_locked"]:
                    if not local_state.get("expires_at") and local_state.get("from_when") and local_state.get("duration"):
                        from datetime import timedelta
                        local_state["expires_at"] = (utc(local_state["from_when"]) + timedelta(seconds=local_state["duration"])).isoformat()
                    SESSION_CLOCK.restore(local_state.get("control_version"), local_state.get("session_id"),
                                          local_state.get("expires_at"), local_state.get("server_offset_seconds", 0))
            logging.info("State successfully loaded from file.")
        except Exception as e:
            with STATE_LOCK:
                local_state.update(is_locked=True, pending_actions=[], expires_at=None,
                                   from_when=None, duration=None, control_version=None)
                SESSION_CLOCK.clear()
            logging.error(f"Error loading state from file: {e}")
    else:
        save_state()

def save_state():
    try:
        with SAVE_LOCK:
            with STATE_LOCK:
                state_copy = copy.deepcopy(local_state)
            temp_path = state_path + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(state_copy, f, indent=2)
            os.replace(temp_path, state_path)
        return True
    except Exception as e:
        logging.error(f"Error saving state to file: {e}")
        return False

# Event to trigger heartbeat wakeups immediately upon manual action
heartbeat_event = threading.Event()

def _set_local_control_locked(locked, action):
    global LOCAL_CONTROL_EPOCH
    LOCAL_CONTROL_EPOCH += 1
    local_state["is_locked"] = locked
    local_state["from_when"] = None
    local_state["duration"] = None
    local_state["expires_at"] = None
    local_state["session_id"] = None
    local_state["applied_control_version"] = None
    local_state["attempted_control_version"] = None
    local_state["control_error"] = None
    SESSION_CLOCK.clear()
    pending = {"id": uuid.uuid4().hex, "action": action,
               "expected_version": local_state.get("control_version")}
    local_state["pending_actions"] = [pending]
    return pending["id"]


def set_local_control(locked, action, expected_version=None):
    with STATE_LOCK:
        if expected_version is not None and expected_version != local_state.get("control_version"):
            return None
        pending_id = _set_local_control_locked(locked, action)
    save_state()
    heartbeat_event.set()
    return pending_id


def authorize_local_access(password, minutes=None):
    """Both local actions require the configured staff password, without network I/O."""
    import hmac
    configured = config.get("master_password")
    if not isinstance(configured, str) or not configured or not isinstance(password, str):
        raise ValueError("Password petugas belum diatur.")
    if not hmac.compare_digest(password.encode(), configured.encode()):
        raise ValueError("Password petugas salah.")
    if minutes is None:
        return set_local_control(False, "force-unlock")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
        raise ValueError("Durasi harus 1–1440 menit.")
    seconds = minutes * 60
    with STATE_LOCK:
        pending_id = _set_local_control_locked(False, "start-session")
        now = datetime.now(timezone.utc) + timedelta(seconds=local_state.get("server_offset_seconds", 0))
        expiry = (now + timedelta(seconds=seconds)).isoformat()
        local_state.update(from_when=now.isoformat(), duration=seconds,
                           expires_at=expiry, session_id="local:" + pending_id)
        local_state["pending_actions"][0]["expires_at"] = expiry
        SESSION_CLOCK.sync(local_state.get("control_version"), local_state["session_id"], expiry, now.isoformat())
    save_state()
    heartbeat_event.set()
    logging.info("Staff started a local timed session for %s minutes.", minutes)
    return pending_id


def enqueue_gui(callback):
    GUI_COMMANDS.put(callback)


def process_gui_commands():
    for _ in range(100):
        try:
            callback = GUI_COMMANDS.get_nowait()
        except queue.Empty:
            break
        try:
            callback()
        except Exception as error:
            logging.error("GUI callback failed: %s", error)
    root.after(50, process_gui_commands)


def apply_gui_control(version):
    with STATE_LOCK:
        if version != local_state.get("control_version") or local_state["pending_actions"]:
            return False
        before = (local_state.get("applied_control_version"), local_state.get("control_error"))
        local_state["attempted_control_version"] = version
        applied = False
        try:
            if local_state["is_locked"]:
                show_lock_screen_gui()
            else:
                hide_lock_screen_gui()
            local_state["applied_control_version"] = version
            local_state["control_error"] = None
            applied = True
        except Exception as error:
            local_state["applied_control_version"] = None
            local_state["control_error"] = re.sub(r'[\x00-\x1f\x7f]', ' ', str(error))[:500]
            logging.error("Could not apply control version %s: %s", version, error)
        changed = before != (local_state.get("applied_control_version"), local_state.get("control_error"))
    if changed:
        save_state()
        heartbeat_event.set()
    return applied


def apply_local_gui(pending_id):
    with STATE_LOCK:
        pending = local_state["pending_actions"]
        if not pending:
            # A fast heartbeat can acknowledge the action before this Tk callback runs.
            # Reconcile current state, including any newer server lock, instead of skipping it.
            return apply_gui_control(local_state.get("control_version"))
        if pending[0].get("id") != pending_id:
            return False
        applied = False
        try:
            if local_state["is_locked"]:
                show_lock_screen_gui()
            else:
                hide_lock_screen_gui()
            local_state["control_error"] = None
            applied = True
        except Exception as error:
            local_state["attempted_control_version"] = local_state.get("control_version")
            local_state["control_error"] = re.sub(r'[\x00-\x1f\x7f]', ' ', str(error))[:500]
            logging.error("Could not apply local control: %s", error)
    save_state()
    heartbeat_event.set()
    return applied


def restore_gui_state():
    with STATE_LOCK:
        pending = local_state["pending_actions"]
        pending_id = pending[0].get("id") if pending else None
        version = local_state.get("control_version")
    if pending_id:
        apply_local_gui(pending_id)
    else:
        apply_gui_control(version)


def expire_local_session():
    with STATE_LOCK:
        remaining = SESSION_CLOCK.remaining()
        if local_state["is_locked"] or remaining is None or remaining > 0:
            return False
        pending_id = _set_local_control_locked(True, "force-lock")
    save_state()
    heartbeat_event.set()
    enqueue_gui(lambda: apply_local_gui(pending_id))
    return True


# 5. Helpers for System Status
def get_agent_hostname():
    custom_name = config.get("hostname")
    if custom_name:
        return custom_name.strip()
    return socket.gethostname()

def update_autorun(enabled):
    global AUTORUN_STATUS
    if os.name != 'nt':
        AUTORUN_STATUS = 'Tidak tersedia · khusus Windows'
        return True
    try:
        from windows_autorun import configure_autorun
        task = configure_autorun(enabled, sys.executable, os.path.dirname(config_path),
                                 script=None if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
        AUTORUN_STATUS = ('Terdaftar · ' if enabled else 'Nonaktif · ') + task
        logging.info('Autorun login task %s: %s', 'registered' if enabled else 'removed', task)
        return True
    except Exception as error:
        AUTORUN_STATUS = 'Gagal · ' + str(error)
        logging.error('Failed to configure autorun login task: %s', error)
        return False


def warn_autorun_failure():
    messagebox.showwarning(
        'Startup otomatis belum siap',
        'Pengaturan jalankan saat masuk belum berhasil diterapkan.\n\n' + AUTORUN_STATUS +
        '\n\nPeriksa izin administrator dan layanan Task Scheduler, lalu jalankan kembali agen.',
        parent=lock_window or root)


def configure_startup_autorun():
    if not update_autorun(config.get('autorun', False)):
        enqueue_gui(warn_autorun_failure)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not need to be reachable
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def get_running_apps():
    apps = set()
    for proc in psutil.process_iter(['name']):
        try:
            name = proc.info['name']
            if name:
                apps.add(name.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return sorted(list(apps))

def get_blocked_sites():
    blocked = set()
    try:
        with open(hosts_path, 'r') as f:
            for line in f:
                line_stripped = line.strip()
                if "# agent-blocked" in line_stripped:
                    parts = line_stripped.split()
                    if len(parts) >= 2 and parts[0] == "127.0.0.1":
                        domain = parts[1]
                        if domain.startswith("www."):
                            domain = domain[4:]
                        blocked.add(domain)
    except FileNotFoundError:
        pass
    return sorted(list(blocked))

def block_domain(domain):
    domain = domain.strip().lower()
    domains_to_block = [domain, f"www.{domain}"]
    
    try:
        with open(hosts_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
        
    lines = content.splitlines()
    updated = False
    
    for dom in domains_to_block:
        exists = False
        for line in lines:
            line_stripped = line.strip().lower()
            if not line_stripped.startswith("#"):
                parts = line_stripped.split()
                if len(parts) >= 2 and parts[0] == "127.0.0.1" and parts[1] == dom:
                    exists = True
                    break
        if not exists:
            lines.append(f"127.0.0.1 {dom} # agent-blocked")
            updated = True
            
    if updated:
        with open(hosts_path, 'w') as f:
            f.write("\n".join(lines) + "\n")
        
        # Flush DNS
        if os.name == 'nt':
            subprocess.run(["ipconfig", "/flushdns"], shell=True, capture_output=True)
            
    return updated

def unblock_domain(domain):
    domain = domain.strip().lower()
    targets = [domain, f"www.{domain}"]
    
    try:
        with open(hosts_path, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return False
        
    new_lines = []
    removed = False
    
    for line in lines:
        line_stripped = line.strip().lower()
        is_match = False
        if "127.0.0.1" in line_stripped:
            parts = line_stripped.split()
            if len(parts) >= 2 and parts[0] == "127.0.0.1":
                if parts[1] in targets and "# agent-blocked" in line_stripped:
                    is_match = True
        
        if is_match:
            removed = True
        else:
            new_lines.append(line)
            
    if removed:
        with open(hosts_path, 'w') as f:
            f.writelines(new_lines)
            
        # Flush DNS
        if os.name == 'nt':
            subprocess.run(["ipconfig", "/flushdns"], shell=True, capture_output=True)
            
    return removed

def sync_hosts_file(blocked_sites):
    with HOSTS_LOCK:
        current_blocked = get_blocked_sites()
        to_block = [d.strip().lower() for d in blocked_sites if d.strip()]
    
        for domain in to_block:
            if domain not in current_blocked:
                block_domain(domain)
                logging.info(f"Blocked domain: {domain}")
            
        for domain in current_blocked:
            if domain not in to_block:
                unblock_domain(domain)
                logging.info(f"Unblocked domain: {domain}")

def restore_cached_site_policy():
    try:
        with HOSTS_LOCK:
            with STATE_LOCK:
                sites = list(local_state.get("blocked_sites", []))
            sync_hosts_file(sites)
    except Exception as error:
        logging.error("Cached website policy could not be restored: %s", error)


# 7. GUI Kiosk Screen Operations (Must be executed on main Tkinter thread)
UI_BG = "#141c23"
UI_PANEL = "#1e2a33"
UI_INPUT = "#273742"
UI_TEXT = "#eef3f5"
UI_MUTED = "#a8bbc6"
UI_ACCENT = "#87cbbb"
TIMER_PRESETS = ((30, "30 menit"), (60, "1 jam"), (180, "3 jam"))


def startup_config_ready(parent):
    if CONFIG_ERROR:
        messagebox.showwarning(
            "Konfigurasi belum siap",
            CONFIG_ERROR + "\n\nLokasi yang dibaca:\n" + config_path +
            "\n\nPerbaiki file tersebut, lalu jalankan kembali agen. Komputer belum dikunci oleh proses ini.",
            parent=parent)
        try:
            logging.error("Startup configuration rejected at %s: %s", config_path, CONFIG_ERROR)
        except OSError:
            pass  # A read-only directory must not prevent the warning dialog.
        return False
    logging.info("Configuration loaded from %s at %s.", config_path, CONFIG_LOADED_AT)
    return True


def draw_password_eye(canvas, visible):
    canvas.delete("icon")
    canvas.create_line(9, 18, 16, 12, 24, 12, 31, 18, 24, 24, 16, 24, 9, 18,
                       smooth=True, fill=UI_MUTED, width=2, tags="icon")
    canvas.create_oval(17, 15, 23, 21, outline=UI_MUTED, width=2, tags="icon")
    if visible:
        canvas.create_line(10, 28, 30, 8, fill=UI_TEXT, width=2, tags="icon")


def show_configuration_viewer(window):
    existing = getattr(window, "_settings_dialog", None)
    if existing and existing.winfo_exists():
        existing.lift()
        return
    dialog = tk.Toplevel(window)
    window._settings_dialog = dialog
    apply_window_identity(dialog, "Konfigurasi komputer")
    dialog.configure(bg=UI_PANEL)
    dialog.transient(window)
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    width = min(660, window.winfo_screenwidth() - 48)
    height = min(560, window.winfo_screenheight() - 64)
    dialog.geometry(f"{width}x{height}+{max(0, (window.winfo_screenwidth() - width) // 2)}+{max(0, (window.winfo_screenheight() - height) // 2)}")
    body = tk.Frame(dialog, bg=UI_PANEL, padx=24, pady=22)
    body.pack(fill="both", expand=True)
    tk.Label(body, text="Konfigurasi aktif", font=("Segoe UI", 18, "bold"),
             fg=UI_TEXT, bg=UI_PANEL).pack(anchor="w")
    tk.Label(body, text="Nilai saat proses dimulai. Perubahan file memerlukan mulai ulang.",
             font=("Segoe UI", 10), fg=UI_MUTED, bg=UI_PANEL,
             wraplength=width - 52, justify="left").pack(anchor="w", pady=(6, 14))
    content = tk.Frame(body, bg=UI_PANEL)
    scrollbar = tk.Scrollbar(content)
    scrollbar.pack(side="right", fill="y")
    details = tk.Text(content, bg=UI_BG, fg=UI_TEXT, font=("Consolas", 10),
                      wrap="word", relief="flat", padx=12, pady=12,
                      yscrollcommand=scrollbar.set, highlightthickness=1,
                      highlightbackground=UI_INPUT, insertbackground=UI_TEXT)
    details.pack(fill="both", expand=True)
    scrollbar.config(command=details.yview)
    rows = [("File konfigurasi", config_path), ("Dibaca pada", CONFIG_LOADED_AT or "Tidak diketahui"),
            ("Versi agen", VERSION), ("Format paket", {"onedir": "Folder lengkap", "onefile": "File tunggal", "source": "Python"}.get(BUILD_FORMAT, BUILD_FORMAT)),
            *configuration_summary(config, get_agent_hostname(), update_supported=BUILD_FORMAT == "onefile"),
            ("Pendaftaran saat masuk", AUTORUN_STATUS)]
    details.insert("1.0", "\n\n".join(label + "\n" + str(value) for label, value in rows))
    details.config(state="disabled")

    def close():
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        dialog.destroy()
        window._settings_dialog = None
        if window is lock_window and is_locked and window.winfo_exists():
            window.grab_set()
            getattr(window, "_password_entry", window).focus_set()

    # Reserve footer space before the scrollable configuration body.
    footer = tk.Frame(body, bg=UI_PANEL)
    footer.pack(side="bottom", fill="x", pady=(14, 0))
    update_status = tk.Label(footer, text=f"Versi saat ini: v{VERSION}.", font=("Segoe UI", 10),
                             fg=UI_MUTED, bg=UI_PANEL, wraplength=width - 52, justify="left", anchor="w")
    update_status.pack(fill="x", pady=(0, 10))
    actions = tk.Frame(footer, bg=UI_PANEL)
    actions.pack(fill="x")

    def finish_check(text, color):
        if (window is not lock_window or window._settings_dialog is not dialog
                or not dialog.winfo_exists()):
            return
        update_status.config(text=text, fg=color)
        check_button.config(text="Periksa pembaruan", state="normal")

    def check_updates():
        if not UPDATE_CHECK_LOCK.acquire(blocking=False):
            update_status.config(text="Pemeriksaan sedang berjalan. Coba lagi sebentar.", fg=UI_MUTED)
            return
        check_button.config(text="Memeriksa…", state="disabled")
        update_status.config(text="Memeriksa rilis terbaru…", fg=UI_MUTED)

        def worker():
            color = "#e9ba72"
            try:
                from updater import check_for_update
                selected = check_for_update(VERSION)
                if selected:
                    text = f"Pembaruan tersedia: {selected[0]}. Versi saat ini: v{VERSION}."
                    if selected[1].get("name") in PORTABLE_ASSETS:
                        text += " Unduh paket folder lengkap dan pasang secara manual."
                else:
                    text = f"Versi v{VERSION} sudah yang terbaru."
                color = UI_ACCENT
            except (requests.Timeout, requests.ConnectionError):
                text = "Tidak dapat terhubung ke GitHub. Periksa koneksi internet, lalu coba lagi."
            except requests.HTTPError as error:
                status = error.response.status_code if error.response is not None else None
                if status in (403, 429):
                    text = "GitHub membatasi permintaan. Coba lagi nanti."
                elif status == 404:
                    text = "Belum ada rilis publik yang tersedia."
                else:
                    text = "GitHub belum dapat diakses. Coba lagi nanti."
            except ValueError:
                text = "Informasi rilis terbaru belum dapat diverifikasi. Coba lagi nanti."
            except Exception as error:
                logging.warning("Manual update check deferred (%s).", type(error).__name__)
                text = "Pembaruan belum dapat diperiksa. Coba lagi nanti."
            finally:
                UPDATE_CHECK_LOCK.release()
            # All widget access stays on the Tk thread, including after a dialog closes.
            enqueue_gui(lambda: finish_check(text, color))

        try:
            threading.Thread(target=worker, daemon=True, name="ManualUpdateCheck").start()
        except RuntimeError:
            UPDATE_CHECK_LOCK.release()
            finish_check("Pemeriksaan belum dapat dimulai. Coba lagi nanti.", "#e9ba72")

    check_button = tk.Button(actions, text="Periksa pembaruan", command=check_updates,
                             font=("Segoe UI", 11), bg=UI_INPUT, fg=UI_TEXT,
                             activebackground="#354b59", activeforeground=UI_TEXT,
                             relief="flat", padx=16, pady=8)
    check_button.pack(side="left")
    tk.Button(actions, text="Tutup", command=close, font=("Segoe UI", 11), bg=UI_INPUT,
              fg=UI_TEXT, activebackground="#354b59", activeforeground=UI_TEXT,
              relief="flat", padx=24, pady=8).pack(side="right")
    content.pack(fill="both", expand=True)
    dialog.protocol("WM_DELETE_WINDOW", close)
    dialog.bind("<Escape>", lambda event: close())
    dialog.grab_set()
    dialog.focus_set()


def show_lock_screen_gui():
    global lock_window, is_locked, lock_health_error
    window = lock_window
    if window is not None and window.winfo_exists():
        if is_locked:
            if lock_health_error:
                raise RuntimeError(lock_health_error)
            dialog = getattr(window, "_settings_dialog", None)
            if dialog and dialog.winfo_exists():
                dialog.lift()
            else:
                window.lift()
            return
        # Recover an incomplete previous build without leaving an orphan fullscreen window.
        hide_lock_screen_gui()

    window = tk.Toplevel(root)
    apply_window_identity(window)
    lock_window = window
    window._closing = False
    window._regrab_job = None
    window._status_job = None
    window._settings_dialog = None
    window.configure(bg=UI_BG)
    window.attributes("-fullscreen", True)
    window.attributes("-topmost", True)
    window.overrideredirect(True)
    window.protocol("WM_DELETE_WINDOW", lambda: "break")
    keyboard_error = None
    if os.name == 'nt' and keyboard:
        try:
            keyboard.block_key('windows')
            for shortcut in ('alt+tab', 'alt+f4', 'alt+esc', 'ctrl+esc', 'windows+d', 'windows+r'):
                keyboard.add_hotkey(shortcut, lambda: None, suppress=True)
        except Exception as error:
            keyboard_error = str(error)
    elif os.name == 'nt':
        keyboard_error = 'Keyboard hook dependency is unavailable'
    window.bind("<Alt-F4>", lambda event: "break")
    window.bind("<Alt-Tab>", lambda event: "break")

    def re_grab():
        window._regrab_job = None
        if window is not lock_window or window._closing or not is_locked:
            return
        dialog = window._settings_dialog
        if dialog and dialog.winfo_exists():
            return
        if window.winfo_exists():
            focused = window.focus_displayof()
            if focused is not None and focused.winfo_toplevel() is window:
                return
            window.focus_force()
            pwd_entry.focus_set()
            window.grab_set()

    def schedule_regrab(event=None):
        if not window._closing and window._regrab_job is None:
            window._regrab_job = window.after(20, re_grab)

    window.bind("<FocusOut>", schedule_regrab)
    window.focus_force()
    window.grab_set()
    # Keep the form short enough for a 768px laptop screen, including Windows text scaling.
    compact = window.winfo_screenheight() < 900
    frame = tk.Frame(window, bg=UI_BG, padx=30, pady=18)
    frame.place(relx=0.5, rely=0.5, anchor="center")
    wrap = max(260, min(560, window.winfo_screenwidth() - 100))
    tk.Label(frame, text=get_agent_hostname(), font=("Segoe UI", 12),
             fg=UI_MUTED, bg=UI_BG).pack(anchor="w", pady=(0, 12))
    tk.Label(frame, text="Komputer terkunci", font=("Segoe UI", 26 if compact else 32, "bold"),
             fg=UI_TEXT, bg=UI_BG).pack(anchor="w")
    tk.Label(frame, text="Hubungi petugas untuk memulai sesi penggunaan.",
             font=("Segoe UI", 12), fg=UI_MUTED, bg=UI_BG, wraplength=wrap,
             justify="left").pack(anchor="w", pady=(8, 10))
    status = tk.Label(frame, font=("Segoe UI", 10), bg=UI_BG)
    status.pack(anchor="w", pady=(0, 18))

    def update_connection_status():
        if window is not lock_window or window._closing or not status.winfo_exists():
            return
        connected = LAST_HEARTBEAT is not None and time.monotonic() - LAST_HEARTBEAT < 60
        status.config(text="●  Terhubung ke server" if connected else "●  Server tidak terhubung · akses lokal tersedia",
                      fg=UI_ACCENT if connected else "#e9ba72")
        window._status_job = window.after(2000, update_connection_status)

    update_connection_status()
    tk.Label(frame, text="Password petugas", font=("Segoe UI", 11, "bold"),
             fg=UI_TEXT, bg=UI_BG).pack(anchor="w", pady=(0, 8))
    password_row = tk.Frame(frame, bg=UI_INPUT, highlightthickness=1, highlightbackground="#405562")
    password_row.pack(fill="x")
    pwd_entry = tk.Entry(password_row, show="•", font=("Segoe UI", 14), bg=UI_INPUT,
                         fg=UI_TEXT, insertbackground=UI_TEXT, relief="flat", width=27)
    pwd_entry.pack(side="left", fill="x", expand=True, padx=(12, 0), ipady=9)
    window._password_entry = pwd_entry
    eye = tk.Canvas(password_row, width=40, height=36, bg=UI_INPUT, highlightthickness=1,
                    highlightbackground=UI_INPUT, highlightcolor=UI_ACCENT,
                    takefocus=1, cursor="hand2")
    eye.pack(side="right", padx=4)
    draw_password_eye(eye, False)

    def toggle_password(event=None):
        visible = bool(pwd_entry.cget("show"))
        pwd_entry.config(show="" if visible else "•")
        draw_password_eye(eye, visible)
        return "break"

    eye.bind("<Button-1>", toggle_password)
    eye.bind("<Return>", toggle_password)
    eye.bind("<space>", toggle_password)
    eye.bind("<Enter>", lambda event: eye.config(highlightbackground=UI_MUTED))
    eye.bind("<Leave>", lambda event: eye.config(highlightbackground=UI_INPUT))
    tk.Label(frame, text="Durasi sesi", font=("Segoe UI", 11), fg=UI_MUTED,
             bg=UI_BG).pack(anchor="w", pady=(16, 8))
    minutes = tk.StringVar(value="30")
    presets = tk.Frame(frame, bg=UI_BG)
    presets.pack(fill="x")
    preset_buttons = []

    def select_duration(value):
        minutes.set(str(value))
        for duration, button in preset_buttons:
            selected = duration == value
            button.config(bg=UI_ACCENT if selected else UI_INPUT,
                          fg=UI_BG if selected else UI_TEXT)

    for value, label in TIMER_PRESETS:
        button = tk.Button(presets, text=label, command=lambda value=value: select_duration(value),
                           font=("Segoe UI", 11), relief="flat", pady=8, cursor="hand2",
                           activebackground="#a4ddcf", activeforeground=UI_BG)
        button.pack(side="left", fill="x", expand=True, padx=(0, 8 if value != 180 else 0))
        preset_buttons.append((value, button))
    select_duration(30)
    custom = tk.Frame(frame, bg=UI_BG)
    custom.pack(fill="x", pady=(10, 0))
    tk.Label(custom, text="Atau atur menit", font=("Segoe UI", 10), fg=UI_MUTED,
             bg=UI_BG).pack(side="left", padx=(0, 10))
    duration_entry = tk.Spinbox(custom, from_=1, to=1440, textvariable=minutes,
                               font=("Segoe UI", 11), width=6, bg=UI_INPUT, fg=UI_TEXT,
                               insertbackground=UI_TEXT, relief="flat", buttonbackground=UI_INPUT)
    duration_entry.pack(side="left", ipady=4)

    def duration_changed(*args):
        for duration, button in preset_buttons:
            selected = minutes.get() == str(duration)
            button.config(bg=UI_ACCENT if selected else UI_INPUT,
                          fg=UI_BG if selected else UI_TEXT)

    minutes.trace_add("write", duration_changed)
    error_label = tk.Label(frame, text="", font=("Segoe UI", 10), fg="#f5a8a8",
                           bg=UI_BG, wraplength=wrap, justify="left", anchor="w")
    error_label.pack(fill="x", pady=(8, 0))
    pwd_entry.bind("<KeyRelease>", lambda event: error_label.config(text=""))

    def handle_access(timed=True):
        error_label.config(text="")
        try:
            if timed:
                try:
                    duration = int(minutes.get())
                except ValueError:
                    raise ValueError("Durasi harus 1–1440 menit.") from None
            else:
                duration = None
            pending_id = authorize_local_access(pwd_entry.get(), duration)
            pwd_entry.delete(0, tk.END)
            pwd_entry.config(show="•")
            draw_password_eye(eye, False)
            if not apply_local_gui(pending_id):
                raise RuntimeError(local_state.get("control_error") or "Status komputer berubah. Silakan coba lagi.")
        except (ValueError, RuntimeError) as error:
            if window.winfo_exists():
                error_label.config(text=str(error))
            else:
                messagebox.showwarning("Akses petugas", str(error), parent=root)

    tk.Button(frame, text="Mulai sesi dengan timer", font=("Segoe UI", 12, "bold"),
              bg=UI_ACCENT, fg=UI_BG, activebackground="#a4ddcf", activeforeground=UI_BG,
              relief="flat", pady=10, cursor="hand2", command=handle_access).pack(fill="x", pady=(8, 6))
    tk.Button(frame, text="Buka tanpa batas waktu", font=("Segoe UI", 10),
              bg=UI_BG, fg=UI_MUTED, activebackground=UI_INPUT, activeforeground=UI_TEXT,
              relief="flat", pady=6, cursor="hand2", command=lambda: handle_access(False)).pack(fill="x")
    pwd_entry.bind("<Return>", lambda event: handle_access(True))

    # Invisible hit area becomes a settings button on hover or keyboard focus.
    settings = tk.Canvas(window, width=44, height=44, bg=UI_BG, highlightthickness=0,
                         takefocus=1, cursor="hand2")
    tk.Label(window, text=f"Versi {VERSION}", font=("Segoe UI", 9), fg=UI_MUTED,
             bg=UI_BG).place(relx=0, rely=1, x=22, y=-28, anchor="sw")
    settings.place(relx=1, rely=1, x=-18, y=-18, anchor="se")
    gear = settings.create_text(22, 22, text="⚙", font=("Segoe UI Symbol", 21), fill=UI_BG)
    settings.bind("<Enter>", lambda event: settings.itemconfig(gear, fill=UI_MUTED))
    settings.bind("<Leave>", lambda event: settings.itemconfig(gear, fill=UI_BG))
    settings.bind("<FocusIn>", lambda event: settings.itemconfig(gear, fill=UI_ACCENT))
    settings.bind("<FocusOut>", lambda event: settings.itemconfig(gear, fill=UI_BG))

    def open_settings(event=None):
        pwd_entry.config(show="•")
        draw_password_eye(eye, False)
        show_configuration_viewer(window)
        return "break"

    settings.bind("<Button-1>", open_settings)
    settings.bind("<Return>", open_settings)
    settings.bind("<space>", open_settings)
    pwd_entry.focus_set()
    with gui_lock:
        is_locked = True
    lock_health_error = 'Lock screen visible but shortcut blocking failed: ' + keyboard_error if keyboard_error else None
    if lock_health_error:
        raise RuntimeError(lock_health_error)


def hide_lock_screen_gui():
    global lock_window, is_locked, lock_health_error
    window = lock_window
    with gui_lock:
        if window is None and not is_locked and not lock_health_error:
            return
    keyboard_error = None
    if os.name == 'nt' and keyboard:
        try:
            keyboard.unhook_all()
        except Exception as error:
            keyboard_error = str(error)
    if window is not None:
        window._closing = True
        window.unbind("<FocusOut>")
        for name in ("_regrab_job", "_status_job"):
            job = getattr(window, name, None)
            if job:
                try:
                    window.after_cancel(job)
                except tk.TclError:
                    pass
        # A transferred/already-released grab must never prevent destruction.
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            if window.winfo_exists():
                raise
        if lock_window is window:
            lock_window = None
    with gui_lock:
        is_locked = False
    lock_health_error = 'Shortcut hooks could not be released: ' + keyboard_error if keyboard_error else None
    if lock_health_error:
        raise RuntimeError(lock_health_error)


def rearrange_notifications():
    with notification_lock:
        for index, noti in enumerate(active_notifications):
            width = 320
            height = 90
            try:
                screen_width = noti.winfo_screenwidth()
                screen_height = noti.winfo_screenheight()
                margin_x = 24
                margin_y = 60
                y_offset = index * (height + 12)
                x = screen_width - width - margin_x
                y = screen_height - height - margin_y - y_offset
                noti.geometry(f"{width}x{height}+{x}+{y}")
            except Exception:
                pass

def trigger_kill_notification(exe_name):
    # Create borderless, topmost window
    noti = tk.Toplevel(root)
    apply_window_identity(noti)
    noti.overrideredirect(True)
    noti.attributes("-topmost", True)
    
    try:
        noti.attributes("-alpha", 0.0)
    except Exception:
        pass
        
    noti.configure(bg="#1e1e2e")
    
    width = 320
    height = 90
    
    screen_width = noti.winfo_screenwidth()
    screen_height = noti.winfo_screenheight()
    margin_x = 24
    margin_y = 60
    
    with notification_lock:
        active_notifications.append(noti)
        index = len(active_notifications) - 1
        
    y_offset = index * (height + 12)
    x = screen_width - width - margin_x
    y = screen_height - height - margin_y - y_offset
    
    noti.geometry(f"{width}x{height}+{x}+{y}")
    
    # Red accent bar on the left
    accent_bar = tk.Frame(noti, bg="#ef4444", width=5)
    accent_bar.pack(side="left", fill="y")
    
    # Message container
    container = tk.Frame(noti, bg="#1e1e2e", padx=15, pady=10)
    container.pack(side="left", fill="both", expand=True)
    
    header_label = tk.Label(
        container,
        text="⚠️ Aplikasi Dihentikan",
        font=("Helvetica", 14, "bold"),
        fg="#ef4444",
        bg="#1e1e2e",
        anchor="w"
    )
    header_label.pack(fill="x", anchor="w")
    
    message = f"'{exe_name}' diblokir oleh administrator."
    msg_label = tk.Label(
        container,
        text=message,
        font=("Helvetica", 10),
        fg="#dddddd",
        bg="#1e1e2e",
        anchor="w",
        wraplength=270,
        justify="left"
    )
    msg_label.pack(fill="x", anchor="w", pady=(4, 0))
    
    # Close button
    close_btn = tk.Label(
        noti,
        text="×",
        font=("Helvetica", 14),
        fg="#a6adc8",
        bg="#1e1e2e",
        cursor="hand2"
    )
    close_btn.place(x=width - 25, y=5)
    
    close_btn.bind("<Enter>", lambda e: close_btn.config(fg="#ef4444"))
    close_btn.bind("<Leave>", lambda e: close_btn.config(fg="#a6adc8"))
    
    # Fade in animation
    def fade_in(alpha=0.0):
        if alpha < 0.95:
            alpha += 0.1
            try:
                noti.attributes("-alpha", alpha)
            except Exception:
                pass
            noti.after(20, lambda: fade_in(alpha))
            
    fade_in(0.0)
    
    # Fade out and destroy
    def close_noti():
        def fade_out(alpha=0.95):
            if alpha > 0.05:
                alpha -= 0.1
                try:
                    noti.attributes("-alpha", alpha)
                except Exception:
                    pass
                noti.after(20, lambda: fade_out(alpha))
            else:
                try:
                    noti.destroy()
                except Exception:
                    pass
                with notification_lock:
                    if noti in active_notifications:
                        active_notifications.remove(noti)
                rearrange_notifications()
        fade_out(0.95)
        
    close_btn.bind("<Button-1>", lambda e: close_noti())
    noti.after(5000, close_noti)

# --- Drag and drop support for floating window ---
def start_drag(event):
    win = event.widget.winfo_toplevel()
    win._drag_x = event.x
    win._drag_y = event.y

def drag_motion(event):
    win = event.widget.winfo_toplevel()
    dx = event.x - win._drag_x
    dy = event.y - win._drag_y
    x = max(0, min(win.winfo_x() + dx, win.winfo_screenwidth() - win.winfo_width()))
    y = max(0, min(win.winfo_y() + dy, win.winfo_screenheight() - win.winfo_height()))
    win.geometry(f"+{x}+{y}")

# --- Stop session action ---
def stop_session_action():
    if messagebox.askyesno("Akhiri sesi", "Akhiri sesi sekarang?\nKomputer akan langsung dikunci.", parent=timer_window or root):
        pending_id = set_local_control(True, "force-lock")
        apply_local_gui(pending_id)

def dismiss_timer_window():
    global timer_window
    window = timer_window
    timer_window = None
    if window is not None:
        try:
            window.destroy()
        except tk.TclError:
            pass


def timer_display(remaining):
    seconds = max(0, math.ceil(remaining))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    if remaining < 180:
        return clock, "#f29797", "Sesi segera berakhir.\nSimpan pekerjaan Anda sekarang.", True
    if remaining < 600:
        return clock, "#e9ba72", "Kurang dari 10 menit.\nSimpan pekerjaan sebelum sesi berakhir.", True
    return clock, UI_ACCENT, "Seret panel untuk memindahkannya.\nAkhiri sesi setelah selesai.", False


# --- Floating countdown timer widget ---
def show_timer_window_gui():
    global timer_window
    if timer_window is not None:
        return
    window = tk.Toplevel(root)
    apply_window_identity(window)
    timer_window = window
    window.configure(bg=UI_PANEL)
    window.overrideredirect(True)
    window.attributes("-topmost", True)
    window._drag_x = 0
    window._drag_y = 0
    window.mouse_inside = False
    window.layout_expanded = None
    window.session_identity = None
    window.session_total = 1
    window.geometry(f"280x104+{max(0, window.winfo_screenwidth() - 304)}+24")
    container = tk.Frame(window, bg=UI_PANEL, padx=16, pady=12,
                         highlightthickness=1, highlightbackground="#3c515d")
    container.pack(fill="both", expand=True)
    window.container_frame = container
    heading = tk.Frame(container, bg=UI_PANEL)
    heading.pack(fill="x")
    title = tk.Label(heading, text="Sisa waktu", font=("Segoe UI", 9), fg=UI_MUTED, bg=UI_PANEL)
    title.pack(side="left")
    pc = get_agent_hostname()
    name = tk.Label(heading, text=pc[:19] + ("…" if len(pc) > 19 else ""),
                    font=("Segoe UI", 9), fg=UI_MUTED, bg=UI_PANEL)
    name.pack(side="right")
    time_label = tk.Label(container, text="", font=("Consolas", 26, "bold"), fg=UI_ACCENT,
                          bg=UI_PANEL, anchor="w")
    time_label.pack(fill="x", pady=(2, 7))
    window.time_label = time_label
    progress = tk.Canvas(container, height=3, bg=UI_INPUT, highlightthickness=0)
    progress.pack(fill="x")
    window.progress = progress
    window.progress_fill = progress.create_rectangle(0, 0, 248, 3, fill=UI_ACCENT, outline="")
    details = tk.Frame(container, bg=UI_PANEL)
    window.details_frame = details
    description = tk.Label(details, text="", font=("Segoe UI", 9), fg=UI_MUTED, bg=UI_PANEL,
                           anchor="w", justify="left", wraplength=245)
    description.pack(fill="x", pady=(10, 8))
    window.desc_label = description
    stop = tk.Button(details, text="Akhiri sesi", font=("Segoe UI", 10), bg=UI_INPUT,
                     fg=UI_TEXT, activebackground="#405563", activeforeground=UI_TEXT,
                     relief="flat", pady=5, cursor="hand2", command=stop_session_action)
    stop.pack(fill="x")
    window.stop_btn = stop

    def on_enter(event):
        if window is timer_window:
            window.mouse_inside = True
            update_timer_layout()

    def on_leave(event):
        if window is not timer_window:
            return
        x, y = window.winfo_pointerxy()
        if not (window.winfo_rootx() <= x < window.winfo_rootx() + window.winfo_width()
                and window.winfo_rooty() <= y < window.winfo_rooty() + window.winfo_height()):
            window.mouse_inside = False
            update_timer_layout()

    draggable = (container, heading, title, name, time_label, progress, details, description)
    for widget in draggable:
        widget.bind("<Button-1>", start_drag)
        widget.bind("<B1-Motion>", drag_motion)
    for widget in (window, *draggable, stop):
        widget.bind("<Enter>", on_enter, add="+")
        widget.bind("<Leave>", on_leave, add="+")
    # Keep controls expanded while the panel receives keyboard focus.
    window.bind("<FocusIn>", on_enter, add="+")
    stop.bind("<FocusIn>", on_enter, add="+")
    stop.bind("<FocusOut>", on_leave, add="+")
    update_timer_layout()


def update_timer_layout():
    window = timer_window
    if window is None:
        return
    with STATE_LOCK:
        remaining = SESSION_CLOCK.remaining()
        identity = (local_state.get("session_id"), local_state.get("expires_at"))
        duration = local_state.get("duration")
    if remaining is None or remaining <= 0:
        dismiss_timer_window()
        return
    if identity != window.session_identity:
        window.session_identity = identity
        window.session_total = max(remaining, duration if isinstance(duration, (int, float)) else 0, 1)
    clock, accent, message, warning = timer_display(remaining)
    expanded = warning or window.mouse_inside
    window.time_label.config(text=clock, fg=accent)
    window.desc_label.config(text=message, fg=accent if warning else UI_MUTED)
    window.container_frame.config(highlightbackground=accent if warning else "#3c515d")
    track_width = max(1, window.progress.winfo_width())
    fraction = max(0, min(1, remaining / window.session_total))
    window.progress.coords(window.progress_fill, 0, 0, track_width * fraction, 3)
    window.progress.itemconfig(window.progress_fill, fill=accent)
    if expanded != window.layout_expanded:
        window.layout_expanded = expanded
        if expanded:
            window.details_frame.pack(fill="x")
        else:
            window.details_frame.pack_forget()
        height = 194 if expanded else 104
        x = max(0, min(window.winfo_x(), window.winfo_screenwidth() - 280))
        y = max(0, min(window.winfo_y(), window.winfo_screenheight() - height))
        window.geometry(f"280x{height}+{x}+{y}")


def update_timer_gui():
    with STATE_LOCK:
        remaining = SESSION_CLOCK.remaining()
        desired_active = not local_state["is_locked"] and remaining is not None and remaining > 0
    with gui_lock:
        # Do not show a countdown over a failed or not-yet-applied unlock.
        visible_unlocked = not is_locked and lock_window is None and not lock_health_error
    if desired_active and visible_unlocked:
        if timer_window is None:
            show_timer_window_gui()
        else:
            update_timer_layout()
    else:
        dismiss_timer_window()
    if root:
        root.after(1000, update_timer_gui)


# 8. Background Process Blocked Apps Watcher Thread
def blacklist_watcher():
    while True:
        try:
            with STATE_LOCK:
                blocked = [app.lower() for app in local_state.get("blocked_apps", [])]
            if blocked:
                for proc in psutil.process_iter(['name']):
                    try:
                        name = proc.info['name']
                        if name and name.lower() in blocked:
                            proc.kill()
                            msg = f"Killed blocked app: {name}"
                            logging.info(msg)
                            print(msg)
                            if root:
                                enqueue_gui(lambda n=name: trigger_kill_notification(n))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except Exception as e:
            # Prevent thread termination
            pass
        time.sleep(15)

# 9. Background Heartbeat Thread

def agent_headers():
    return {"X-API-Key": config.get("api_key", ""),
            "X-Pantau-Hostname": get_agent_hostname(), "Content-Type": "application/json"}


def discard_pending(pending):
    with STATE_LOCK:
        local_state["pending_actions"] = [item for item in local_state["pending_actions"]
                                            if item.get("id") != pending.get("id")]
    save_state()


def flush_pending_action(server_url, headers, verify_ssl):
    global LOCAL_ADOPTION
    with STATE_LOCK:
        actions = copy.deepcopy(local_state.get("pending_actions", []))
    for pending in actions:
        version = pending.get("expected_version")
        if not isinstance(version, int) or isinstance(version, bool):
            discard_pending(pending)
            continue
        try:
            payload = {"hostname": get_agent_hostname(), "ip": get_local_ip(), "expected_version": version}
            if pending["action"] == "start-session":
                with STATE_LOCK:
                    current = local_state["pending_actions"]
                    if not current or current[0]["id"] != pending["id"]:
                        continue
                    remaining = SESSION_CLOCK.remaining()
                if remaining is None or remaining <= 0:
                    expire_local_session()
                    continue
                import math
                payload.update(remaining_seconds=min(86400, math.ceil(remaining)), expires_at=pending["expires_at"])
            response = requests.post(f"{server_url}/api/pantau/action/{pending['action']}",
                                     json=payload,
                                     headers=headers, timeout=5, verify=verify_ssl)
            if response.status_code in (200, 201):
                new_version = response.json()["control_version"]
                if not isinstance(new_version, int) or isinstance(new_version, bool):
                    raise ValueError('Invalid action control_version')
                NETWORK_LOG.recovered('local-action')
                with STATE_LOCK:
                    local_state["control_version"] = new_version
                    current = local_state["pending_actions"]
                    if current and current[0].get("id") == pending.get("id"):
                        if pending["action"] == "start-session":
                            LOCAL_ADOPTION = (new_version, SESSION_CLOCK.deadline)
                        local_state["pending_actions"] = []
                    elif current and current[0].get("expected_version") == version:
                        current[0]["expected_version"] = new_version
                save_state()
            elif response.status_code in (409, 422):
                discard_pending(pending)
                logging.warning('Discarded stale or invalid local action: %s', pending['action'])
            else:
                NETWORK_LOG.failure('local-action', 'HTTP %s' % response.status_code)
        except Exception as error:
            NETWORK_LOG.failure('local-action', error)


def accept_server_state(data, round_trip):
    global LAST_HEARTBEAT, LOCAL_ADOPTION
    version = data.get("control_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ValueError('Invalid heartbeat control_version')
    if not isinstance(data.get("is_locked"), (bool, int)) or data.get("is_locked") not in (0, 1):
        raise ValueError('Invalid heartbeat lock state')
    server_now = utc(data['server_now'])
    LAST_HEARTBEAT = time.monotonic()
    NETWORK_LOG.recovered('heartbeat')
    with STATE_LOCK:
        previous = local_state.get("control_version")
        if previous is not None and version < previous:
            return
        pending = local_state["pending_actions"]
        # A newer server decision supersedes an offline local action, including
        # a previously accepted action whose response was lost.
        if pending and pending[0].get("expected_version") != version:
            local_state["pending_actions"] = []
        local_state["control_version"] = version
        if local_state["pending_actions"]:
            return
        locked = bool(data["is_locked"])
        expires_at = None if locked else data.get("expires_at")
        SESSION_CLOCK.sync(version, data.get("session_id"), expires_at, data['server_now'], round_trip)
        if LOCAL_ADOPTION and LOCAL_ADOPTION[0] == version and SESSION_CLOCK.deadline is not None:
            SESSION_CLOCK.deadline = min(SESSION_CLOCK.deadline, LOCAL_ADOPTION[1])
        LOCAL_ADOPTION = None
        local_state["is_locked"] = locked
        local_state["from_when"] = data.get("from_when") if not locked else None
        local_state["duration"] = data.get("duration") if not locked else None
        local_state["session_id"] = data.get("session_id") if not locked else None
        local_state["expires_at"] = expires_at
        local_state["server_offset_seconds"] = (server_now - datetime.now(timezone.utc)).total_seconds() + round_trip / 2
    save_state()
    enqueue_gui(lambda v=version: apply_gui_control(v))


def heartbeat_once():
    server_url = config.get("server_url", "").rstrip('/')
    if not server_url:
        return
    verify_ssl = config.get("verify_ssl", True)
    headers = agent_headers()
    flush_pending_action(server_url, headers, verify_ssl)
    with gui_lock:
        actual_locked = bool(is_locked)
    with STATE_LOCK:
        payload = {"hostname": get_agent_hostname(), "ip": get_local_ip(),
                   "running_apps": get_running_apps(), "reported_is_locked": actual_locked,
                   "applied_control_version": local_state.get("applied_control_version"),
                   "attempted_control_version": local_state.get("attempted_control_version"),
                   "control_error": local_state.get("control_error"),
                   "agent_version": VERSION, "boot_id": BOOT_ID,
                   "capabilities": command_capabilities()}
    started = time.monotonic()
    response = requests.post(f"{server_url}/api/pantau/heartbeat", json=payload,
                             headers=headers, timeout=5, verify=verify_ssl)
    if response.status_code not in (200, 201):
        raise RuntimeError(f'Heartbeat failed with HTTP {response.status_code}')
    data = response.json()
    accept_server_state(data, time.monotonic() - started)
    if COMMAND_MANAGER:
        COMMAND_MANAGER.observe(data.get("commands", []), data["server_now"])


def heartbeat_loop():
    if not config.get("verify_ssl", True):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    while True:
        try:
            heartbeat_once()
        except Exception as error:
            NETWORK_LOG.failure('heartbeat', error)
        heartbeat_event.wait(5)
        heartbeat_event.clear()

# 10. Background Blocklist Sync Thread
def blocklist_sync_loop():
    global local_state
    server_url = config.get("server_url", "")
    verify_ssl = config.get("verify_ssl", True)
    
    headers = agent_headers()
    
    while True:
        try:
            if not server_url:
                time.sleep(10)
                continue
            
            url = f"{server_url.rstrip('/')}/api/pantau/blocklist"
            res = requests.get(url, headers=headers, timeout=5, verify=verify_ssl)
            if res.status_code == 200:
                data = res.json()
                banned_apps = data.get("blocked_apps", data.get("blacklisted_apps", []))
                blocked_sites = data.get("blocked_sites", [])
                if not isinstance(banned_apps, list) or not isinstance(blocked_sites, list) or any(
                        not isinstance(item, str) for item in banned_apps + blocked_sites):
                    raise ValueError("Invalid blocklist response")
                NETWORK_LOG.recovered('blocklist')
                with STATE_LOCK:
                    local_state["blocked_apps"] = banned_apps
                    local_state["blocked_sites"] = blocked_sites
                save_state()
                
                # Sync sites list to local hosts mapping
                sync_hosts_file(blocked_sites)
            else:
                NETWORK_LOG.failure('blocklist', 'HTTP %s' % res.status_code)
        except Exception as e:
            NETWORK_LOG.failure('blocklist', e)
            
        time.sleep(180)

# 11. Local Session Countdown Timer Thread
def session_timer_loop():
    while True:
        try:
            if expire_local_session():
                logging.info('Local session expired; locking PC.')
        except Exception as error:
            logging.error('Local countdown failed: %s', error)
        time.sleep(1)

def command_capabilities():
    if os.name != 'nt' or COMMAND_MANAGER is None:
        return []
    capabilities = ["shutdown"]
    try:
        import mss
        from PIL import Image
        capabilities.append("screenshot")
    except ImportError:
        pass
    return capabilities


def command_guard():
    with STATE_LOCK:
        return (local_state.get("control_version"), LOCAL_CONTROL_EPOCH,
                bool(local_state["pending_actions"]))


def command_response(response):
    from remote_commands import CommandRejected, CommandTransient
    if response.status_code >= 500:
        raise CommandTransient("Server unavailable")
    if response.status_code not in (200, 201):
        raise CommandRejected("Server rejected command")
    data = response.json()
    if data.get("success") is not True:
        raise CommandRejected("Invalid command response")
    return data


def report_remote_command(command_id, status, error=None):
    from remote_commands import CommandTransient
    try:
        response = requests.post(config["server_url"].rstrip('/') + f"/api/pantau/commands/{command_id}/result",
                                 json={"hostname": get_agent_hostname(), "boot_id": BOOT_ID, "status": status, "error": error},
                                 headers=agent_headers(), verify=config.get("verify_ssl", True), timeout=5)
    except requests.RequestException as error:
        raise CommandTransient("Command report connection failed") from error
    return command_response(response)


def upload_remote_screenshot(command_id, image, captured_at=None):
    from remote_commands import CommandTransient
    from screenshot_codec import image_type
    mime, extension = image_type(image)
    headers = agent_headers()
    headers.pop("Content-Type", None)  # requests generates the multipart boundary.
    try:
        response = requests.post(config["server_url"].rstrip('/') + f"/api/pantau/commands/{command_id}/image",
                                 data={"hostname": get_agent_hostname(), "boot_id": BOOT_ID,
                                       "captured_at": captured_at or datetime.now(timezone.utc).isoformat()},
                                 files={"image": (f"screenshot.{extension}", image, mime)},
                                 headers=headers, verify=config.get("verify_ssl", True), timeout=(5, 15))
    except requests.RequestException as error:
        raise CommandTransient("Screenshot upload connection failed") from error
    return command_response(response)


def capture_primary_monitor():
    if os.name != 'nt':
        raise RuntimeError("Screenshot commands require Windows")
    from windows_runtime import session_locked
    from remote_commands import CaptureUnavailable
    if session_locked():
        raise CaptureUnavailable("Windows sedang terkunci pada desktop aman; screenshot tidak tersedia.")
    from mss import MSS
    from screenshot_codec import encode_screenshot
    with MSS() as capture:
        # MSS monitor ordering is positional; find the display containing the desktop origin.
        monitor = next((m for m in capture.monitors[1:] if m["left"] == 0 and m["top"] == 0), capture.monitors[1])
        if monitor["width"] > 8192 or monitor["height"] > 8192 or monitor["width"] * monitor["height"] > 16_000_000:
            raise RuntimeError("Monitor exceeds screenshot pixel limit")
        shot = capture.grab(monitor)
        return encode_screenshot(shot.rgb, shot.size, config.get("screenshot_jpeg_quality", 80))


def commit_remote_shutdown(version, token):
    with STATE_LOCK:
        current = command_guard()
        if current[1:] != token[1:] or current[0] not in (token[0], version):
            raise RuntimeError("Local staff changed PC control during shutdown authorization")
        local_state.update(control_version=version, is_locked=True, from_when=None, duration=None,
                           expires_at=None, session_id=None, applied_control_version=None,
                           attempted_control_version=None, control_error=None)
        SESSION_CLOCK.clear()
    if not save_state():
        raise RuntimeError("Cannot save next-startup lock state")


def submit_windows_shutdown():
    if os.name != 'nt':
        raise RuntimeError("Shutdown commands require Windows")
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "shutdown.exe")
    result = subprocess.run([executable, "/s", "/t", "0", "/d", "p:0:0"],
                            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError("Windows rejected shutdown")


def show_shutdown_notice(seconds):
    def show():
        if not COMMAND_MANAGER or not COMMAND_MANAGER.busy.is_set():
            return
        window = tk.Toplevel(root)
        apply_window_identity(window)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg="#7c2d12")
        window.geometry(f"520x110+{max(0, (root.winfo_screenwidth()-520)//2)}+30")
        label = tk.Label(window, bg="#7c2d12", fg="white", font=("Segoe UI", 14, "bold"), wraplength=490)
        label.pack(fill="both", expand=True, padx=15, pady=12)
        deadline = time.monotonic() + seconds
        def tick():
            if not COMMAND_MANAGER.busy.is_set():
                window.destroy()
                return
            left = max(0, int(deadline - time.monotonic()))
            label.config(text=f"PC akan dimatikan oleh petugas dalam {left} detik.\nSegera simpan pekerjaan Anda.")
            window.after(250, tick)
        tick()
    enqueue_gui(show)


def initialize_remote_commands():
    global COMMAND_MANAGER
    from remote_commands import CommandReceipts, RemoteCommands
    try:
        receipts = CommandReceipts(os.path.join(os.path.dirname(config_path), "command-receipts.json"))
        COMMAND_MANAGER = RemoteCommands(BOOT_ID, receipts, report_remote_command,
            upload_remote_screenshot, capture_primary_monitor, submit_windows_shutdown,
            command_guard, show_shutdown_notice, commit_remote_shutdown, HOST_ACTION_LOCK)
        threading.Thread(target=COMMAND_MANAGER.run, daemon=True).start()
    except Exception as error:
        logging.error("Remote commands disabled until receipt cache is repaired (%s).", type(error).__name__)
        COMMAND_MANAGER = None


def update_allowed():
    with STATE_LOCK:
        with gui_lock:
            return (local_state["is_locked"] and is_locked and not lock_health_error
                    and SESSION_CLOCK.remaining() is None and not local_state["pending_actions"]
                    and not (COMMAND_MANAGER and COMMAND_MANAGER.busy.is_set()))


def install_release(staged, tag, digest):
    from pathlib import Path
    from windows_runtime import lock_for_update
    from updater import installer_script
    if not HOST_ACTION_LOCK.acquire(blocking=False):
        return
    try:
        with STATE_LOCK:
            if not update_allowed():
                return
            directory = Path(config_path).parent
            marker = directory / ("update-health-" + uuid.uuid4().hex + ".ready")
            helper = directory / "update-install.ps1"
            helper.write_text(installer_script(sys.executable, staged, marker, os.getpid(), digest, tag), encoding="utf-8-sig")
            # Native Windows lock keeps the desktop protected while the Tk process restarts.
            if not lock_for_update():
                raise RuntimeError("Windows could not lock the workstation for update")
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", str(helper)], creationflags=subprocess.CREATE_NO_WINDOW)
            logging.info("Installing verified update %s; previous executable retained for rollback.", tag)
            logging.shutdown()
            os._exit(0)

    finally:
        HOST_ACTION_LOCK.release()



def report_update_ready():
    from updater import signal_ready
    with STATE_LOCK:
        with gui_lock:
            healthy = not lock_health_error and local_state["is_locked"] == is_locked
            signal_ready(os.path.dirname(config_path), is_locked, healthy)
    # Readiness can arrive after a queued control action; the helper waits up to 180 seconds.
    if root:
        root.after(1000, report_update_ready)


# Main Application Entrypoint
if __name__ == "__main__":
    if os.name == 'nt':
        import ctypes
        # Prevent two agents (and two rotating log writers) on the same Windows session.
        ctypes.windll.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
        instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SIPANTAUAgent")
        if not instance_mutex or ctypes.windll.kernel32.GetLastError() == 183:
            sys.exit(0)
    # A real Tk parent makes startup warnings visible even for --noconsole releases.
    root = tk.Tk()
    apply_window_identity(root)
    root.withdraw()
    if not startup_config_ready(root):
        root.destroy()
        sys.exit(1)

    # Load persistent state from state.json cache
    load_state()

    # Expired cached sessions must not briefly unlock the desktop on startup.
    expire_local_session()
    threading.Thread(target=restore_cached_site_policy, daemon=True).start()

    # Enforce cached local state even when an offline action is awaiting sync.
    if config.get("auto_lock_on_run", False):
        set_local_control(True, "force-lock")
    root.after(0, restore_gui_state)
    root.after(50, process_gui_commands)

    # Task Scheduler may be slow/unavailable; never delay the kiosk or network loops.
    AUTORUN_STATUS = 'Sedang diperiksa'
    threading.Thread(target=configure_startup_autorun, name='Autorun', daemon=True).start()

    # Run the background blacklist process checker
    watcher_thread = threading.Thread(target=blacklist_watcher, daemon=True)
    watcher_thread.start()

    initialize_remote_commands()

    # Run the client status heartbeat loop (replacing the old push Flask API server)
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Run the background blocklist sync loop
    blocklist_sync_thread = threading.Thread(target=blocklist_sync_loop, daemon=True)
    blocklist_sync_thread.start()

    # Run the local session timer loop
    timer_thread = threading.Thread(target=session_timer_loop, daemon=True)
    timer_thread.start()

    from updater import ReleaseUpdater
    release_updater = ReleaseUpdater(config, os.path.dirname(config_path), VERSION,
                                     enqueue_gui, update_allowed, install_release)
    threading.Thread(target=release_updater.run, daemon=True).start()
    root.after(1000, report_update_ready)
    logging.info("%s %s started; auto-update enabled: %s", APP_NAME, VERSION, release_updater.enabled)

    # Start periodic GUI timer updates
    root.after(1000, update_timer_gui)

    # Enter blocking Tkinter GUI loop on the main thread
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
