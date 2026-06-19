import os
import sys
import json
import time
import socket
import threading
import logging
import subprocess
import tkinter as tk
from tkinter import messagebox
from functools import wraps
from flask import Flask, request, jsonify
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
            "api_key": "changeme",
            "server_url": "http://192.168.1.1:3333",
            "blacklisted_apps": ["steam.exe", "discord.exe"],
            "time_limit_seconds": 10800,
            "master_password": "secret"
        }
        return fallback, config_path

config, config_path = load_config()

# 2. Setup Logging
log_path = os.path.join(os.path.dirname(config_path), "agent.log")
logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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

# Flask App Initialisation
app = Flask(__name__)

# GUI Global Control State
root = None
lock_window = None
is_locked = False
gui_lock = threading.Lock()
active_notifications = []
notification_lock = threading.Lock()

# Local Session Control State
session_remaining_seconds = 0
session_active = False
session_lock = threading.Lock()
timer_window = None

# 4. Authentication Decorator
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key")
        if not api_key or api_key != config.get("api_key"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

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
                if parts[1] in targets:
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

# 7. GUI Kiosk Screen Operations (Must be executed on main Tkinter thread)
def show_lock_screen_gui():
    global lock_window, is_locked
    with gui_lock:
        if is_locked:
            return
        is_locked = True

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
        except Exception:
            pass

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

    # Kiosk interface elements
    frame = tk.Frame(lock_window, bg="black")
    frame.place(relx=0.5, rely=0.5, anchor="center")

    label = tk.Label(
        frame, 
        text="Komputer Terkunci. Silahkan hubungi petugas untuk menggunakan komputer.",
        font=("Helvetica", 24, "bold"),
        fg="white",
        bg="black",
        wraplength=800
    )
    label.pack(pady=30)

    pwd_frame = tk.Frame(frame, bg="black")
    pwd_frame.pack(pady=10)

    pwd_label = tk.Label(
        pwd_frame,
        text="Masukkan Master Password :",
        font=("Helvetica", 14),
        fg="#aaaaaa",
        bg="black"
    )
    pwd_label.pack(side="left", padx=5)

    pwd_entry = tk.Entry(
        pwd_frame,
        show="*",
        font=("Helvetica", 14),
        bg="#222222",
        fg="white",
        insertbackground="white",
        bd=1,
        relief="flat"
    )
    pwd_entry.pack(side="left", padx=5)

    error_label = tk.Label(
        frame,
        text="",
        font=("Helvetica", 12),
        fg="red",
        bg="black"
    )
    error_label.pack(pady=10)

    def handle_pwd_unlock(event=None):
        entered = pwd_entry.get()
        if entered == config.get("master_password"):
            global session_remaining_seconds, session_active
            with session_lock:
                session_active = False
                session_remaining_seconds = 0
            hide_lock_screen_gui()
        else:
            pwd_entry.delete(0, tk.END)
            error_label.config(text="Master Password Salah.")
            lock_window.after(3000, lambda: error_label.config(text=""))

    pwd_entry.bind("<Return>", handle_pwd_unlock)

    btn = tk.Button(
        pwd_frame,
        text="Buka Kunci",
        font=("Helvetica", 12, "bold"),
        bg="#444444",
        fg="white",
        activebackground="#555555",
        activeforeground="white",
        relief="flat",
        command=handle_pwd_unlock
    )
    btn.pack(side="left", padx=10)

    pwd_entry.focus_set()

def hide_lock_screen_gui():
    global lock_window, is_locked
    with gui_lock:
        if not is_locked:
            return
        is_locked = False

    # Remove low-level Windows keyboard hooks
    if os.name == 'nt' and keyboard:
        try:
            keyboard.unhook_all()
        except Exception:
            pass

    if lock_window:
        lock_window.grab_release()
        lock_window.destroy()
        lock_window = None

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
        global session_active, session_remaining_seconds
        with session_lock:
            session_active = False
            session_remaining_seconds = 0
        show_lock_screen_gui()

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

def update_timer_gui():
    global timer_window, session_active, session_remaining_seconds, is_locked
    
    with session_lock:
        active = session_active
        remaining = session_remaining_seconds
        
    with gui_lock:
        locked = is_locked
        
    if active and not locked:
        if timer_window is None:
            show_timer_window_gui()
            
        if timer_window:
            if remaining >= 600:
                hrs = remaining // 3600
                mins = (remaining % 3600) // 60
                if hrs > 0:
                    time_str = f"Sisa: {hrs} jam {mins} menit"
                else:
                    time_str = f"Sisa: {mins} menit"
                
                timer_window.container_frame.config(bg="#222222", highlightbackground="#3b82f6")
                timer_window.time_label.config(text=time_str, fg="#3b82f6", bg="#222222")
                timer_window.desc_label.config(text="", bg="#222222")
                timer_window.geometry("240x90")
                timer_window.attributes("-topmost", True)
            
            elif remaining >= 180:
                mins = remaining // 60
                secs = remaining % 60
                time_str = f"Sisa: {mins:02d}:{secs:02d}"
                
                timer_window.container_frame.config(bg="#222222", highlightbackground="#f97316")
                timer_window.time_label.config(text=time_str, fg="#f97316", bg="#222222")
                timer_window.desc_label.config(text="", bg="#222222")
                timer_window.geometry("240x90")
                timer_window.attributes("-topmost", True)
                
            else:
                mins = remaining // 60
                secs = remaining % 60
                time_str = f"Sisa: {mins:02d}:{secs:02d}"
                alert_text = "PERINGATAN: Sesi hampir habis!\nSegera simpan pekerjaan Anda!"
                
                timer_window.container_frame.config(bg="#450a0a", highlightbackground="#ef4444")
                timer_window.time_label.config(text=time_str, fg="#fca5a5", bg="#450a0a")
                timer_window.desc_label.config(text=alert_text, fg="#ffffff", bg="#450a0a")
                timer_window.geometry("240x120")
                timer_window.attributes("-topmost", True)
                
    else:
        if timer_window is not None:
            try:
                timer_window.destroy()
            except Exception:
                pass
            timer_window = None
            
    if root:
        root.after(1000, update_timer_gui)

# 8. API Endpoints
@app.route("/sync-blacklist", methods=["POST"])
@require_api_key
def sync_blacklist():
    data = request.get_json(force=True, silent=True) or {}
    blacklisted_apps = data.get("blacklisted_apps")
    if blacklisted_apps is None or not isinstance(blacklisted_apps, list):
        return jsonify({"error": "Missing or invalid blacklisted_apps parameter"}), 400

    config["blacklisted_apps"] = blacklisted_apps
    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        return jsonify({"error": f"Failed to save config to disk: {str(e)}"}), 500

    return jsonify({"status": "synchronized", "blacklisted_apps": blacklisted_apps})

@app.route("/lock", methods=["POST"])
@require_api_key
def lock_endpoint():
    global session_remaining_seconds, session_active
    with session_lock:
        session_active = False
        session_remaining_seconds = 0
    root.after(0, show_lock_screen_gui)
    return jsonify({"status": "locked"})

@app.route("/unlock", methods=["POST"])
@require_api_key
def unlock_endpoint():
    global session_remaining_seconds, session_active
    data = request.get_json(force=True, silent=True) or {}
    time_limit = data.get("time_limit_seconds")
    
    with session_lock:
        if time_limit is not None:
            try:
                session_remaining_seconds = int(time_limit)
                session_active = True
                logging.info(f"Session unlocked with local timer set to {session_remaining_seconds} seconds")
            except ValueError:
                logging.error(f"Invalid time_limit_seconds received: {time_limit}")
        else:
            session_remaining_seconds = 0
            session_active = False
            logging.info("Session unlocked without local timer (untimed)")

    root.after(0, hide_lock_screen_gui)
    return jsonify({
        "status": "unlocked",
        "session_active": session_active,
        "session_remaining_seconds": session_remaining_seconds
    })

@app.route("/update-session-time", methods=["POST"])
@require_api_key
def update_session_time():
    global session_remaining_seconds, session_active
    data = request.get_json(force=True, silent=True) or {}
    time_limit = data.get("remaining_seconds")
    
    if time_limit is None:
        return jsonify({"error": "Missing remaining_seconds parameter"}), 400
        
    with session_lock:
        try:
            session_remaining_seconds = int(time_limit)
            session_active = True
            logging.info(f"Session timer updated to {session_remaining_seconds} seconds")
        except ValueError:
            return jsonify({"error": "Invalid remaining_seconds parameter"}), 400
            
    return jsonify({
        "status": "updated",
        "session_active": session_active,
        "session_remaining_seconds": session_remaining_seconds
    })

@app.route("/block-site", methods=["POST"])
@require_api_key
def block_site():
    data = request.get_json(force=True, silent=True) or {}
    domain = data.get("domain")
    if not domain:
        return jsonify({"error": "Missing domain parameter"}), 400
    
    block_domain(domain)
    return jsonify({"status": "blocked", "domain": domain})

@app.route("/unblock-site", methods=["POST"])
@require_api_key
def unblock_site():
    data = request.get_json(force=True, silent=True) or {}
    domain = data.get("domain")
    if not domain:
        return jsonify({"error": "Missing domain parameter"}), 400
    
    unblock_domain(domain)
    return jsonify({"status": "unblocked", "domain": domain})

@app.route("/status", methods=["GET"])
@require_api_key
def status_endpoint():
    with gui_lock:
        locked_state = is_locked
    with session_lock:
        rem_seconds = session_remaining_seconds
        active_state = session_active
        
    status_data = {
        "hostname": get_agent_hostname(),
        "ip": get_local_ip(),
        "running_apps": get_running_apps(),
        "blocked_sites": get_blocked_sites(),
        "is_locked": locked_state,
        "session_remaining_seconds": rem_seconds,
        "session_active": active_state
    }
    return jsonify(status_data)

@app.route("/kill-app", methods=["POST"])
@require_api_key
def kill_app():
    data = request.get_json(force=True, silent=True) or {}
    exe = data.get("exe")
    if not exe:
        return jsonify({"error": "Missing exe parameter"}), 400
        
    killed = False
    if os.name == 'nt':
        res = subprocess.run(["taskkill", "/IM", exe, "/F"], shell=True, capture_output=True)
        if res.returncode == 0:
            killed = True
    else:
        # Fallback to kill local mock processes via psutil on non-Windows
        for proc in psutil.process_iter(['name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == exe.lower():
                    proc.kill()
                    killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    if killed and root:
        root.after(0, lambda: trigger_kill_notification(exe))

    return jsonify({"status": "killed", "exe": exe})

# 9. Background Process Blacklist Watcher Thread
def blacklist_watcher():
    while True:
        try:
            blacklisted = [app.lower() for app in config.get("blacklisted_apps", [])]
            if blacklisted:
                for proc in psutil.process_iter(['name']):
                    try:
                        name = proc.info['name']
                        if name and name.lower() in blacklisted:
                            proc.kill()
                            msg = f"Killed blacklisted app: {name}"
                            logging.info(msg)
                            print(msg)
                            if root:
                                root.after(0, lambda n=name: trigger_kill_notification(n))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except Exception as e:
            # Prevent thread termination
            pass
        time.sleep(15)

# 10. Background Heartbeat Thread
def heartbeat_loop():
    while True:
        try:
            server_url = config.get("server_url", "")
            if server_url:
                with gui_lock:
                    locked_state = is_locked
                with session_lock:
                    rem_seconds = session_remaining_seconds
                    active_state = session_active
                
                payload = {
                    "hostname": get_agent_hostname(),
                    "ip": get_local_ip(),
                    "is_locked": locked_state,
                    "running_apps": get_running_apps(),
                    "blacklisted_apps": config.get("blacklisted_apps", []),
                    "session_remaining_seconds": rem_seconds,
                    "session_active": active_state
                }
                
                api_key = config.get("api_key", "")
                headers = {
                    "X-API-Key": api_key,
                    "Content-Type": "application/json"
                }
                
                url = f"{server_url.rstrip('/')}/api/pantau/heartbeat"
                
                verify_ssl = config.get("verify_ssl", True)
                if not verify_ssl:
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                
                response = requests.post(url, json=payload, headers=headers, timeout=5, verify=verify_ssl)
                if response.status_code not in (200, 201):
                    msg = f"Heartbeat failed with status code {response.status_code}: {response.text}"
                    logging.error(msg)
                    print(msg)
        except Exception as e:
            msg = f"Heartbeat exception: {str(e)}"
            logging.error(msg)
            print(msg)
        time.sleep(10)

# 11. Local Session Countdown Timer Thread
def session_timer_loop():
    global session_remaining_seconds, session_active
    while True:
        try:
            with session_lock:
                if session_active:
                    if session_remaining_seconds > 0:
                        session_remaining_seconds -= 1
                    else:
                        session_active = False
                        logging.info("Local session countdown reached 0. Triggering self-lock.")
                        if root:
                            root.after(0, show_lock_screen_gui)
        except Exception as e:
            logging.error(f"Error in session timer loop: {str(e)}")
        time.sleep(1)

# Main Application Entrypoint
if __name__ == "__main__":
    # Initialize Tkinter system first on the main thread
    root = tk.Tk()
    root.withdraw() # Hide root controller window

    # Configure autorun registration (Windows only)
    update_autorun(config.get("autorun", False))

    # Auto-lock the PC on run if configured
    if config.get("auto_lock_on_run", False):
        root.after(0, show_lock_screen_gui)

    # Run Flask API server in a background daemon thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.get("port", 5000), debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()

    # Run the background blacklist process checker
    watcher_thread = threading.Thread(target=blacklist_watcher, daemon=True)
    watcher_thread.start()

    # Run the client status heartbeat loop
    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Run the local session timer loop
    timer_thread = threading.Thread(target=session_timer_loop, daemon=True)
    timer_thread.start()

    # Start periodic GUI timer updates
    root.after(1000, update_timer_gui)

    # Enter blocking Tkinter GUI loop on the main thread
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
