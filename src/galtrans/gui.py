from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from galtrans import __version__
from galtrans.automated import (
    AutomatedRenpyTranslationProgress,
    AutomatedRenpyTranslationResult,
    AutomatedRenpyTranslationStage,
    ProgressCallback,
    default_automated_workspace,
    run_automated_renpy_translation,
)
from galtrans.providers import OpenAICompatibleChatBackend


@dataclass(frozen=True, slots=True)
class PlayerTranslationRequest:
    sdk_path: Path
    project_path: Path
    output_path: Path
    endpoint: str
    model: str
    api_key: str
    workspace_path: Path | None = None
    source_language: str = "ja"
    target_language: str = "schinese"
    batch_size: int = 8
    provider_timeout_seconds: float = 120.0
    sdk_timeout_seconds: float = 60.0
    max_definitive_attempts: int = 2

    @property
    def resolved_workspace_path(self) -> Path:
        return self.workspace_path or default_automated_workspace(self.output_path)


def execute_player_translation(
    request: PlayerTranslationRequest,
    progress_callback: ProgressCallback | None = None,
) -> AutomatedRenpyTranslationResult:
    """Run the existing automatic workflow without putting credentials in the environment."""
    backend = OpenAICompatibleChatBackend(
        endpoint=request.endpoint,
        model=request.model,
        api_key=request.api_key,
        timeout_seconds=request.provider_timeout_seconds,
    )
    return run_automated_renpy_translation(
        request.sdk_path,
        request.project_path,
        request.output_path,
        request.resolved_workspace_path,
        backend,
        backend_identity=backend.identity,
        source_language=request.source_language,
        target_language=request.target_language,
        batch_size=request.batch_size,
        max_definitive_attempts=request.max_definitive_attempts,
        sdk_timeout_seconds=request.sdk_timeout_seconds,
        progress_callback=progress_callback,
    )


class PlayerWorkerEventKind(StrEnum):
    PROGRESS = "progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlayerWorkerEvent:
    kind: PlayerWorkerEventKind
    progress: AutomatedRenpyTranslationProgress | None = None
    result: AutomatedRenpyTranslationResult | None = None
    error_message: str | None = None


PlayerExecutor = Callable[
    [PlayerTranslationRequest, ProgressCallback | None],
    AutomatedRenpyTranslationResult,
]


def _redacted_error_message(error: Exception, api_key: str) -> str:
    message = str(error) or type(error).__name__
    if api_key:
        message = message.replace(api_key, "[凭据已隐藏]")
    return message


class PlayerTranslationWorker(threading.Thread):
    """One background translation run that communicates only through a queue."""

    def __init__(
        self,
        request: PlayerTranslationRequest,
        events: queue.Queue[PlayerWorkerEvent],
        executor: PlayerExecutor = execute_player_translation,
    ) -> None:
        super().__init__(name="galtrans-player-translation", daemon=True)
        self._request: PlayerTranslationRequest | None = request
        self._events = events
        self._executor = executor

    def run(self) -> None:
        request = self._request
        if request is None:
            return
        try:
            result = self._executor(request, self._report_progress)
        except Exception as error:
            self._events.put(
                PlayerWorkerEvent(
                    kind=PlayerWorkerEventKind.FAILED,
                    error_message=_redacted_error_message(error, request.api_key),
                )
            )
        else:
            self._events.put(
                PlayerWorkerEvent(
                    kind=PlayerWorkerEventKind.SUCCEEDED,
                    result=result,
                )
            )
        finally:
            self._request = None

    def _report_progress(self, progress: AutomatedRenpyTranslationProgress) -> None:
        self._events.put(
            PlayerWorkerEvent(
                kind=PlayerWorkerEventKind.PROGRESS,
                progress=progress,
            )
        )


WorkerFactory = Callable[
    [PlayerTranslationRequest, queue.Queue[PlayerWorkerEvent]],
    PlayerTranslationWorker,
]


