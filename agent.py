import os
import sys
import json
import time
import socket
import threading
import logging
import subprocess
import tkinter as tk
from functools import wraps
from flask import Flask, request, jsonify
import requests
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
            "time_limit_hours": 3,
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

# 6. Hosts Blocking Operations
def block_domain(domain):
    domain = domain.strip().lower()
    entries = [f"127.0.0.1 {domain}", f"127.0.0.1 www.{domain}"]
    
    try:
        with open(hosts_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
        
    lines = content.splitlines()
    updated = False
    
    for entry in entries:
        exists = False
        for line in lines:
            line_stripped = line.strip().lower()
            if not line_stripped.startswith("#") and entry in line_stripped:
                exists = True
                break
        if not exists:
            lines.append(f"{entry} # agent-blocked")
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
        text="Session time limit reached. Please ask the staff to continue.",
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
        text="Enter password to unlock:",
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
            hide_lock_screen_gui()
        else:
            pwd_entry.delete(0, tk.END)
            error_label.config(text="Incorrect master password.")
            lock_window.after(3000, lambda: error_label.config(text=""))

    pwd_entry.bind("<Return>", handle_pwd_unlock)

    btn = tk.Button(
        pwd_frame,
        text="Unlock",
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
    root.after(0, show_lock_screen_gui)
    return jsonify({"status": "locked"})

@app.route("/unlock", methods=["POST"])
@require_api_key
def unlock_endpoint():
    root.after(0, hide_lock_screen_gui)
    return jsonify({"status": "unlocked"})

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
        
    status_data = {
        "hostname": socket.gethostname(),
        "ip": get_local_ip(),
        "running_apps": get_running_apps(),
        "blocked_sites": get_blocked_sites(),
        "is_locked": locked_state
    }
    return jsonify(status_data)

@app.route("/kill-app", methods=["POST"])
@require_api_key
def kill_app():
    data = request.get_json(force=True, silent=True) or {}
    exe = data.get("exe")
    if not exe:
        return jsonify({"error": "Missing exe parameter"}), 400
        
    if os.name == 'nt':
        subprocess.run(["taskkill", "/IM", exe, "/F"], shell=True, capture_output=True)
    else:
        # Fallback to kill local mock processes via psutil on non-Windows
        for proc in psutil.process_iter(['name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == exe.lower():
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

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
                
                payload = {
                    "hostname": socket.gethostname(),
                    "ip": get_local_ip(),
                    "is_locked": locked_state,
                    "running_apps": get_running_apps(),
                    "blacklisted_apps": config.get("blacklisted_apps", [])
                }
                
                api_key = config.get("api_key", "")
                headers = {
                    "X-API-Key": api_key,
                    "Content-Type": "application/json"
                }
                
                url = f"{server_url.rstrip('/')}/api/pantau/heartbeat"
                requests.post(url, json=payload, headers=headers, timeout=5)
        except Exception:
            # Silently catch unreachable server exceptions and retry next iteration
            pass
        time.sleep(10)

# Main Application Entrypoint
if __name__ == "__main__":
    # Initialize Tkinter system first on the main thread
    root = tk.Tk()
    root.withdraw() # Hide root controller window

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

    # Enter blocking Tkinter GUI loop on the main thread
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
