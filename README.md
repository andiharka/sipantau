# Windows 10 School Computer Lab Agent

This is a Python-based administration agent designed to run on Windows 10 client PCs in a school computer lab. The agent is controlled remotely via an AdonisJS server over HTTP and requires administrative privileges.

## Features

- **Authenticated endpoints:** Standardized endpoint control secured via `X-API-Key` checking.
- **Kiosk-style lock screen:** Fullscreen black window blocking Windows hotkeys (Win Key, Alt+Tab, Alt+F4) and requiring a master password or API command to dismiss.
- **Hosts-based website blocking:** Add or remove redirection entries for specified websites, accompanied by local DNS cache flushes.
- **Process blacklisting:** Background thread terminating game or distracting processes (e.g. Steam, Discord) automatically every 15 seconds, logging closures to `agent.log`.
- **Status reporting & heartbeat:** Heartbeat pinging system telemetry to the server every 10 seconds.

---

## Configuration

Place `config.json` in the same directory as `agent.py` (or the compiled `.exe`).

```json
{
  "api_key": "changeme",
  "server_url": "http://192.168.1.1:3333",
  "blacklisted_apps": ["steam.exe", "discord.exe"],
  "time_limit_hours": 3,
  "master_password": "secret"
}
```

---

## Installation & Setup

1. **Install Dependencies:**
   Ensure Python 3.9+ is installed, then run:
   ```cmd
   pip install -r requirements.txt
   ```

2. **Run the Agent:**
   Open a Command Prompt or PowerShell terminal **as Administrator** and execute:
   ```cmd
   python agent.py
   ```

---

## Compiling to Standalone Executable (.exe)

To package the agent into a single executable that requests administrator permissions automatically:

1. **Install PyInstaller:**
   ```cmd
   pip install pyinstaller
   ```

2. **Compile with PyInstaller:**
   Use the following command to generate a windowed/no-console executable requesting UAC elevation on launch:
   ```cmd
   pyinstaller --onefile --noconsole --uac-admin agent.py
   ```
   - `--onefile`: Bundles the script and packages into one `.exe`.
   - `--noconsole`: Hides the command line window (necessary for background/kiosk operation).
   - `--uac-admin`: Configures the manifest to prompt for Administrator permissions when opened on Windows.

3. **Deploy:**
   Move the compiled `agent.exe` from the `dist` directory and place `config.json` next to it.

---

## Security Tuning (Ctrl+Alt+Del & System Shortcuts)

Windows 10 protects the `Ctrl+Alt+Del` Secure Attention Sequence (SAS) at the kernel level. To prevent students from accessing the Task Manager, switching users, or logging off when locked:

### Option 1: Using Windows Group Policy (GPO)
1. Press `Win + R`, type `gpedit.msc`, and hit Enter.
2. Go to: **User Configuration** -> **Administrative Templates** -> **System** -> **Ctrl+Alt+Del Options**.
3. Enable and set the following to prevent bypass:
   - **Remove Task Manager**: Enabled (prevents terminating the lock screen agent).
   - **Remove Lock Computer**: Enabled.
   - **Remove Change Password**: Enabled.
   - **Remove Logoff**: Enabled.

### Option 2: Using the Windows Registry (.reg)
Alternatively, run these Registry commands as Administrator:
```cmd
:: Disable Task Manager
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System" /v DisableTaskMgr /t REG_DWORD /d 1 /f

:: Disable Lock Computer
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System" /v DisableLockWorkstation /t REG_DWORD /d 1 /f
```

---

## API Documentation

All commands (except GET requests) require JSON bodies and the `X-API-Key` HTTP header.

### 1. Lock Screen
* **URL:** `POST /lock`
* **Response:**
  ```json
  { "status": "locked" }
  ```

### 2. Unlock Screen
* **URL:** `POST /unlock`
* **Response:**
  ```json
  { "status": "unlocked" }
  ```

### 3. Block Website
* **URL:** `POST /block-site`
* **Request Body:**
  ```json
  { "domain": "tiktok.com" }
  ```
* **Response:**
  ```json
  { "status": "blocked", "domain": "tiktok.com" }
  ```

### 4. Unblock Website
* **URL:** `POST /unblock-site`
* **Request Body:**
  ```json
  { "domain": "tiktok.com" }
  ```
* **Response:**
  ```json
  { "status": "unblocked", "domain": "tiktok.com" }
  ```

### 5. Check Agent Status
* **URL:** `GET /status`
* **Response:**
  ```json
  {
    "hostname": "STUDENT-PC01",
    "ip": "192.168.1.105",
    "is_locked": false,
    "running_apps": ["chrome.exe", "explorer.exe", "svchost.exe"],
    "blocked_sites": ["tiktok.com"]
  }
  ```

### 6. Force Kill App
* **URL:** `POST /kill-app`
* **Request Body:**
  ```json
  { "exe": "chrome.exe" }
  ```
* **Response:**
  ```json
  { "status": "killed", "exe": "chrome.exe" }
  ```
