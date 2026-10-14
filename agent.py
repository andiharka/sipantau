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
from control_runtime import SessionClock, utc
import logging
from logging.handlers import RotatingFileHandler
from client_logging import NetworkLog
from version import VERSION
import subprocess
import tkinter as tk
from tkinter import messagebox
from datetime import datetime, timezone, timedelta
import requests
import urllib3
import psutil

# 1. Config Loader
def load_config():
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        dir_path = os.path.dirname(sys.executable)
    else:
        # Running as script
        dir_path = os.path.dirname(os.path.abspath(__file__))
    
    config_path = os.path.join(dir_path, "config.json")
    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)
            return config_data, config_path
    except Exception as e:
        # Default fallback config if file missing
        fallback = {
            "api_key": "",
            "server_url": "",
            "master_password": None,
        }
        return fallback, config_path

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
    except Exception as e:
        logging.error(f"Error saving state to file: {e}")

# Event to trigger heartbeat wakeups immediately upon manual action
heartbeat_event = threading.Event()

def _set_local_control_locked(locked, action):
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
            return
        before = (local_state.get("applied_control_version"), local_state.get("control_error"))
        local_state["attempted_control_version"] = version
        try:
            if local_state["is_locked"]:
                show_lock_screen_gui()
            else:
                hide_lock_screen_gui()
            local_state["applied_control_version"] = version
            local_state["control_error"] = None
        except Exception as error:
            local_state["applied_control_version"] = None
            local_state["control_error"] = re.sub(r'[\x00-\x1f\x7f]', ' ', str(error))[:500]
            logging.error("Could not apply control version %s: %s", version, error)
        changed = before != (local_state.get("applied_control_version"), local_state.get("control_error"))
    if changed:
        save_state()
        heartbeat_event.set()


