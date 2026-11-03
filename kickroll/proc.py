"""Process trees on Windows: Job Object (KILL_ON_JOB_CLOSE) with taskkill /T /F fallback."""
import ctypes
import os
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000

if IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]


class JobHandle:
    """Job Object that ends all assigned processes when closed. Errors -> None (taskkill fallback)."""

    def __init__(self, handle):
        self.handle = handle

    @classmethod
    def attach(cls, proc: subprocess.Popen) -> "JobHandle | None":
        if not IS_WINDOWS or not getattr(proc, "_handle", None):
            return None
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok or not _k32.AssignProcessToJobObject(job, int(proc._handle)):
            _k32.CloseHandle(job)
            return None
        return cls(job)

    def terminate(self) -> None:
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


def kill_tree(pid: int, run=subprocess.run) -> None:
    """Forcefully end the process tree (Popen.kill does not reach child processes on Windows)."""
    if IS_WINDOWS:
        run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=30, check=False)
    else:  # POSIX: worker runs in its own session -> kill the whole process group
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def pid_alive(pid: int) -> bool:
    if not IS_WINDOWS:  # pragma: no cover
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = _k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == 5  # access denied -> exists
    try:
        code = wintypes.DWORD()
        return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def image_name(pid: int) -> str:
    """File name of the process (empty if unknown)."""
    if not IS_WINDOWS:  # pragma: no cover
        return ""
    handle = _k32.OpenProcess(_QUERY_LIMITED, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _k32.CloseHandle(handle)


def kill_orphans(pids, alive=pid_alive, name_of=image_name, kill=kill_tree) -> list[int]:
    """End orphaned claude processes from an earlier run (only if the name matches: PID reuse)."""
    killed = []
    for pid in pids or []:
        if alive(pid) and "claude" in name_of(pid).lower():
            kill(pid)
            killed.append(pid)
    return killed
