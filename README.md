# SIPANTAU

This is a Python-based administration agent designed to run on Windows 10 client PCs in a public computer. The agent is controlled remotely via an AdonisJS server over HTTP and requires administrative privileges.

## Features

- **Authenticated agents:** Shared bootstrap keys and individual PC keys are checked by Perpusip; revoked PC credentials cannot sync.
- **Server polling:** The agent polls the Perpusip server for lock state and block rules.
- **Kiosk-style lock screen:** Fullscreen lock window blocking Windows hotkeys (Win Key, Alt+Tab, Alt+F4) and requiring a master password or API command to dismiss.
- **Hosts-based website blocking:** Add or remove redirection entries for specified websites, accompanied by local DNS cache flushes.
- **Process blacklisting:** Background thread terminating game or distracting processes (e.g. Steam, Discord) automatically every 15 seconds, logging closures to `agent.log`.
- **Status reporting & heartbeat:** The agent sends system telemetry and reads lock state about every 5 seconds.

---

## Configuration

Place `config.json` beside the complete Python source checkout (or the compiled `.exe`). Keep the helper modules `control_runtime.py`, `client_logging.py`, `updater.py`, `windows_runtime.py`, and `version.py` alongside `agent.py` when running from source. Obtain a shared bootstrap key or an individual PC key from the Perpusip administrator. The API key must contain at least 32 characters.

```json
{
  "api_key": "paste_the_generated_agent_key_here",
  "server_url": "http://192.168.1.1:3333",
  "master_password": "set_a_private_local_password",
  "verify_ssl": true,
  "auto_update": false,
  "autorun": false,
  "auto_lock_on_run": false,
  "hostname": "STUDENT-PC01"
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
   pyinstaller --onefile --noconsole --uac-admin --name SIPANTAU agent.py
   ```
   - `--onefile`: Bundles the script and packages into one `.exe`.
   - `--noconsole`: Hides the command line window (necessary for background/kiosk operation).
   - `--uac-admin`: Configures the manifest to prompt for Administrator permissions when opened on Windows.

3. **Deploy:**
   Move the compiled `SIPANTAU.exe` from the `dist` directory and place `config.json` next to it.

---

## Security Tuning (Ctrl+Alt+Del & System Shortcuts)

Windows 10 protects the `Ctrl+Alt+Del` Secure Attention Sequence (SAS) at the kernel level. To prevent students from accessing the Task Manager, switching users, or logging off when locked:

### Option 1: Using Windows Group Policy (GPO)
1. Press `Win + R`, type `gpedit.msc`, and hit Enter.
2. Go to: **User Configuration** -> **Administrative Templates** -> **System** -> **Ctrl+Alt+Del Options**.
3. Enable and set the following to prevent bypass:
   - **Remove Task Manager**: Enabled (prevents terminating the lock screen agent).
   - **Remove Lock Computer**: Leave disabled/not configured when using auto-updates, which require native Windows locking.
   - **Remove Change Password**: Enabled.
   - **Remove Logoff**: Enabled.

### Option 2: Using the Windows Registry (.reg)
Alternatively, run these Registry commands as Administrator:
```cmd
:: Disable Task Manager
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System" /v DisableTaskMgr /t REG_DWORD /d 1 /f

:: Permit the native Windows lock needed for protected auto-updates
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Policies\System" /v DisableLockWorkstation /t REG_DWORD /d 0 /f
```

---

## Server protocol

Apply the Perpusip control-version, online-lookup, and `1791936000000_add_pantau_reports_credentials_member_snapshot` migrations to its `perpusip` database and deploy the updated server before this agent. The server validates `X-API-Key`; the agent also sends `X-Pantau-Hostname` to bind individual credentials to the configured PC. Use HTTPS and leave `verify_ssl` enabled in production.

- `POST /api/pantau/heartbeat` sends `hostname`, `ip`, and `running_apps`. The request also reports `reported_is_locked`, `applied_control_version`, `attempted_control_version`, and `control_error`. The response supplies `is_locked`, `from_when`, `duration`, `control_version`, `session_id`, `expires_at`, and `server_now`. A successful GUI operation produces an acknowledgment on the next heartbeat; a failed operation reports its error.
- `GET /api/pantau/blocklist` supplies `blacklisted_apps` and `blocked_sites`.
- `POST /api/pantau/action/force-lock` and `POST /api/pantau/action/force-unlock` send `hostname`, `ip`, and the last observed `expected_version`. A successful response supplies the next `control_version`.
- The server responds with `409` when staff or session expiry has made a newer control decision. The agent discards that local action and applies the state from its next heartbeat.

Local actions made before the agent has received a server control version cannot be replayed safely. The agent discards them and follows the server on its next heartbeat.

## Countdown and verification

The running countdown uses a monotonic deadline synchronized with the server, so PC clock corrections do not extend or prematurely expire the session. The GUI display and local expiry share that deadline. Cached UTC expiry is reconstructed on restart and corrected on reconnection. Control callbacks check their version or local action ID before acting on a replacement session.

Generate or rotate an individual key through **Kunci Agen** on the Perpusip PC card, copy the returned key into `api_key`, and restart this agent. Revoked credentials require a newly issued key. Set `PANTAU_ALLOW_SHARED_KEY=false` on Perpusip after every PC has been provisioned individually.

Run `python -m unittest discover -s tests -v` for protocol and countdown tests. These mock the GUI; verify lock-window behavior and Windows shortcuts on a PC with administrator privileges.


