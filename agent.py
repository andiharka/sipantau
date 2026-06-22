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
from datetime import datetime, timezone
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
            "server_url": "http://localhost:3333",
            "master_password": "secret",
        }
        return fallback, config_path

config, config_path = load_config()
state_path = os.path.join(os.path.dirname(config_path), "state.json")

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

# GUI Global Control State
root = None
lock_window = None
is_locked = False
gui_lock = threading.Lock()
active_notifications = []
notification_lock = threading.Lock()
timer_window = None

# Local Persistent State Management
STATE_LOCK = threading.Lock()
local_state = {
    "is_locked": True,
    "from_when": None,
    "duration": None,
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
                    local_state.update(loaded)
            logging.info("State successfully loaded from file.")
        except Exception as e:
            logging.error(f"Error loading state from file: {e}")
    else:
        save_state()

def save_state():
    try:
        with STATE_LOCK:
            state_copy = dict(local_state)
        temp_path = state_path + ".tmp"
        with open(temp_path, "w") as f:
            json.dump(state_copy, f, indent=2)
        os.replace(temp_path, state_path)
    except Exception as e:
        logging.error(f"Error saving state to file: {e}")

# Event to trigger heartbeat wakeups immediately upon manual action
heartbeat_event = threading.Event()

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

def sync_hosts_file(blocked_sites):
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
            global local_state
            with STATE_LOCK:
                local_state["is_locked"] = False
                local_state["from_when"] = None
                local_state["duration"] = None
                if "force-unlock" not in local_state["pending_actions"]:
                    local_state["pending_actions"].append("force-unlock")
            save_state()
            
            hide_lock_screen_gui()
            heartbeat_event.set()
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
        global local_state
        with STATE_LOCK:
            local_state["is_locked"] = True
            local_state["from_when"] = None
            local_state["duration"] = None
            if "force-lock" not in local_state["pending_actions"]:
                local_state["pending_actions"].append("force-lock")
        save_state()
        
        show_lock_screen_gui()
        heartbeat_event.set()

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
        is_locked_local = local_state["is_locked"]
        from_when_local = local_state["from_when"]
        duration_local = local_state["duration"]
        
    remaining = 0
    if not is_locked_local and from_when_local and duration_local:
        try:
            cleaned_time = from_when_local.replace("Z", "+00:00")
            started_at = datetime.fromisoformat(cleaned_time)
            now_utc = datetime.now(timezone.utc)
            elapsed = (now_utc - started_at).total_seconds()
            remaining = int(duration_local - elapsed)
        except Exception:
            pass
            
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
        is_locked_local = local_state["is_locked"]
        from_when_local = local_state["from_when"]
        duration_local = local_state["duration"]
        
    if not is_locked_local and from_when_local and duration_local:
        try:
            cleaned_time = from_when_local.replace("Z", "+00:00")
            started_at = datetime.fromisoformat(cleaned_time)
            now_utc = datetime.now(timezone.utc)
            elapsed = (now_utc - started_at).total_seconds()
            remaining = int(duration_local - elapsed)
        except Exception:
            remaining = 0
            
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
                                root.after(0, lambda n=name: trigger_kill_notification(n))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except Exception as e:
            # Prevent thread termination
            pass
        time.sleep(15)

# 9. Background Heartbeat Thread
def heartbeat_loop():
    global local_state
    server_url = config.get("server_url", "")
    api_key = config.get("api_key", "")
    verify_ssl = config.get("verify_ssl", True)
    
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    while True:
        try:
            if not server_url:
                time.sleep(5)
                continue
            
            # Flush pending actions first
            with STATE_LOCK:
                actions = list(local_state.get("pending_actions", []))
            
            success_actions = []
            failed = False
            for act in actions:
                url_act = f"{server_url.rstrip('/')}/api/pantau/action/{act}"
                payload_act = {
                    "hostname": get_agent_hostname(),
                    "ip": get_local_ip()
                }
                try:
                    res = requests.post(url_act, json=payload_act, headers=headers, timeout=5, verify=verify_ssl)
                    if res.status_code in (200, 201):
                        success_actions.append(act)
                        logging.info(f"Successfully posted pending action: {act}")
                    else:
                        logging.error(f"Failed to post action {act}: status code {res.status_code}")
                        failed = True
                        break
                except Exception as ex:
                    logging.error(f"Connection error posting action {act}: {ex}")
                    failed = True
                    break
            
            if success_actions:
                with STATE_LOCK:
                    for act in success_actions:
                        if act in local_state["pending_actions"]:
                            local_state["pending_actions"].remove(act)
                save_state()
            
            if failed:
                # Sleep or wait event before retrying pending actions
                heartbeat_event.wait(5)
                heartbeat_event.clear()
                continue
            
            # Perform normal heartbeat
            url_hb = f"{server_url.rstrip('/')}/api/pantau/heartbeat"
            payload_hb = {
                "hostname": get_agent_hostname(),
                "ip": get_local_ip(),
                "running_apps": get_running_apps()
            }
            
            res = requests.post(url_hb, json=payload_hb, headers=headers, timeout=5, verify=verify_ssl)
            if res.status_code in (200, 201):
                data = res.json()
                is_locked_srv = data.get("is_locked", True)
                from_when_srv = data.get("from_when")
                duration_srv = data.get("duration")
                
                with STATE_LOCK:
                    local_state["is_locked"] = is_locked_srv
                    local_state["from_when"] = from_when_srv
                    local_state["duration"] = duration_srv
                save_state()
                
                if is_locked_srv:
                    root.after(0, show_lock_screen_gui)
                else:
                    root.after(0, hide_lock_screen_gui)
            else:
                logging.error(f"Heartbeat failed with code {res.status_code}: {res.text}")
                
        except Exception as e:
            logging.error(f"Heartbeat exception: {e}")
            with STATE_LOCK:
                offline_locked = local_state["is_locked"]
            if offline_locked:
                root.after(0, show_lock_screen_gui)
            else:
                root.after(0, hide_lock_screen_gui)
                
        # Wait 5 seconds or wait until manual action sets event
        heartbeat_event.wait(5)
        heartbeat_event.clear()

# 10. Background Blocklist Sync Thread
def blocklist_sync_loop():
    global local_state
    server_url = config.get("server_url", "")
    api_key = config.get("api_key", "")
    verify_ssl = config.get("verify_ssl", True)
    
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    while True:
        try:
            if not server_url:
                time.sleep(10)
                continue
            
            url = f"{server_url.rstrip('/')}/api/pantau/blocklist"
            res = requests.get(url, headers=headers, timeout=5, verify=verify_ssl)
            if res.status_code == 200:
                data = res.json()
                banned_apps = data.get("blocked_apps") or data.get("blacklisted_apps", [])
                blocked_sites = data.get("blocked_sites", [])
                
                with STATE_LOCK:
                    local_state["blocked_apps"] = banned_apps
                    local_state["blocked_sites"] = blocked_sites
                save_state()
                
                # Sync sites list to local hosts mapping
                sync_hosts_file(blocked_sites)
            else:
                logging.error(f"Blocklist fetch failed with code {res.status_code}")
        except Exception as e:
            logging.error(f"Blocklist sync exception: {e}")
            
        time.sleep(180)

# 11. Local Session Countdown Timer Thread
def session_timer_loop():
    global local_state
    while True:
        try:
            with STATE_LOCK:
                is_locked_local = local_state["is_locked"]
                from_when_local = local_state["from_when"]
                duration_local = local_state["duration"]
                
            if not is_locked_local and from_when_local and duration_local:
                try:
                    cleaned_time = from_when_local.replace("Z", "+00:00")
                    started_at = datetime.fromisoformat(cleaned_time)
                    now_utc = datetime.now(timezone.utc)
                    elapsed = (now_utc - started_at).total_seconds()
                    remaining = duration_local - elapsed
                    
                    if remaining <= 0:
                        logging.info("Local session countdown expired. Locking PC.")
                        with STATE_LOCK:
                            local_state["is_locked"] = True
                            local_state["from_when"] = None
                            local_state["duration"] = None
                            if "force-lock" not in local_state["pending_actions"]:
                                local_state["pending_actions"].append("force-lock")
                        save_state()
                        
                        root.after(0, show_lock_screen_gui)
                        heartbeat_event.set()
                except Exception as ex:
                    logging.error(f"Error parsing session start time: {ex}")
        except Exception as e:
            logging.error(f"Error in session timer loop: {e}")
            
        time.sleep(1)

# Main Application Entrypoint
if __name__ == "__main__":
    # Initialize Tkinter system first on the main thread
    root = tk.Tk()
    root.withdraw() # Hide root controller window

    # Load persistent state from state.json cache
    load_state()

    # Configure autorun registration (Windows only)
    update_autorun(config.get("autorun", False))

    # Auto-lock or restore state on run
    with STATE_LOCK:
        start_locked = local_state["is_locked"]
    if start_locked or config.get("auto_lock_on_run", False):
        root.after(0, show_lock_screen_gui)

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

    # Start periodic GUI timer updates
    root.after(1000, update_timer_gui)

    # Enter blocking Tkinter GUI loop on the main thread
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
