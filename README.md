# Service Host: System Observation

Computer lab management client for the SIPANTAU project. Windows packages use `SystemObservation.exe` and a gear icon; publisher metadata continues to identify the SIPANTAU project. The GitHub repository, configuration, state, and stable installation IDs retain their existing names.

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

Place `config.json` beside the complete Python source checkout (or the compiled `.exe`). Keep the helper modules `control_runtime.py`, `client_logging.py`, `client_config.py`, `updater.py`, `windows_runtime.py`, `remote_commands.py`, `screenshot_codec.py`, and `version.py` alongside `agent.py` when running from source. Obtain a shared bootstrap key or an individual PC key from the Perpusip administrator. The API key must contain at least 32 characters.

```json
{
  "api_key": "paste_the_generated_agent_key_here",
  "server_url": "http://192.168.1.1:3333",
  "master_password": "set_a_private_local_password",
  "verify_ssl": true,
  "screenshot_jpeg_quality": 80,
  "auto_update": false,
  "autorun": false,
  "auto_lock_on_run": false,
  "hostname": "STUDENT-PC01"
}
```

---

## Installation & Setup

1. **Install Dependencies:**
   Ensure Python 3.10+ is installed, then run:
   ```cmd
   pip install -r requirements.txt
   ```

2. **Run the Agent:**
   Open a Command Prompt or PowerShell terminal **as Administrator** and execute:
   ```cmd
   python agent.py
   ```

---

## Building and installing Windows packages

The recommended package is **SystemObservation-portable.zip**: extract the entire `SystemObservation` folder and keep `_internal` beside `SystemObservation.exe`. It does not self-extract its runtime into a temporary directory. Store the folder where only administrators can modify program files. Configure `server_url`, `api_key`, and a private `master_password` in `config.json` beside the executable before starting it.

Releases also include a separate **config.json** template for the optional single-file executable. Save it beside `SystemObservation.exe` and configure it before the first launch. The same template is already included in the portable ZIP.

To upgrade a folder installation, stop the old agent, back up its directory, extract the new package into a separate directory, and copy your existing `config.json` and `state.json` into that directory. Keep the new `_internal` directory. The included config is a public template, not your saved configuration; do not overwrite your private settings with it. The build helper never copies the workstation's real config into a release.

Build on Windows using the actual release version in place of the example tag:

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python scripts/build_windows.py onedir --tag v1.7.0
./scripts/scan_release.ps1 -Path dist/onedir/SystemObservation -ReportPath build-reports/portable-scan.json
python scripts/build_windows.py archive --tag v1.7.0
./scripts/scan_release.ps1 -Path release/SystemObservation-portable.zip -ReportPath build-reports/portable-archive-scan.json
```

The helper embeds the version and package format, adds Windows product/version metadata, pins the build tool versions through `requirements-build.txt`, and explicitly disables UPX. The original source `version.py` is restored even if building fails. Windows administrator access remains required for the existing hosts and kiosk functions.

Releases retain identical `SIPANTAU.exe` and `SIPANTAU-portable.zip` compatibility assets so older installed update checks can find the renamed build. These installations continue running at their existing executable path. New installations use `SystemObservation.exe`.

An optional legacy `SystemObservation.exe` single-file artifact is retained for compatible existing installations and their executable-only updater. The workflow publishes it only after its separate Defender scan succeeds. PyInstaller advises against running a one-file executable with administrator privileges because of temporary-library extraction risks; use the complete folder package for new deployments. A different package or a passing scan is not a guarantee that a detection was a false positive.

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

- **Mulai sesi dengan timer:** 1–1440 minutes, with **30 menit**, **1 jam**, and **3 jam** presets; 30 minutes is selected initially. Enter starts this mode by default. The PC unlocks immediately and relocks when time runs out, even if Perpusip and the internet are unreachable.
- **Buka tanpa batas waktu:** unlock until a staff/server lock command or the configured startup lock policy. This still requires the password.

The floating countdown and **Akhiri sesi** work locally. Cached application rules continue to be enforced. Cached website rules are reapplied at startup independently of network requests; changes remove only `# agent-blocked` entries. The initial cache can only contain rules previously received from Perpusip. Without a cache there are no rules to enforce, and the default local lock remains available.

