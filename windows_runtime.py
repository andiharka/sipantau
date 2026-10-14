"""Small Windows API boundary used to protect executable replacement."""
import ctypes
import sys
import time

# Fixed-width types match wtsapi32.h on both 32- and 64-bit Windows.
class WtsLevel1(ctypes.Structure):
    _fields_ = [('session_id', ctypes.c_uint32), ('session_state', ctypes.c_int32),
                ('session_flags', ctypes.c_int32), ('station', ctypes.c_uint16 * 33),
                ('user', ctypes.c_uint16 * 21), ('domain', ctypes.c_uint16 * 18),
                ('logon', ctypes.c_int64), ('connect', ctypes.c_int64),
                ('disconnect', ctypes.c_int64), ('last_input', ctypes.c_int64),
                ('current_time', ctypes.c_int64), ('incoming_bytes', ctypes.c_uint32),
                ('outgoing_bytes', ctypes.c_uint32), ('incoming_frames', ctypes.c_uint32),
                ('outgoing_frames', ctypes.c_uint32), ('incoming_compressed', ctypes.c_uint32),
                ('outgoing_compressed', ctypes.c_uint32)]


class WtsData(ctypes.Union):
    _fields_ = [('level1', WtsLevel1)]


class WtsInfo(ctypes.Structure):
    _fields_ = [('level', ctypes.c_uint32), ('data', WtsData)]


def session_locked():
    api = ctypes.windll.wtsapi32
    api.WTSQuerySessionInformationW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)]
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    buffer = ctypes.c_void_p()
    size = ctypes.c_uint32()
    # WTS_CURRENT_SESSION and WTSSessionInfoEx.
    if not api.WTSQuerySessionInformationW(None, 0xffffffff, 25, ctypes.byref(buffer), ctypes.byref(size)):
        return False
    try:
        if size.value < ctypes.sizeof(WtsInfo):
            return False
        info = ctypes.cast(buffer, ctypes.POINTER(WtsInfo)).contents
        return info.level == 1 and info.data.level1.session_flags == 0
    finally:
        api.WTSFreeMemory(buffer)


def lock_for_update(query=session_locked, sleep=time.sleep):
    # Windows 7 has reversed WTS flags and is outside the supported update platform.
    if sys.getwindowsversion().major < 10:
        return False
    if query():
        return True
    if not ctypes.windll.user32.LockWorkStation():
        return False
    # LockWorkStation returns before locking. Verify native session state before exiting Tk.
    for _ in range(50):
        sleep(0.1)
        if query():
            return True
    return False
