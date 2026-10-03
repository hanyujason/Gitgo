"""Cross-platform process-tree lifetime control."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
    )


def attach_kill_job(proc: subprocess.Popen):
    """Attach a Windows child to a kill-on-close Job Object."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(proc._handle)),
        ):
            kernel32.CloseHandle(job)
            return None
        return job
    except (AttributeError, OSError, ValueError):
        return None


def close_job(job_handle) -> None:
    if sys.platform != "win32" or not job_handle:
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    except (OSError, ValueError):
        pass


def _unix_process_tree(root_pid: int) -> set[int]:
    """Snapshot a Unix process tree, including descendants in new sessions."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return {root_pid}
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    tree = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in tree:
                tree.add(child)
                pending.append(child)
    return tree


def _terminate_unix_tree(proc: subprocess.Popen) -> None:
    """Kill every owned process group, including nested new sessions."""
    try:
        process_ids = _unix_process_tree(proc.pid)
    except (OSError, subprocess.SubprocessError):
        process_ids = {proc.pid}

    own_group = os.getpgrp()
    groups: set[int] = set()
    ungrouped: set[int] = set()
    for pid in process_ids:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        except OSError:
            ungrouped.add(pid)
            continue
        if group == own_group:
            ungrouped.add(pid)
        else:
            groups.add(group)

    # Descendants may deliberately create their own sessions. Killing only the
    # root group would leave those commands alive after an Agent cancellation.
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pid in ungrouped:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def terminate_tree(proc: subprocess.Popen, job_handle=None) -> None:
    """Terminate descendants and wait until the root process is reaped."""
    if proc.poll() is not None:
        close_job(job_handle)
        return
    try:
        if sys.platform == "win32":
            if job_handle:
                close_job(job_handle)
                job_handle = None
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        else:
            try:
                _terminate_unix_tree(proc)
            except ProcessLookupError:
                pass
            except OSError:
                proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    finally:
        close_job(job_handle)