New internet connectivity is not required for Perpusip control when the LAN server remains reachable. With the server unreachable, new remote commands, rule changes and member lookups wait for connectivity. The lock screen shows server connection status, not general internet status.

`POST /api/pantau/action/start-session` sends `hostname`, `ip`, `expected_version`, `remaining_seconds`, and the original timezone-aware `expires_at`. Perpusip adopts at most the remaining time and original deadline. Expired sessions stay locked. A newer server control version supersedes the queued local action. Before the first server heartbeat, local access is usable but cannot be replayed safely on reconnect; the server state takes precedence.

Running countdowns use monotonic time. Restarting offline reconstructs the deadline from the persisted UTC expiry and last server clock offset; changing the system clock across a restart can affect this estimate. `auto_lock_on_run=true` always locks on startup, even with an unexpired cached session. Damaged state files fail closed.

## Opt-in automatic updates

For a **Windows 10/11 legacy single-file release**, set `"auto_update": true` in `config.json` and restart the agent. The folder package disables executable-only automatic installation regardless of this flag: check for updates in settings and install the complete new folder manually. It is disabled by default and for source runs. Install the first updated executable manually; older agents cannot update themselves. The existing public release `v1.4.0` predates this updater; publish a newer tagged release containing these changes before rollout. The release workflow embeds each stable `vMAJOR.MINOR.PATCH` tag into the executable.

1. Check the public `andiharka/sipantau` GitHub repository after 30 seconds and then every six hours. Prereleases and older/equal versions are ignored.
2. While the PC is locked with no local pending action or session, download `SystemObservation.exe` over verified HTTPS. Require the SHA-256 digest and size from the GitHub release asset API, cap downloads at 128 MiB, and reject incomplete/mismatched files. No Pantau key or password is sent to GitHub.
3. Recheck lock state on the GUI thread, request a native Windows workstation lock, and confirm its session lock flag. If Windows locking is denied by policy, postpone installation and keep the overlay running. **A Windows sign-in is required to return to the desktop after an update.**
4. A PowerShell helper waits for the old agent to exit, checks the staged digest again, saves a `.previous.exe` backup beside the current executable, replaces only the executable, and starts the new agent. `config.json`, `state.json`, hosts rules and logs are preserved.
5. The new agent writes a readiness marker after its GUI state applies successfully. If startup exits or readiness is absent after 180 seconds, restore and restart the previous executable. `update-status.json` records the latest result. Failed/rolled-back tags are skipped until a new release; an administrator can remove that status file to retry.

Internet loss, GitHub errors or failed downloads leave the current agent running. The updater does not install while an active timed or unlimited session is unlocked. Allow PowerShell and writes to the install directory. Keep that directory protected from ordinary users. SHA-256 verifies integrity against GitHub's metadata; release access controls remain the trust boundary. Authenticode signing would be a useful further improvement.