def apply_local_gui(pending_id):
    with STATE_LOCK:
        pending = local_state["pending_actions"]
        if not pending or pending[0].get("id") != pending_id:
            return
        try:
            if local_state["is_locked"]:
                show_lock_screen_gui()
            else:
                hide_lock_screen_gui()
        except Exception as error:
            local_state["attempted_control_version"] = local_state.get("control_version")
            local_state["control_error"] = re.sub(r'[\x00-\x1f\x7f]', ' ', str(error))[:500]
    save_state()
    heartbeat_event.set()


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
    if os.name != 'nt':
        return
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "SipantauAgent"
        
        # Determine executable path
        if getattr(sys, 'frozen', False):
            # Running as compiled exe
            exe_path = sys.executable
        else:
            # Running as script
            exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, exe_path)
            logging.info(f"Autorun registry entry added: {exe_path}")
        else:
            try:
                winreg.DeleteValue(key, app_name)
                logging.info("Autorun registry entry removed.")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        logging.error(f"Failed to update autorun registry: {str(e)}")

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
def show_lock_screen_gui():
    global lock_window, is_locked, lock_health_error
    with gui_lock:
        if is_locked:
            if lock_health_error:
                raise RuntimeError(lock_health_error)
            return
    keyboard_error = None

    lock_window = tk.Toplevel(root)
    lock_window.configure(bg="black")
    lock_window.attributes("-fullscreen", True)
    lock_window.attributes("-topmost", True)
    lock_window.overrideredirect(True)

    # Prevent closing by taskbar/shortcuts
    lock_window.protocol("WM_DELETE_WINDOW", lambda: "break")

    # Install Windows low-level hook overrides
    if os.name == 'nt' and keyboard:
        try:
            keyboard.block_key('windows')
            keyboard.add_hotkey('alt+tab', lambda: None, suppress=True)
            keyboard.add_hotkey('alt+f4', lambda: None, suppress=True)
            keyboard.add_hotkey('alt+esc', lambda: None, suppress=True)
            keyboard.add_hotkey('ctrl+esc', lambda: None, suppress=True)
            keyboard.add_hotkey('windows+d', lambda: None, suppress=True)
            keyboard.add_hotkey('windows+r', lambda: None, suppress=True)
        except Exception as error:
            keyboard_error = str(error)
    elif os.name == 'nt':
        keyboard_error = 'Keyboard hook dependency is unavailable'

    # Binding events to reject default Tkinter close shortcuts
    lock_window.bind_all("<Alt-F4>", lambda e: "break")
    lock_window.bind_all("<Alt-Tab>", lambda e: "break")

    # Lock focus inside this window
    lock_window.focus_force()
    lock_window.grab_set()

    def re_grab(event=None):
        with gui_lock:
            active = is_locked
        if active and lock_window:
            lock_window.focus_force()
            lock_window.grab_set()

    # Re-apply grab if window loses focus
    lock_window.bind("<FocusOut>", lambda e: lock_window.after(1, re_grab))

    # Visitor instructions and local staff controls share a clear, responsive layout.
    bg = "#101923"
    lock_window.configure(bg=bg)
    frame = tk.Frame(lock_window, bg=bg, padx=36, pady=24)
    frame.place(relx=0.5, rely=0.5, anchor="center")
    wrap = max(280, min(720, lock_window.winfo_screenwidth() - 100))
    tk.Label(frame, text="SIPANTAU  /  " + get_agent_hostname(), font=("Segoe UI", 12),
             fg="#8aa5bb", bg=bg).pack(pady=(0, 18))
    tk.Label(frame, text="Komputer terkunci", font=("Segoe UI", 30, "bold"),
             fg="#f1f5f9", bg=bg).pack()
    tk.Label(frame, text="Hubungi petugas untuk memulai sesi penggunaan.",
             font=("Segoe UI", 14), fg="#becbd6", bg=bg, wraplength=wrap).pack(pady=(10, 20))
    status = tk.Label(frame, font=("Segoe UI", 11), bg=bg)
    status.pack(pady=(0, 22))
    def update_connection_status():
        if not lock_window or not status.winfo_exists():
            return
        connected = LAST_HEARTBEAT is not None and time.monotonic() - LAST_HEARTBEAT < 60
        status.config(text="Terhubung ke server" if connected else
                      "Server tidak terhubung · akses petugas tetap tersedia",
                      fg="#73d4b0" if connected else "#e4bb78")
        status.after(2000, update_connection_status)
    update_connection_status()
    tk.Label(frame, text="Akses petugas", font=("Segoe UI", 13, "bold"),
             fg="#f1f5f9", bg=bg).pack(anchor="w")
    pwd_entry = tk.Entry(frame, show="*", font=("Segoe UI", 14), bg="#213142",
                         fg="white", insertbackground="white", relief="flat", width=28)
    pwd_entry.pack(fill="x", pady=(10, 14), ipady=8)
    tk.Label(frame, text="Durasi sesi (menit)", font=("Segoe UI", 11),
             fg="#becbd6", bg=bg).pack(anchor="w")
    minutes = tk.StringVar(value="60")
    duration_entry = tk.Spinbox(frame, from_=1, to=1440, textvariable=minutes,
                               font=("Segoe UI", 14), width=8, bg="#213142", fg="white",
                               insertbackground="white", relief="flat")
    duration_entry.pack(anchor="w", pady=8)
    presets = tk.Frame(frame, bg=bg)
    presets.pack(anchor="w", pady=(0, 12))
    for value in (30, 60, 120):
        tk.Button(presets, text=f"{value} menit", command=lambda v=value: minutes.set(str(v)),
                  bg="#213142", fg="white", relief="flat", padx=12, pady=5).pack(side="left", padx=(0, 8))
    error_label = tk.Label(frame, text="", font=("Segoe UI", 11), fg="#ffb4b4",
                           bg=bg, wraplength=wrap)
    error_label.pack()
    def handle_access(timed=True):
        try:
            duration = int(minutes.get()) if timed else None
            pending_id = authorize_local_access(pwd_entry.get(), duration)
            pwd_entry.delete(0, tk.END)
            apply_local_gui(pending_id)
        except ValueError as error:
            pwd_entry.delete(0, tk.END)
            error_label.config(text=str(error))
    tk.Button(frame, text="Mulai sesi dengan timer", font=("Segoe UI", 12, "bold"),
              bg="#317d9b", fg="white", activebackground="#24647e", activeforeground="white",
              relief="flat", pady=10, command=handle_access).pack(fill="x", pady=(10, 8))
    tk.Button(frame, text="Buka tanpa batas waktu", font=("Segoe UI", 11),
              bg="#213142", fg="#becbd6", relief="flat", pady=8,
              command=lambda: handle_access(False)).pack(fill="x")
    pwd_entry.bind("<Return>", lambda event: handle_access(True))
    pwd_entry.focus_set()
    with gui_lock:
        is_locked = True
    lock_health_error = 'Lock screen visible but shortcut blocking failed: ' + keyboard_error if keyboard_error else None
    if lock_health_error:
        raise RuntimeError(lock_health_error)

