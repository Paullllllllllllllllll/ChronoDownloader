"""Whether the process of a run on this machine is still running.

The store records each run's process creation time (:func:`own_creation`)
and asks :func:`process_alive` when another run's lease blocks a work or
holds a work the reconciliation would repair: a lease whose holder process
is gone is free. Nothing here signals or ends a process.

On Windows the process is opened for query only (``OpenProcess`` with
``PROCESS_QUERY_LIMITED_INFORMATION``); it runs while ``GetExitCodeProcess``
reports ``STILL_ACTIVE``, and a running process whose creation time
(``GetProcessTimes``) differs from the recorded one reused the pid and does
not count. An unknown identity (no recorded time, or a query that fails)
counts as running. Elsewhere ``os.kill(pid, 0)`` tests for the pid, without
a guard against pid reuse, and no creation time is recorded. ``os.kill`` is
never called on Windows, where it ends the process.
"""

from __future__ import annotations

import os
import sys

__all__ = ["CREATION_TOLERANCE_S", "own_creation", "process_alive"]

CREATION_TOLERANCE_S = 1.0
"""Seconds two readings of one process's creation time may differ."""

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _EPOCH_AS_FILETIME = 116_444_736_000_000_000
    """1970-01-01 in 100-ns intervals since 1601-01-01."""

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = ()
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        *(ctypes.POINTER(wintypes.FILETIME),) * 4,
    )
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _created(handle: int) -> float | None:
        """Return the process's creation time (epoch s), None if unknown."""
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        creation = times[0]
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return (ticks - _EPOCH_AS_FILETIME) / 10_000_000

    def own_creation() -> float | None:
        """Return this process's creation time (epoch s), None if unknown."""
        return _created(_kernel32.GetCurrentProcess())

    def process_alive(pid: int, created: float | None = None) -> bool:
        """Return True while *pid* runs as the process created at *created*.

        *created* is in epoch seconds, as :func:`own_creation` recorded it;
        None skips the check. A process this user may not query counts as running.
        """
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            if code.value != _STILL_ACTIVE:
                return False
            if created is None:
                return True
            actual = _created(handle)
            return actual is None or abs(actual - created) <= CREATION_TOLERANCE_S
        finally:
            _kernel32.CloseHandle(handle)

else:

    def own_creation() -> float | None:
        """Return None: no creation time is recorded on this platform."""
        return None

    def process_alive(pid: int, created: float | None = None) -> bool:
        """Return True while *pid* exists; *created* is not checked here.

        A process this user may not signal counts as running.
        """
        del created
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
