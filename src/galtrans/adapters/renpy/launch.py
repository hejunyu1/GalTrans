from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar

from galtrans.adapters.renpy.sdk import (
    _FATAL_OUTPUT_MARKERS,
    RenpySdkError,
    _renpy_version,
    _stage_renpy_export,
)

_WM_CLOSE = 0x0010
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_LAUNCH_HARNESS = """# Generated only inside a GalTrans temporary validation project.
init -1000 python:
    config.quit_action = Quit(confirm=False)
"""


@dataclass(frozen=True, slots=True)
class _WindowEvidence:
    handle: int
    title: str
    client_width: int
    client_height: int


@dataclass(frozen=True, slots=True)
class RenpyLaunchValidation:
    sdk_root: Path
    version: str
    language: str
    source_file_count: int
    translation_file_count: int
    window_title: str
    client_width: int
    client_height: int
    shutdown_method: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["sdk_root"] = str(self.sdk_root)
        return result


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
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


class _IoCounters(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsJob:
    """Own a process tree and let Windows kill every member when the job closes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            raise RenpySdkError("Ren'Py 图形启动验证目前只支持 Windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise RenpySdkError(f"无法创建 Windows Job Object：错误 {ctypes.get_last_error()}")
        self._kernel32 = kernel32
        self._handle: int | None = int(handle)

        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise RenpySdkError(f"无法配置 Windows Job Object：错误 {error}")

        process_handle = wintypes.HANDLE(int(process._handle))
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            error = ctypes.get_last_error()
            self.close()
            raise RenpySdkError(f"无法把 Ren'Py 进程加入 Job Object：错误 {error}")

    def close(self, *, timeout_seconds: float = 2.0) -> None:
        if self._handle is None:
            return
        handle = wintypes.HANDLE(self._handle)
        self._handle = None
        cleanup_error: RenpySdkError | None = None
        try:
            if not self._kernel32.TerminateJobObject(handle, 1):
                cleanup_error = RenpySdkError(
                    f"无法终止 Ren'Py Job Object 中的进程树：错误 {ctypes.get_last_error()}"
                )
            else:
                deadline = time.monotonic() + timeout_seconds
                while True:
                    accounting = _JobObjectBasicAccountingInformation()
                    if not self._kernel32.QueryInformationJobObject(
                        handle,
                        1,
                        ctypes.byref(accounting),
                        ctypes.sizeof(accounting),
                        None,
                    ):
                        cleanup_error = RenpySdkError(
                            f"无法确认 Ren'Py Job Object 已清空：错误 {ctypes.get_last_error()}"
                        )
                        break
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        cleanup_error = RenpySdkError("Ren'Py Job Object 终止后仍有活动进程")
                        break
                    time.sleep(0.01)
        finally:
            if not self._kernel32.CloseHandle(handle) and cleanup_error is None:
                cleanup_error = RenpySdkError(
                    f"无法关闭 Windows Job Object：错误 {ctypes.get_last_error()}"
                )
        if cleanup_error is not None:
            raise cleanup_error


def _visible_window(process_id: int) -> _WindowEvidence | None:
    if os.name != "nt":
        raise RenpySdkError("Ren'Py 图形启动验证目前只支持 Windows")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    evidence: _WindowEvidence | None = None

    def inspect_window(handle: int, _: int) -> bool:
        nonlocal evidence
        owner_process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_process_id))
        if owner_process_id.value != process_id or not user32.IsWindowVisible(handle):
            return True

        rectangle = wintypes.RECT()
        if not user32.GetClientRect(handle, ctypes.byref(rectangle)):
            return True
        width = rectangle.right - rectangle.left
        height = rectangle.bottom - rectangle.top
        if width <= 0 or height <= 0:
            return True

        title_length = user32.GetWindowTextLengthW(handle)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(handle, title_buffer, len(title_buffer))
        evidence = _WindowEvidence(
            handle=int(handle),
            title=title_buffer.value,
            client_width=width,
            client_height=height,
        )
        return False

    callback = callback_type(inspect_window)
    ctypes.set_last_error(0)
    if not user32.EnumWindows(callback, 0) and evidence is None:
        error = ctypes.get_last_error()
        if error:
            raise RenpySdkError(f"无法枚举 Ren'Py 窗口：错误 {error}")
    return evidence


def _request_window_close(process_id: int) -> None:
    if os.name != "nt":
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL

    def close_window(handle: int, _: int) -> bool:
        owner_process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_process_id))
        if owner_process_id.value == process_id:
            user32.PostMessageW(handle, _WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(callback_type(close_window), 0)


def _wait_for_display(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    stability_seconds: float,
    poll_interval_seconds: float = 0.05,
) -> _WindowEvidence:
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    stable_evidence: _WindowEvidence | None = None
    while True:
        return_code = process.poll()
        evidence = _visible_window(process.pid)
        now = time.monotonic()
        if evidence is None:
            stable_since = None
            stable_evidence = None
        elif stable_since is None:
            stable_since = now
            stable_evidence = evidence
        else:
            stable_evidence = evidence
            if now - stable_since >= stability_seconds:
                return stable_evidence

        if return_code is not None:
            raise RenpySdkError(f"Ren'Py 在基础显示证据出现前退出（退出码 {return_code}）")
        if now >= deadline:
            raise RenpySdkError(f"Ren'Py 在 {timeout_seconds:g} 秒内未出现稳定的可见窗口")
        time.sleep(min(poll_interval_seconds, max(0.0, deadline - now)))


def _shutdown_process(
    process: subprocess.Popen[bytes],
    job: _WindowsJob,
    *,
    grace_seconds: float,
) -> str:
    method = "normal-exit"
    try:
        return_code = process.poll()
        if return_code is not None:
            if return_code != 0:
                raise RenpySdkError(f"Ren'Py 在显示成功后异常退出（退出码 {return_code}）")
            return method

        _request_window_close(process.pid)
        method = "window-close"
        try:
            process.wait(timeout=grace_seconds)
            return method
        except subprocess.TimeoutExpired:
            process.terminate()

        method = "terminate"
        try:
            process.wait(timeout=grace_seconds)
            return method
        except subprocess.TimeoutExpired:
            process.kill()

        method = "kill"
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired as error:
            raise RenpySdkError("Ren'Py 进程在 kill 后仍未退出") from error
        return method
    finally:
        job.close(timeout_seconds=grace_seconds)


def _log_detail(stdout_path: Path, stderr_path: Path) -> str:
    parts: list[str] = []
    for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            parts.append(f"{label}:\n{text[-4000:]}")
    return "\n".join(parts)


def _runtime_error_detail(staged_project: Path, output_detail: str) -> str | None:
    combined = output_detail
    for marker in _FATAL_OUTPUT_MARKERS:
        if marker in combined:
            return combined
    for filename in ("traceback.txt", "errors.txt"):
        path = staged_project / filename
        if path.is_file() and path.stat().st_size:
            contents = path.read_text(encoding="utf-8", errors="replace").strip()
            return f"{filename}:\n{contents[-4000:]}"
    return None


def _install_launch_harness(staged_project: Path) -> None:
    harness_path = staged_project / "game" / "_galtrans_launch_validation.rpy"
    if harness_path.exists():
        raise RenpySdkError(f"临时启动保留脚本路径已被输入项目占用：{harness_path.name}")
    with harness_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_LAUNCH_HARNESS)


def _kill_unmanaged_process(
    process: subprocess.Popen[bytes] | None,
    *,
    timeout_seconds: float,
) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenpySdkError("Ren'Py 未加入 Job Object，且直接 kill 后仍无法确认退出") from error


def validate_renpy_launch(
    sdk_path: Path,
    project_path: Path,
    export_path: Path,
    *,
    language: str = "schinese",
    timeout_seconds: float = 30.0,
    stability_seconds: float = 0.5,
    shutdown_grace_seconds: float = 2.0,
) -> RenpyLaunchValidation:
    """Start a merged export only in writable copies and observe a real window."""
    if os.name != "nt":
        raise RenpySdkError("Ren'Py 图形启动验证目前只支持 Windows")
    if timeout_seconds <= 0 or stability_seconds < 0 or shutdown_grace_seconds <= 0:
        raise ValueError("启动超时和关闭宽限必须大于 0，显示稳定时间不得小于 0")

    with _stage_renpy_export(
        sdk_path,
        project_path,
        export_path,
        language=language,
    ) as staged:
        version = _renpy_version(
            staged.executable,
            staged.staged_sdk,
            timeout_seconds=timeout_seconds,
        )
        _install_launch_harness(staged.staged_project)
        stdout_path = staged.temporary_root / "run.stdout.log"
        stderr_path = staged.temporary_root / "run.stderr.log"
        environment = os.environ.copy()
        isolated_paths = {
            "APPDATA": staged.temporary_root / "appdata",
            "LOCALAPPDATA": staged.temporary_root / "localappdata",
            "TEMP": staged.temporary_root / "process-temp",
            "TMP": staged.temporary_root / "process-temp",
        }
        for path in set(isolated_paths.values()):
            path.mkdir(parents=True, exist_ok=True)
        environment.update({name: str(path) for name, path in isolated_paths.items()})
        environment["RENPY_LANGUAGE"] = language
        environment["SDL_AUDIODRIVER"] = "dummy"

        command = [
            str(staged.executable),
            str(staged.staged_project),
            "run",
            "--savedir",
            str(staged.save_root),
        ]
        process: subprocess.Popen[bytes] | None = None
        job: _WindowsJob | None = None
        with (
            stdout_path.open("xb") as stdout_file,
            stderr_path.open("xb") as stderr_file,
        ):
            try:
                process = subprocess.Popen(
                    command,
                    cwd=staged.staged_sdk,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=environment,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                job = _WindowsJob(process)
            except OSError as error:
                try:
                    _kill_unmanaged_process(
                        process,
                        timeout_seconds=shutdown_grace_seconds,
                    )
                except RenpySdkError as cleanup_error:
                    raise RenpySdkError(
                        f"无法启动临时 Ren'Py 进程：{error}；进程清理也失败：{cleanup_error}"
                    ) from cleanup_error
                raise RenpySdkError(f"无法启动临时 Ren'Py 进程：{error}") from error
            except RenpySdkError as error:
                try:
                    _kill_unmanaged_process(
                        process,
                        timeout_seconds=shutdown_grace_seconds,
                    )
                except RenpySdkError as cleanup_error:
                    raise RenpySdkError(
                        f"{error}；进程清理也失败：{cleanup_error}"
                    ) from cleanup_error
                raise

            try:
                evidence = _wait_for_display(
                    process,
                    timeout_seconds=timeout_seconds,
                    stability_seconds=stability_seconds,
                )
            except RenpySdkError as error:
                try:
                    _shutdown_process(
                        process,
                        job,
                        grace_seconds=shutdown_grace_seconds,
                    )
                except RenpySdkError as shutdown_error:
                    raise RenpySdkError(
                        f"{error}；进程清理也失败：{shutdown_error}"
                    ) from shutdown_error
                detail = _log_detail(stdout_path, stderr_path)
                suffix = f"\n{detail}" if detail else ""
                raise RenpySdkError(f"{error}{suffix}") from error

            shutdown_method = _shutdown_process(
                process,
                job,
                grace_seconds=shutdown_grace_seconds,
            )

        output_detail = _log_detail(stdout_path, stderr_path)
        runtime_error = _runtime_error_detail(staged.staged_project, output_detail)
        if runtime_error is not None:
            raise RenpySdkError(f"Ren'Py 虽出现窗口，但运行时报告了致命错误：\n{runtime_error}")

        return RenpyLaunchValidation(
            sdk_root=staged.sdk_root,
            version=version,
            language=language,
            source_file_count=staged.source_file_count,
            translation_file_count=staged.translation_file_count,
            window_title=evidence.title,
            client_width=evidence.client_width,
            client_height=evidence.client_height,
            shutdown_method=shutdown_method,
        )