References: [GitHub release asset API](https://docs.github.com/en/rest/releases/assets), [Windows asynchronous workstation locking](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-lockworkstation), [Windows session lock flags](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w).

## Logs for long runs

`agent.log` now rotates at 5 MiB, keeping five backups (`agent.log.1` through `.5`): about 30 MiB of normal retained logs. A pre-existing oversized log is retained on the first rotation and ages out with subsequent rotations. UTF-8 logs include timestamps, severity and thread names. Network error storms emit at most one summary per service every five minutes, plus a recovery entry. Exception categories are logged for network failures so URLs cannot expose embedded credentials. GUI/control failures remain visible.

The log records starts, local timer starts, process blocking, command failures and update outcomes. It is an operational log with bounded retention, not a permanent audit archive. `state.json` remains the durable control cache. See [Python rotating file handler documentation](https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler).

## Verification before deployment

`python -m unittest discover -s tests -v` covers password checks, offline timed/permanent access, expiry, stop-session, cached restart, corrupt cache, cached app/site rules, reconnect deadlines, stale commands, GUI acknowledgment guards, log rotation/error storms, update metadata/download checks, and native-lock confirmation decisions. Network failures and GUI boundaries are mocked. On Windows CI, three additional tests compile harmless dummy executables and execute the actual PowerShell install, rollback and checksum-rejection paths.

A physical Windows acceptance run is still required for the real fullscreen layout, administrator keyboard hooks, multi-monitor setup, native lock policies, packaged startup, and UAC environment. Unplug networking; start a short local timer and verify expiry; restart during a cached session; verify cached blockers; reconnect after a newer remote command; then test an opt-in update and rollback on a spare PC. Local automated tests cannot prove these Windows desktop interactions.

Further overlay ideas: a collapsible staff panel, configurable library help contact, and multi-monitor lock coverage. The implemented screen separates visitor instructions from staff access, shows the PC name and server status without a product heading, and makes timed access the primary action.


## Server screenshot and shutdown commands

The Pantau PC menu now groups **Kunci Agen**, **Ambil Screenshot**, and **Matikan PC**. Update Perpusip and apply `1792022400000_add_pantau_commands` before installing this client. Install the updated requirements (`mss==10.2.0` and `Pillow==12.3.0` are required) or use a packaged release containing this code.

The agent advertises its version, capabilities and a fresh `boot_id` on heartbeat. Screenshot and shutdown buttons are disabled for old clients, offline/revoked PCs, stale dashboard data and PCs already handling a command. Remote commands require a reachable Perpusip server; internet access is not needed when the server is available on the LAN.

### Delivery and failures

Commands have a UUID, current agent boot ID, expected control version and expiry. Undelivered commands expire after 30 seconds; accepted work has a 120-second limit. One command runs per PC. Durable `command-receipts.json` keeps the last 100 IDs, written before acknowledgment or OS actions. Duplicate delivery never repeats a capture or shutdown. A restart creates a different boot ID, cancelling old requests. If the receipt file is corrupt/unwritable, commands are disabled or rejected instead of risking replay; timers and existing controls continue.

No arbitrary shell commands are accepted. The agent reports through `POST /api/pantau/commands/:id/result` and uploads through `POST /api/pantau/commands/:id/image`, using its existing credentials and hostname binding.

### Shutdown

Staff confirms the target PC and any active session. The client displays a local 30-second warning. A newer remote control version, local staff action, or timer expiry cancels the pending shutdown. The final execution permission is checked by Perpusip under the PC row lock. At that point the active session ends and the next-startup desired state is locked. Execution permission is not replayable; losing its response prevents the client from executing shutdown.

The agent then uses the fixed Windows command `shutdown.exe /s /t 0 /d p:0:0`. The warning is implemented locally so the OS command does not imply forced application closure. Unsaved applications can block shutdown. **Submitted** means Windows accepted the shutdown command, not that power-off has been proven; **Offline** can also mean a network failure. If reporting is interrupted, the result may remain unconfirmed. The updater and remote command worker share a guard so they cannot replace the executable during shutdown/capture, and Tk does not wait on their worker lock.

### Screenshots

MSS captures the primary monitor's currently visible desktop. If Pantau is locked, the capture includes its overlay; the agent never hides it to capture underlying content. The native Windows secure desktop is unavailable and returns an explicit error. The encoder compares JPEG at quality 80 with PNG and sends whichever is smaller. JPEG keeps full resolution and full color sampling to preserve colored UI text; simple screens often retain lossless PNG. Set `screenshot_jpeg_quality` to an integer from 40–95 (default 80) to adjust JPEG quality. Images are limited to 10 MiB, 8192 pixels per side and 16 million pixels total. Capture timestamps are derived from the acknowledged server clock plus monotonic elapsed time. Upload retries reuse the same capture.

Perpusip accepts both JPEG and PNG, validates actual image data and dimensions (with bounded decoding), then stores the image privately in `pantau_commands.screenshot_data` in the Perpusip database. It is never exposed through `/uploads`. Authenticated staff can view the preview, capture time and dimensions, or request another capture. Image access has `Cache-Control: no-store`. Images become inaccessible after 24 hours and are removed by scheduler cleanup; command metadata is retained for seven days. Status polling never selects image bytes. Ensure MySQL `max_allowed_packet` permits a 10 MiB image.

### Acceptance checks

Automated tests mock OS shutdown and desktop capture; they never turn off a real PC. They exercise delivery/replay guards, offline timeouts, state changes during countdown, receipt failures, upload retry, secure-desktop errors, native command arguments, updater interaction, authentication, image ownership, JPEG/PNG validation and retention. The Windows release workflow runs these tests before PyInstaller.

On a spare Windows PC, verify the three menu actions, keyboard menu navigation, screenshot preview with unlocked/locked overlay states, unsaved-work warning and shutdown, cancellation by a new session/local staff access, reconnect/restart without replay, and the packaged updater interaction before rollout.


JPEG server decoding uses a bounded worker so image processing cannot block heartbeat polling. PNG screenshots from older clients remain supported, and previews use the MIME type detected from the stored bytes. No additional database migration is needed for JPEG support. Deploy the server dependency/code update before updating client packages.

### Updater failure diagnostics and CI cleanup

The installer records `phase`, `error`, and the launched `agent_pid` in `update-status.json`. It explicitly handles an already-exited parent, replaces the executable and creates its backup with a single Windows file-replacement operation, and retries transient image locks. Rollback stops the complete PyInstaller process tree before restoring the previous executable. A `rollback-failed` outcome requires staff repair and is skipped by automatic retries.

Windows installer tests use a real temporary parent process. Every test waits for or stops only its own dummy executables before deleting their directory, including when an assertion fails. Checksum rejection, successful readiness and failed-startup rollback are tested separately, with phase/error diagnostics included in failed assertions. The Windows test workflow runs on pull requests independently of tagged releases, so these checks can be verified before publishing a new executable.

The installer computes the staged SHA-256 using .NET with a file stream, disposing the stream before replacement. It does not depend on the `Get-FileHash` cmdlet, which can be unavailable in inherited PowerShell environments. All three native installer tests deliberately disable that cmdlet to cover this failure. Both Windows workflows use Node 24 versions of checkout and setup-python.


### Startup configuration and staff controls

At every process start, the client reads `config.json` beside the executable (or beside `agent.py` for source runs), independent of the working directory. It accepts UTF-8 with or without a BOM. Missing/unreadable files, invalid JSON or field types, invalid connection settings, and a missing/blank staff password show a warning dialog containing the exact file path. The process exits before loading the kiosk or keyboard hooks. There is no default master password; configure a private nonempty value before starting. Configuration is read once per process; restart after editing it. The startup log records the loaded path/time without credentials.

The lock screen has a mouse- and keyboard-operated eye icon for password visibility. Presets are 30 minutes, 1 hour and 3 hours; custom durations of 1–1440 minutes remain available. Password errors clear on editing or another attempt. Unlock application also reconciles already-acknowledged local actions, so a fast heartbeat cannot skip dismissing the overlay. Old focus/status callbacks are cancelled on dismissal, an already-released grab cannot prevent destruction, and repeated lock commands reuse the same window.

Hover over the bottom-right 44-pixel corner button (also reachable with Tab) to reveal settings. The viewer shows the configuration loaded at startup, its file path/time and the agent version. It uses an explicit field allowlist: staff passwords are never displayed, API keys show only the last four characters, URL credentials/query/fragment are removed, and unknown config fields are omitted. The viewer is read-only and closing it returns focus to the password field.

The draggable timer uses a readable seconds countdown and a remaining-time bar. Hover or focus expands its controls. It automatically expands with an amber warning below ten minutes and a red warning below three minutes. The timer only appears after the lock overlay is actually dismissed; ending a session still requires confirmation and works offline.

Config and UI regression tests cover BOM files, startup warnings and exact paths, secret masking, preset/eye/settings callbacks, wrong-then-correct password entry, early heartbeat acknowledgment, stale focus callbacks and failed unlocks. GUI widgets are simulated in local tests: verify the actual Windows appearance, hover/focus behavior, text scaling and keyboard hooks on a spare PC before rollout.


The lock screen shows the running version in its bottom-left corner without a product heading. The hidden settings viewer includes **Periksa pembaruan**, which checks the latest public stable GitHub release in a background worker and reports an available version, an up-to-date version, connection/rate-limit failures, missing releases, or unverifiable metadata. It works independently of `auto_update`, uses verified HTTPS and the same release asset checks as the automatic updater, and does not download or install a package. Closing/unlocking the screen while a check runs safely discards that dialog's result. Installation continues to use the existing opt-in automatic updater and its lock/readiness protections.


## Antivirus detection and release verification

These changes improve package transparency and release checks; they do not establish that the reported v1.6.0 Defender detection was a false positive or guarantee acceptance by every antivirus product. The project does not modify antivirus exclusions, disable protection, obfuscate the program, or claim that unsigned binaries are signed.

For each release the workflow:

1. Tests the client and build/release guards, builds the complete folder, and scans all its files with Microsoft Defender.
2. Creates and scans the final portable ZIP. Defender must provide a running scan service, a valid engine, and a successful security-intelligence update. Real-time monitoring is recorded but is not required for the explicit on-demand scan. Missing scanners, scan errors/detections, and files changed/deleted during scanning block publication of the required folder package.
3. Separately builds and scans the optional legacy executable. If it fails to build or scan, it is omitted while an approved folder package can still be released. Existing executable-only updaters skip folder-only releases; manual checks can discover the folder package.
4. Records Defender engine/signature versions, scan outcomes, per-file SHA-256 values, installed dependency versions, source commit and workflow URL. `SHA256SUMS.txt` covers the actual release artifacts. These hashes provide consistency checks, not publisher authentication.

`-DisableRemediation` in the custom scan prevents the scanner from modifying that scan's input so hashes can be compared before/after. It does not change real-time protection settings. GitHub's hosted Windows image disables real-time monitoring and adds broad exclusions; this custom-scan option ignores file exclusions and includes archive scanning as documented by Microsoft. Any nonzero scan result is rejected, and files that disappear due to real-time quarantine also fail the gate.

Run **Build and Release System Observation → Run workflow** with a stable version string to test the build/scans without creating a GitHub Release. A successful manual run saves only the approved candidate packages as an Actions artifact. Scan reports/logs are saved on failed runs as well. Tag-triggered runs publish only after the required gates pass. If the hosted Windows runner cannot provide a running Defender service and a working on-demand scan, the workflow stops with the native scan error and diagnostic report; use a runner that supports Defender scanning. The gate does not require endpoint real-time monitoring to be enabled.

PowerShell regression tests mock Defender to verify clean scans with real-time monitoring enabled or disabled, passive-mode scanning, detections even with real-time monitoring off, quarantine, altered files, disabled services, unavailable scanners, missing engines/signatures and signature-update failure paths. They are decision tests, not real malware scans. Native Windows packaging, the Defender verdict on the resulting binaries, and the kiosk UI still require a Windows run. Publisher signing remains an additional step requiring a valid signing identity; none is configured or fabricated by this change.

References: [PyInstaller packaging and administrator warning](https://pyinstaller.org/en/stable/operating-mode.html), [Defender scan options and exit codes](https://learn.microsoft.com/en-us/defender-endpoint/command-line-arguments-microsoft-defender-antivirus), [Microsoft developer detection review](https://learn.microsoft.com/en-us/defender-xdr/developer-faq).


### Hosted-runner Defender status

Defender status is read directly from `MSFT_MpComputerStatus` via CIM, avoiding the PowerShell 7 `WinPSCompatSession` warning from importing the Defender module. Each scan report includes `scan_policy: on-demand`, `defender_before` and `defender_after_update` snapshots with service, antivirus, real-time monitoring, engine and signature flags. Those diagnostics distinguish runner configuration from a malware detection; the package still requires a completed zero-exit-code custom scan and unchanged file hashes.

GitHub's [Windows Defender runner-image configuration](https://github.com/actions/runner-images/blob/main/images/windows/scripts/build/Configure-WindowsDefender.ps1) disables real-time monitoring. Treating that flag as a prerequisite incorrectly blocked the initial v1.6.1 build before its artifact was scanned. No global Defender policy or protection settings are changed by this correction.