## Local staff access and offline behavior

On the lock screen, staff can enter the configured `master_password` and choose:

- **Mulai sesi dengan timer:** 1–1440 minutes, with 30/60/120 minute presets. Enter starts this mode by default. The PC unlocks immediately and relocks when time runs out, even if Perpusip and the internet are unreachable.
- **Buka tanpa batas waktu:** unlock until a staff/server lock command or the configured startup lock policy. This still requires the password.

The floating countdown and **Hentikan Sesi** work locally. Cached application rules continue to be enforced. Cached website rules are reapplied at startup independently of network requests; changes remove only `# agent-blocked` entries. The initial cache can only contain rules previously received from Perpusip. Without a cache there are no rules to enforce, and the default local lock remains available.

New internet connectivity is not required for Perpusip control when the LAN server remains reachable. With the server unreachable, new remote commands, rule changes and member lookups wait for connectivity. The lock screen shows server connection status, not general internet status.

`POST /api/pantau/action/start-session` sends `hostname`, `ip`, `expected_version`, `remaining_seconds`, and the original timezone-aware `expires_at`. Perpusip adopts at most the remaining time and original deadline. Expired sessions stay locked. A newer server control version supersedes the queued local action. Before the first server heartbeat, local access is usable but cannot be replayed safely on reconnect; the server state takes precedence.

Running countdowns use monotonic time. Restarting offline reconstructs the deadline from the persisted UTC expiry and last server clock offset; changing the system clock across a restart can affect this estimate. `auto_lock_on_run=true` always locks on startup, even with an unexpired cached session. Damaged state files fail closed.

## Opt-in automatic updates

Set `"auto_update": true` in `config.json` and restart a **Windows 10/11 packaged release**. It is disabled by default and for source runs. Install the first updated executable manually; older agents cannot update themselves. The existing public release `v1.4.0` predates this updater; publish a newer tagged release containing these changes before rollout. The release workflow embeds each stable `vMAJOR.MINOR.PATCH` tag into the executable.

1. Check the public `andiharka/sipantau` GitHub repository after 30 seconds and then every six hours. Prereleases and older/equal versions are ignored.
2. While the PC is locked with no local pending action or session, download `SIPANTAU.exe` over verified HTTPS. Require the SHA-256 digest and size from the GitHub release asset API, cap downloads at 128 MiB, and reject incomplete/mismatched files. No Pantau key or password is sent to GitHub.
3. Recheck lock state on the GUI thread, request a native Windows workstation lock, and confirm its session lock flag. If Windows locking is denied by policy, postpone installation and keep the overlay running. **A Windows sign-in is required to return to the desktop after an update.**
4. A PowerShell helper waits for the old agent to exit, checks the staged digest again, saves `SIPANTAU.previous.exe`, replaces only the executable, and starts the new agent. `config.json`, `state.json`, hosts rules and logs are preserved.
5. The new agent writes a readiness marker after its GUI state applies successfully. If startup exits or readiness is absent after 180 seconds, restore and restart the previous executable. `update-status.json` records the latest result. Failed/rolled-back tags are skipped until a new release; an administrator can remove that status file to retry.

Internet loss, GitHub errors or failed downloads leave the current agent running. The updater does not install while an active timed or unlimited session is unlocked. Allow PowerShell and writes to the install directory. Keep that directory protected from ordinary users. SHA-256 verifies integrity against GitHub's metadata; release access controls remain the trust boundary. Authenticode signing would be a useful further improvement.

References: [GitHub release asset API](https://docs.github.com/en/rest/releases/assets), [Windows asynchronous workstation locking](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-lockworkstation), [Windows session lock flags](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w).

## Logs for long runs

`agent.log` now rotates at 5 MiB, keeping five backups (`agent.log.1` through `.5`): about 30 MiB of normal retained logs. A pre-existing oversized log is retained on the first rotation and ages out with subsequent rotations. UTF-8 logs include timestamps, severity and thread names. Network error storms emit at most one summary per service every five minutes, plus a recovery entry. Exception categories are logged for network failures so URLs cannot expose embedded credentials. GUI/control failures remain visible.

The log records starts, local timer starts, process blocking, command failures and update outcomes. It is an operational log with bounded retention, not a permanent audit archive. `state.json` remains the durable control cache. See [Python rotating file handler documentation](https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler).

## Verification before deployment

`python -m unittest discover -s tests -v` covers password checks, offline timed/permanent access, expiry, stop-session, cached restart, corrupt cache, cached app/site rules, reconnect deadlines, stale commands, GUI acknowledgment guards, log rotation/error storms, update metadata/download checks, and native-lock confirmation decisions. Network failures and GUI boundaries are mocked. On Windows CI, two additional tests compile harmless dummy executables and execute the actual PowerShell replacement/rollback helper.

A physical Windows acceptance run is still required for the real fullscreen layout, administrator keyboard hooks, multi-monitor setup, native lock policies, packaged startup, and UAC environment. Unplug networking; start a short local timer and verify expiry; restart during a cached session; verify cached blockers; reconnect after a newer remote command; then test an opt-in update and rollback on a spare PC. Local automated tests cannot prove these Windows desktop interactions.

Further overlay ideas: a collapsible staff panel (cleaner visitor view), keyboard-accessible password visibility toggle, configurable library branding/help contact, scaling across monitor resolutions, and multi-monitor lock coverage. The implemented screen already separates visitor instructions from staff access, shows the PC name and server status, and makes timed access the primary action.