def hide_lock_screen_gui():
    global lock_window, is_locked, lock_health_error
    with gui_lock:
        if not is_locked and not lock_health_error:
            return
    keyboard_error = None

    # Remove low-level Windows keyboard hooks
    if os.name == 'nt' and keyboard:
        try:
            keyboard.unhook_all()
        except Exception as error:
            keyboard_error = str(error)

    if lock_window:
        lock_window.grab_release()
        lock_window.destroy()
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
    x = win.winfo_x() + dx
    y = win.winfo_y() + dy
    win.geometry(f"+{x}+{y}")

# --- Stop session action ---
def stop_session_action():
    if messagebox.askyesno("Hentikan Sesi", "Apakah Anda yakin ingin menghentikan sesi sekarang?\nKomputer Anda akan langsung dikunci."):
        pending_id = set_local_control(True, "force-lock")
        apply_local_gui(pending_id)

# --- Floating countdown timer widget ---
def show_timer_window_gui():
    global timer_window
    if timer_window is not None:
        return
        
    timer_window = tk.Toplevel(root)
    timer_window.configure(bg="#222222")
    timer_window.overrideredirect(True)  # borderless
    timer_window.attributes("-topmost", True)
    
    # Drag-and-drop bindings
    timer_window.bind("<Button-1>", start_drag)
    timer_window.bind("<B1-Motion>", drag_motion)
    
    # Internal variables to track dragging
    timer_window._drag_x = 0
    timer_window._drag_y = 0
    
    # Position widget in the top-right corner of the screen
    screen_width = timer_window.winfo_screenwidth()
    width = 240
    height = 90
    x = screen_width - width - 40
    y = 40
    timer_window.geometry(f"{width}x{height}+{x}+{y}")
    
    # Main container frame
    container = tk.Frame(timer_window, bg="#222222", bd=1, relief="solid", highlightbackground="#3b82f6", highlightcolor="#3b82f6", highlightthickness=1)
    container.pack(fill="both", expand=True)
    container.bind("<Button-1>", start_drag)
    container.bind("<B1-Motion>", drag_motion)
    timer_window.container_frame = container
    
    # Text label for countdown
    time_label = tk.Label(container, text="", font=("Helvetica", 14, "bold"), fg="#3b82f6", bg="#222222")
    time_label.pack(pady=(10, 2))
    time_label.bind("<Button-1>", start_drag)
    time_label.bind("<B1-Motion>", drag_motion)
    timer_window.time_label = time_label
    
    # Warning description label
    desc_label = tk.Label(container, text="", font=("Helvetica", 8), fg="#ef4444", bg="#222222", wraplength=220)
    desc_label.pack(pady=0)
    desc_label.bind("<Button-1>", start_drag)
    desc_label.bind("<B1-Motion>", drag_motion)
    timer_window.desc_label = desc_label
    
    # Stop Session Button
    stop_btn = tk.Button(
        container,
        text="Hentikan Sesi",
        font=("Helvetica", 9, "bold"),
        bg="#dc2626",
        fg="white",
        activebackground="#b91c1c",
        activeforeground="white",
        relief="flat",
        bd=0,
        command=stop_session_action
    )
    stop_btn.pack(pady=(2, 10))
    timer_window.stop_btn = stop_btn

    # Hover detection to support distraction-free mode
    timer_window.mouse_inside = False

    def on_enter(event):
        timer_window.mouse_inside = True
        update_timer_layout()

    def on_leave(event):
        try:
            x, y = timer_window.winfo_pointerxy()
            wx = timer_window.winfo_rootx()
            wy = timer_window.winfo_rooty()
            ww = timer_window.winfo_width()
            wh = timer_window.winfo_height()
            if not (wx <= x < wx + ww and wy <= y < wy + wh):
                timer_window.mouse_inside = False
                update_timer_layout()
        except Exception:
            pass

    # Bind hover listeners to the window and container elements
    for widget in (timer_window, container, time_label, desc_label, stop_btn):
        widget.bind("<Enter>", on_enter, add="+")
        widget.bind("<Leave>", on_leave, add="+")

