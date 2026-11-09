"""Elevated, interactive Windows login tasks; no saved account passwords."""
import base64
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


TASK_PREFIX = 'SIPANTAUAgent-'
LEGACY_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
LEGACY_VALUE = 'SipantauAgent'
TASK_NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


class AutorunError(RuntimeError):
    pass


# Input is JSON on stdin, never interpolated into PowerShell source or a shell command.
IDENTITY_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($inputData.require_interactive) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AgentLoginSession {
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool WTSQuerySessionInformationW(IntPtr server, int session,
        int info, out IntPtr buffer, out int size);
    [DllImport("wtsapi32.dll")]
    public static extern void WTSFreeMemory(IntPtr buffer);
    public static string Read(int session, int info) {
        IntPtr buffer; int size;
        if (!WTSQuerySessionInformationW(IntPtr.Zero, session, info, out buffer, out size))
            throw new System.ComponentModel.Win32Exception();
        try { return Marshal.PtrToStringUni(buffer); }
        finally { WTSFreeMemory(buffer); }
    }
}
'@
        $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
        $user = [AgentLoginSession]::Read($session, 5)
        $domain = [AgentLoginSession]::Read($session, 7)
        if ([string]::IsNullOrWhiteSpace($user)) { throw 'Sesi desktop pengguna tidak ditemukan.' }
        $account = if ($domain) { "$domain\$user" } else { $user }
        $sessionSid = (New-Object Security.Principal.NTAccount($account)).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        if ($sessionSid -ne $identity.User.Value) {
            throw 'Akun administrator berbeda dari pengguna desktop. Masuk ke Windows dengan akun administrator yang akan menjalankan agen, lalu jalankan agen sebagai administrator. Akun lab standar memerlukan layanan terpisah.'
        }
    }
    @{user_id = $identity.User.Value; elevated = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)} | ConvertTo-Json -Compress
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
'''


SCHEDULER_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')
    if ($inputData.enabled) {
        # TASK_CREATE_OR_UPDATE=6; TASK_LOGON_INTERACTIVE_TOKEN=3.
        $null = $folder.RegisterTask($inputData.task_name, $inputData.xml, 6,
            $inputData.user_id, $null, 3, $null)
        $task = $folder.GetTask($inputData.task_name)
        # Verify persisted XML rather than derived COM interfaces. COM may expose
        # only the base trigger interface and returns null for empty arguments.
        [xml]$saved = $task.Xml
        $ns = New-Object Xml.XmlNamespaceManager($saved.NameTable)
        $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        function ReadTask($path) {
            $node = $saved.SelectSingleNode('/t:Task/' + $path, $ns)
            if ($null -eq $node) { return '' }
            return [string]$node.InnerText
        }
        function AccountSid($account) {
            if ($account -like 'S-1-*') { return $account }
            return (New-Object Security.Principal.NTAccount($account)).Translate(
                [Security.Principal.SecurityIdentifier]).Value
        }
        function ReadBool($path, $default) {
            $value = ReadTask $path
            if ($value -eq '') { return $default }
            return [Xml.XmlConvert]::ToBoolean($value)
        }
        $checks = @(
            @('Enabled', [bool]$task.Enabled, $true),
            @('RunLevel', (ReadTask 't:Principals/t:Principal/t:RunLevel'), 'HighestAvailable'),
            @('LogonType', (ReadTask 't:Principals/t:Principal/t:LogonType'), 'InteractiveToken'),
            @('Principal.UserId', (AccountSid (ReadTask 't:Principals/t:Principal/t:UserId')), $inputData.user_id),
            @('Actions.Count', $saved.SelectNodes('/t:Task/t:Actions/*', $ns).Count, 1),
            @('Triggers.Count', $saved.SelectNodes('/t:Task/t:Triggers/*', $ns).Count, 1),
            @('LogonTrigger.Count', $saved.SelectNodes('/t:Task/t:Triggers/t:LogonTrigger', $ns).Count, 1),
            @('Trigger.Enabled', (ReadBool 't:Triggers/t:LogonTrigger/t:Enabled' $true), $true),
            @('Trigger.UserId', (AccountSid (ReadTask 't:Triggers/t:LogonTrigger/t:UserId')), $inputData.user_id),
            @('ExecutionTimeLimit', [Xml.XmlConvert]::ToTimeSpan((ReadTask 't:Settings/t:ExecutionTimeLimit')).Ticks, 0),
            @('DisallowStartIfOnBatteries', (ReadBool 't:Settings/t:DisallowStartIfOnBatteries' $true), $false),
            @('StopIfGoingOnBatteries', (ReadBool 't:Settings/t:StopIfGoingOnBatteries' $true), $false),
            @('RunOnlyIfNetworkAvailable', (ReadBool 't:Settings/t:RunOnlyIfNetworkAvailable' $false), $false),
            @('Command', (ReadTask 't:Actions/t:Exec/t:Command'), [string]$inputData.executable),
            @('Arguments', (ReadTask 't:Actions/t:Exec/t:Arguments'), [string]$inputData.arguments),
            @('WorkingDirectory', (ReadTask 't:Actions/t:Exec/t:WorkingDirectory'), [string]$inputData.directory)
        )
        $mismatches = @($checks | Where-Object {
            if ($_[0] -eq 'Arguments') { $_[1] -cne $_[2] }
            else { $_[1] -ne $_[2] }
        } | ForEach-Object {
            '{0}: expected [{1}], actual [{2}]' -f $_[0], $_[2], $_[1]
        })
        if ($mismatches.Count) {
            throw ('Pendaftaran tugas tidak sesuai: ' + ($mismatches -join '; '))
        }
    } else {
        # Enumerate including hidden tasks, avoiding locale-dependent "not found" errors.
        $exists = @($folder.GetTasks(1) | Where-Object { $_.Name -eq $inputData.task_name })
        if ($exists.Count) { $folder.DeleteTask($inputData.task_name, 0) }
        $remaining = @($folder.GetTasks(1) | Where-Object { $_.Name -eq $inputData.task_name })
        if ($remaining.Count) { throw 'Tugas masuk belum berhasil dihapus.' }
    }
    if ($inputData.enabled) { @{ok = $true; xml = $task.Xml} | ConvertTo-Json -Compress }
    else { @{ok = $true} | ConvertTo-Json -Compress }
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
'''