class GalTransApp:
    def __init__(
        self,
        root: tk.Tk,
        worker_factory: WorkerFactory = PlayerTranslationWorker,
    ) -> None:
        self.root = root
        self._worker_factory = worker_factory
        self._events: queue.Queue[PlayerWorkerEvent] = queue.Queue()
        self._worker: PlayerTranslationWorker | None = None
        self._running = False
        self._config_widgets: list[tk.Widget] = []

        self.sdk_path = tk.StringVar()
        self.project_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.endpoint = tk.StringVar(value=os.environ.get("GALTRANS_API_ENDPOINT", ""))
        self.model = tk.StringVar(value=os.environ.get("GALTRANS_MODEL", ""))
        self.api_key = tk.StringVar()
        self.status = tk.StringVar(value="请选择 Ren'Py SDK、源项目和全新输出位置。")

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_layout(self) -> None:
        self.root.title(f"GalTrans {__version__} - 自动汉化")
        self.root.minsize(720, 620)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=18)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(9, weight=1)

        title = ttk.Label(main, text="Ren'Py 自动汉化", font=("Microsoft YaHei UI", 16, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        subtitle = ttk.Label(
            main,
            text="选择带源脚本的项目。原项目保持只读，输出只会发布到全新目录。",
        )
        subtitle.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

        self._path_row(
            main,
            row=2,
            label="Ren'Py SDK",
            variable=self.sdk_path,
            browse=self._browse_sdk,
        )
        self._path_row(
            main,
            row=3,
            label="源项目",
            variable=self.project_path,
            browse=self._browse_project,
        )
        self._path_row(
            main,
            row=4,
            label="全新输出",
            variable=self.output_path,
            browse=self._browse_output_parent,
        )
        self._text_row(main, row=5, label="翻译服务 URL", variable=self.endpoint)
        self._text_row(main, row=6, label="模型", variable=self.model)
        self._text_row(
            main,
            row=7,
            label="API key",
            variable=self.api_key,
            secret=True,
        )

        privacy = ttk.Label(
            main,
            text="API key 只用于本次内存中的 HTTPS 请求；开始后输入框会立即清空。",
            foreground="#555555",
        )
        privacy.grid(row=8, column=1, columnspan=2, sticky="w", pady=(0, 10))

        progress_frame = ttk.LabelFrame(main, text="进度", padding=10)
        progress_frame.grid(row=9, column=0, columnspan=3, sticky="nsew")
        progress_frame.columnconfigure(0, weight=1)
        progress_frame.rowconfigure(2, weight=1)

        ttk.Label(progress_frame, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.log = tk.Text(progress_frame, height=9, wrap="word", state="disabled")
        self.log.grid(row=2, column=0, sticky="nsew")

        self.start_button = ttk.Button(
            main,
            text="开始自动汉化",
            command=self._start,
        )
        self.start_button.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(14, 0))

    def _path_row(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        button = ttk.Button(parent, text="选择…", command=browse)
        button.grid(row=row, column=2, sticky="ew", padx=(8, 0), pady=4)
        self._config_widgets.extend((entry, button))

    def _text_row(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        label: str,
        variable: tk.StringVar,
        secret: bool = False,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(parent, textvariable=variable, show="*" if secret else "")
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self._config_widgets.append(entry)

    def _browse_sdk(self) -> None:
        selected = filedialog.askdirectory(title="选择 Ren'Py SDK 目录")
        if selected:
            self.sdk_path.set(selected)

    def _browse_project(self) -> None:
        selected = filedialog.askdirectory(title="选择带 game 目录的 Ren'Py 源项目")
        if selected:
            self.project_path.set(selected)

    def _browse_output_parent(self) -> None:
        selected = filedialog.askdirectory(title="选择汉化输出的父目录")
        if not selected:
            return
        project_name = Path(self.project_path.get().strip()).name or "renpy-game"
        self.output_path.set(str(Path(selected) / f"{project_name}-schinese"))

    def _collect_request(self) -> PlayerTranslationRequest:
        values = {
            "Ren'Py SDK": self.sdk_path.get().strip(),
            "源项目": self.project_path.get().strip(),
            "全新输出": self.output_path.get().strip(),
            "翻译服务 URL": self.endpoint.get().strip(),
            "模型": self.model.get().strip(),
        }
        missing = [name for name, value in values.items() if not value]
        api_key = self.api_key.get()
        if not api_key:
            missing.append("API key")
        if missing:
            raise ValueError("请填写：" + "、".join(missing))

        sdk_path = Path(values["Ren'Py SDK"])
        project_path = Path(values["源项目"])
        output_path = Path(values["全新输出"])
        if not sdk_path.exists():
            raise ValueError(f"Ren'Py SDK 路径不存在：{sdk_path}")
        if not project_path.is_dir():
            raise ValueError(f"源项目目录不存在：{project_path}")
        if output_path.exists() or output_path.is_symlink():
            raise ValueError(f"全新输出路径必须尚不存在：{output_path}")
        return PlayerTranslationRequest(
            sdk_path=sdk_path,
            project_path=project_path,
            output_path=output_path,
            endpoint=values["翻译服务 URL"],
            model=values["模型"],
            api_key=api_key,
        )

    def _start(self) -> None:
        try:
            request = self._collect_request()
        except ValueError as error:
            messagebox.showerror("无法开始", str(error), parent=self.root)
            return

        self.api_key.set("")
        self._set_running(True)
        self.progress_bar.configure(value=0)
        self._replace_log("开始新的自动汉化任务。")
        self.status.set("正在启动…")
        self._worker = self._worker_factory(request, self._events)
        self._worker.start()
        self.root.after(100, self._poll_events)

    def _poll_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if event.kind is PlayerWorkerEventKind.PROGRESS and event.progress is not None:
                self._show_progress(event.progress)
            elif event.kind is PlayerWorkerEventKind.SUCCEEDED and event.result is not None:
                self._finish_success(event.result)
            elif event.kind is PlayerWorkerEventKind.FAILED:
                self._finish_failure(event.error_message or "未知错误")
        if self._running:
            self.root.after(100, self._poll_events)

    def _show_progress(self, progress: AutomatedRenpyTranslationProgress) -> None:
        self.status.set(progress.message)
        self.progress_bar.configure(value=self._progress_percent(progress))
        self._append_log(progress.message)

    @staticmethod
    def _progress_percent(progress: AutomatedRenpyTranslationProgress) -> int:
        if (
            progress.stage is AutomatedRenpyTranslationStage.TRANSLATING
            and progress.completed_batches is not None
            and progress.total_batches
        ):
            return 25 + round(45 * progress.completed_batches / progress.total_batches)
        return {
            AutomatedRenpyTranslationStage.PREFLIGHT: 5,
            AutomatedRenpyTranslationStage.EXTRACTING: 12,
            AutomatedRenpyTranslationStage.SDK_CROSSCHECK: 20,
            AutomatedRenpyTranslationStage.TRANSLATING: 25,
            AutomatedRenpyTranslationStage.QUALITY_CHECK: 74,
            AutomatedRenpyTranslationStage.RENDERING: 80,
            AutomatedRenpyTranslationStage.VALIDATING_EXPORT: 88,
            AutomatedRenpyTranslationStage.PUBLISHING: 96,
            AutomatedRenpyTranslationStage.COMPLETED: 100,
        }[progress.stage]

    def _finish_success(self, result: AutomatedRenpyTranslationResult) -> None:
        self.progress_bar.configure(value=100)
        low_confidence = len(result.low_confidence_segment_ids)
        self.status.set("完成：输出已经通过 Ren'Py lint 与 compile。")
        self._append_log(
            f"完成：{result.segment_count} 条文本，低置信度 {low_confidence} 条。"
        )
        self._append_log(f"输出目录：{result.output_root}")
        self._set_running(False)
        self._worker = None
        messagebox.showinfo(
            "自动汉化完成",
            f"已生成并验证新输出目录：\n{result.output_root}\n\n"
            f"低置信度提示：{low_confidence} 条",
            parent=self.root,
        )

    def _finish_failure(self, error_message: str) -> None:
        self.status.set("任务停止，未发布未经验证的新输出。")
        self._append_log("失败：" + error_message)
        self._set_running(False)
        self._worker = None
        messagebox.showerror(
            "自动汉化未完成",
            error_message,
            parent=self.root,
        )

    def _set_running(self, running: bool) -> None:
        self._running = running
        state = "disabled" if running else "normal"
        for widget in self._config_widgets:
            widget.configure(state=state)
        self.start_button.configure(state=state)

    def _replace_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", message + "\n")
        self.log.configure(state="disabled")

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self._running:
            messagebox.showwarning(
                "任务仍在运行",
                "请等待当前批次完成。任务状态会持续保存，可在失败后重新开始恢复。",
                parent=self.root,
            )
            return
        self.root.destroy()


def gui_environment_version() -> str:
    interpreter = tk.Tcl()
    return str(interpreter.eval("info patchlevel"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="galtrans-gui", description="GalTrans 玩家界面")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查 Tkinter 环境，不打开窗口",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.check:
        print(f"GalTrans GUI {__version__}: Tk/Tcl {gui_environment_version()} OK")
        return 0
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"无法启动 GalTrans 图形界面：{error}", file=sys.stderr)
        return 2
    GalTransApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