def update_timer_layout():
    global timer_window
    if not timer_window:
        return
        
    with STATE_LOCK:
        remaining = int(SESSION_CLOCK.remaining() or 0)

    if remaining <= 0:
        if timer_window is not None:
            try:
                timer_window.destroy()
            except Exception:
                pass
            timer_window = None
        return

    mouse_inside = getattr(timer_window, "mouse_inside", False)
    
    if remaining >= 600:
        # Distraction-free mode when remaining time is >= 10 mins
        hrs = remaining // 3600
        mins = (remaining % 3600) // 60
        if hrs > 0:
            time_str = f"Sisa: {hrs} jam {mins} menit"
        else:
            time_str = f"Sisa: {mins} menit"
            
        if mouse_inside:
            # Expanded state on hover
            timer_window.container_frame.config(bg="#222222", highlightbackground="#3b82f6")
            timer_window.time_label.config(text=time_str, fg="#3b82f6", bg="#222222")
            timer_window.desc_label.config(text="", bg="#222222")
            
            # Repack elements to ensure correct layout and spacing
            timer_window.time_label.pack_forget()
            timer_window.desc_label.pack_forget()
            timer_window.stop_btn.pack_forget()
            
            timer_window.time_label.pack(pady=(10, 2))
            timer_window.desc_label.pack(pady=0)
            timer_window.stop_btn.pack(pady=(2, 10))
            
            timer_window.geometry("240x90")
            try:
                timer_window.attributes("-alpha", 0.95)
            except Exception:
                pass
        else:
            # Compact state when idle
            timer_window.container_frame.config(bg="#222222", highlightbackground="#3b82f6")
            timer_window.time_label.config(text=time_str, fg="#3b82f6", bg="#222222")
            timer_window.desc_label.config(text="", bg="#222222")
            
            timer_window.time_label.pack_forget()
            timer_window.desc_label.pack_forget()
            timer_window.stop_btn.pack_forget()
            
            timer_window.time_label.pack(pady=(10, 10)) # centered vertically
            
            timer_window.geometry("180x45")
            try:
                timer_window.attributes("-alpha", 0.65)
            except Exception:
                pass
                
        timer_window.attributes("-topmost", True)
        
    elif remaining >= 180:
        # Warning mode (orange border)
        mins = remaining // 60
        secs = remaining % 60
        time_str = f"Sisa: {mins:02d}:{secs:02d}"
        
        timer_window.container_frame.config(bg="#222222", highlightbackground="#f97316")
        timer_window.time_label.config(text=time_str, fg="#f97316", bg="#222222")
        timer_window.desc_label.config(text="", bg="#222222")
        
        timer_window.time_label.pack_forget()
        timer_window.desc_label.pack_forget()
        timer_window.stop_btn.pack_forget()
        
        timer_window.time_label.pack(pady=(10, 2))
        timer_window.desc_label.pack(pady=0)
        timer_window.stop_btn.pack(pady=(2, 10))
        
        timer_window.geometry("240x90")
        try:
            timer_window.attributes("-alpha", 0.95)
        except Exception:
            pass
        timer_window.attributes("-topmost", True)
        
    else:
        # Critical warning mode (red background)
        mins = remaining // 60
        secs = remaining % 60
        time_str = f"Sisa: {mins:02d}:{secs:02d}"
        alert_text = "PERINGATAN: Sesi hampir habis!\nSegera simpan pekerjaan Anda!"
        
        timer_window.container_frame.config(bg="#450a0a", highlightbackground="#ef4444")
        timer_window.time_label.config(text=time_str, fg="#fca5a5", bg="#450a0a")
        timer_window.desc_label.config(text=alert_text, fg="#ffffff", bg="#450a0a")
        
        timer_window.time_label.pack_forget()
        timer_window.desc_label.pack_forget()
        timer_window.stop_btn.pack_forget()
        
        timer_window.time_label.pack(pady=(10, 2))
        timer_window.desc_label.pack(pady=0)
        timer_window.stop_btn.pack(pady=(2, 10))
        
        timer_window.geometry("240x120")
        try:
            timer_window.attributes("-alpha", 1.0)
        except Exception:
            pass
        timer_window.attributes("-topmost", True)