def _powershell(script, payload):
    # Windows PowerShell accepts UTF-16LE encoded source. JSON uses ASCII escapes for
    # Unicode paths, so Windows console encodings cannot corrupt executable names.
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    system_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    command = [str(system_root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'),
               '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    try:
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutorunError('Windows Task Scheduler tidak dapat diakses: ' + str(error)) from error
    if result.returncode:
        raise AutorunError((result.stderr.strip() or 'Windows menolak pendaftaran tugas.')[:1000])
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object')
        return value
    except (TypeError, ValueError) as error:
        raise AutorunError('Hasil Windows Task Scheduler tidak dapat diverifikasi.') from error


def task_xml(user_id, executable, arguments, directory):
    task = ET.Element('Task', xmlns=TASK_NS, version='1.2')

    def child(parent, name, text=None):
        node = ET.SubElement(parent, name)
        node.text = text
        return node

    registration = child(task, 'RegistrationInfo')
    child(registration, 'Description', 'SIPANTAU interactive agent at user sign-in')
    trigger = child(child(task, 'Triggers'), 'LogonTrigger')
    child(trigger, 'Enabled', 'true')
    child(trigger, 'UserId', user_id)
    principal = ET.SubElement(child(task, 'Principals'), 'Principal', id='AgentUser')
    child(principal, 'UserId', user_id)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'HighestAvailable')
    settings = child(task, 'Settings')
    for name, value in [('MultipleInstancesPolicy', 'IgnoreNew'),
                        ('DisallowStartIfOnBatteries', 'false'),
                        ('StopIfGoingOnBatteries', 'false'),
                        ('StartWhenAvailable', 'true'), ('RunOnlyIfNetworkAvailable', 'false'),
                        ('Enabled', 'true'), ('RunOnlyIfIdle', 'false'),
                        ('ExecutionTimeLimit', 'PT0S')]:
        child(settings, name, value)
    action = child(ET.SubElement(task, 'Actions', Context='AgentUser'), 'Exec')
    child(action, 'Command', executable)
    child(action, 'Arguments', arguments)
    child(action, 'WorkingDirectory', directory)
    return ET.tostring(task, encoding='unicode')


def _remove_legacy_entry():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LEGACY_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, LEGACY_VALUE)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass


def configure_autorun(enabled, executable, directory, script=None):
    """Register/remove this account's task, then retire its old HKCU Run entry.

    Call from the Windows boundary only. Enabling requires elevation in the same
    account as the desktop user; running as another admin cannot provide that
    user's elevated interactive token at their next login.
    """
    identity = _powershell(IDENTITY_SCRIPT, {'require_interactive': enabled})
    user_id = identity.get('user_id', '')
    if not isinstance(user_id, str) or not user_id.startswith('S-1-'):
        raise AutorunError('Akun Windows untuk tugas masuk tidak dapat ditentukan.')
    if enabled and not identity.get('elevated'):
        raise AutorunError('Jalankan agen sebagai administrator untuk mengaktifkan tugas masuk.')
    arguments = subprocess.list2cmdline([script]) if script else ''
    payload = {'enabled': enabled, 'task_name': TASK_PREFIX + user_id, 'user_id': user_id,
               'executable': executable, 'arguments': arguments, 'directory': directory,
               'xml': task_xml(user_id, executable, arguments, directory) if enabled else ''}
    result = _powershell(SCHEDULER_SCRIPT, payload)
    if result.get('ok') is not True:
        raise AutorunError('Pendaftaran Windows Task Scheduler belum terkonfirmasi.')
    try:
        _remove_legacy_entry()
    except OSError as error:
        raise AutorunError('Tugas masuk diperbarui, tetapi entri startup lama gagal dihapus: ' + str(error)) from error
    return payload['task_name']