def update_timer_gui():
    global timer_window
    
    with STATE_LOCK:
        remaining = SESSION_CLOCK.remaining()
        active = not local_state["is_locked"] and remaining is not None

    if active:
        if remaining > 0:
            if timer_window is None:
                show_timer_window_gui()
                
            if timer_window:
                update_timer_layout()
        else:
            if timer_window is not None:
                try:
                    timer_window.destroy()
                except Exception:
                    pass
                timer_window = None
    else:
        if timer_window is not None:
            try:
                timer_window.destroy()
            except Exception:
                pass
            timer_window = None
            
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
                   "control_error": local_state.get("control_error")}
    started = time.monotonic()
    response = requests.post(f"{server_url}/api/pantau/heartbeat", json=payload,
                             headers=headers, timeout=5, verify=verify_ssl)
    if response.status_code not in (200, 201):
        raise RuntimeError(f'Heartbeat failed with HTTP {response.status_code}')
    accept_server_state(response.json(), time.monotonic() - started)


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

def update_allowed():
    with STATE_LOCK:
        with gui_lock:
            return (local_state["is_locked"] and is_locked and not lock_health_error
                    and SESSION_CLOCK.remaining() is None and not local_state["pending_actions"])


def install_release(staged, tag, digest):
    from pathlib import Path
    from windows_runtime import lock_for_update
    from updater import installer_script
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
    from urllib.parse import urlparse
    server = urlparse(config.get("server_url", ""))
    if server.scheme not in ('http', 'https') or not server.hostname or len(config.get("api_key", "")) < 32:
        logging.error('Set a valid server_url and an API key of at least 32 characters in config.json.')
        sys.exit(1)
    # Initialize Tkinter system first on the main thread
    root = tk.Tk()
    root.withdraw() # Hide root controller window

    # Load persistent state from state.json cache
    load_state()

    # Expired cached sessions must not briefly unlock the desktop on startup.
    expire_local_session()
    threading.Thread(target=restore_cached_site_policy, daemon=True).start()

    # Configure autorun registration (Windows only)
    update_autorun(config.get("autorun", False))

    # Enforce cached local state even when an offline action is awaiting sync.
    if config.get("auto_lock_on_run", False):
        set_local_control(True, "force-lock")
    root.after(0, restore_gui_state)

    root.after(50, process_gui_commands)

    # Run the background blacklist process checker
    watcher_thread = threading.Thread(target=blacklist_watcher, daemon=True)
    watcher_thread.start()

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
    logging.info("SIPANTAU %s started; auto-update enabled: %s", VERSION, release_updater.enabled)

    # Start periodic GUI timer updates
    root.after(1000, update_timer_gui)

    # Enter blocking Tkinter GUI loop on the main thread
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
