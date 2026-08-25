from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from galtrans import __version__
from galtrans.adapters.renpy import (
    RenpyExportError,
    RenpyProposalPreparationError,
    RenpySdkError,
    crosscheck_renpy_sdk,
    extract_renpy_path,
    validate_renpy_export,
    validate_renpy_launch,
)
from galtrans.automated import (
    AutomatedRenpyTranslationError,
    AutomatedRenpyTranslationResult,
    default_automated_workspace,
    run_automated_renpy_translation,
)
from galtrans.pipeline import TranslationExecutionError
from galtrans.providers import (
    OpenAICompatibleChatBackend,
    OpenAICompatibleProviderError,
)
from galtrans.scanner import scan_project
from galtrans.storage import TranslationStorageError
from galtrans.translation import (
    TranslationSchemaError,
    TranslationStateError,
    TranslationValidationError,
)

_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="galtrans",
        description="视觉小说安全汉化工作流（早期开发版）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="检查当前运行环境")

    scan_parser = commands.add_parser("scan", help="只读扫描游戏或脚本目录")
    scan_parser.add_argument("path", type=Path, help="要扫描的目录")
    scan_parser.add_argument("--json", action="store_true", help="输出 JSON")

    extract_parser = commands.add_parser("extract-renpy", help="从 Ren'Py 源脚本或项目目录提取文本")
    extract_parser.add_argument("path", type=Path, help=".rpy/.rpym 文件或项目目录")
    extract_parser.add_argument("--output", "-o", type=Path, help="写入新的 JSONL 文件")

    sdk_parser = commands.add_parser(
        "check-renpy-sdk",
        help="在临时源文件副本上用官方 SDK 交叉验证 Ren'Py 提取结果",
    )
    sdk_parser.add_argument("sdk", type=Path, help="Ren'Py SDK 目录或 renpy.exe")
    sdk_parser.add_argument("project", type=Path, help="包含 game 目录的 Ren'Py 源项目")
    sdk_parser.add_argument("--language", default="schinese", help="官方模板的语言名")
    sdk_parser.add_argument("--json", action="store_true", help="输出 JSON")

    validation_parser = commands.add_parser(
        "validate-renpy-export",
        help="在可写临时 SDK 和项目副本上 lint 并编译已导出的翻译目录",
    )
    validation_parser.add_argument("sdk", type=Path, help="Ren'Py SDK 目录或 renpy.exe")
    validation_parser.add_argument("project", type=Path, help="包含 game 目录的 Ren'Py 源项目")
    validation_parser.add_argument(
        "export", type=Path, help="包含 game/tl/<language> 的独立导出根目录"
    )
    validation_parser.add_argument("--language", default="schinese", help="要验证的语言名")
    validation_parser.add_argument("--json", action="store_true", help="输出 JSON")

    launch_parser = commands.add_parser(
        "validate-renpy-launch",
        help="在可写临时副本中启动 Ren'Py 并验证基础窗口显示",
    )
    launch_parser.add_argument("sdk", type=Path, help="Ren'Py SDK 目录或 renpy.exe")
    launch_parser.add_argument("project", type=Path, help="包含 game 目录的 Ren'Py 源项目")
    launch_parser.add_argument(
        "export", type=Path, help="包含 game/tl/<language> 的独立导出根目录"
    )
    launch_parser.add_argument("--language", default="schinese", help="要启动的语言名")
    launch_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="等待稳定可见窗口的秒数（默认 30）",
    )
    launch_parser.add_argument("--json", action="store_true", help="输出 JSON")

    translate_parser = commands.add_parser(
        "translate-renpy",
        help="通过 OpenAI 兼容 Provider 自动翻译并验证 source-only Ren'Py 项目",
    )
    translate_parser.add_argument("sdk", type=Path, help="Ren'Py SDK 目录或 renpy.exe")
    translate_parser.add_argument("project", type=Path, help="包含 game 目录的 Ren'Py 源项目")
    translate_parser.add_argument("output", type=Path, help="必须尚不存在的独立输出目录")
    translate_parser.add_argument(
        "--workspace",
        type=Path,
        help="输入项目之外的任务工作区；默认位于输出目录旁",
    )
    translate_parser.add_argument(
        "--endpoint",
        help="Chat Completions URL；也可用 GALTRANS_API_ENDPOINT 环境变量",
    )
    translate_parser.add_argument(
        "--model",
        help="模型名；也可用 GALTRANS_MODEL 环境变量",
    )
    translate_parser.add_argument(
        "--api-key-env",
        default="GALTRANS_API_KEY",
        help="保存 API key 的环境变量名（默认 GALTRANS_API_KEY）",
    )
    translate_parser.add_argument("--source-language", default="ja", help="源语言")
    translate_parser.add_argument(
        "--language", default="schinese", help="目标语言和 Ren'Py 语言名"
    )
    translate_parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="每次 Provider 请求的文本段数量（默认 8）",
    )
    translate_parser.add_argument(
        "--provider-timeout",
        type=float,
        default=120.0,
        help="单次 Provider 请求超时秒数（默认 120）",
    )
    translate_parser.add_argument(
        "--sdk-timeout",
        type=float,
        default=60.0,
        help="单次 Ren'Py SDK 命令超时秒数（默认 60）",
    )
    translate_parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Provider 明确失败时的总尝试次数（默认 2）",
    )
    translate_parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def _doctor() -> int:
    print(f"GalTrans: {__version__}")
    print(f"Python:   {platform.python_version()} ({sys.executable})")
    print(f"System:   {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"SQLite:   {sqlite3.sqlite_version}")
    print("Status:   OK")
    return 0


def _scan(path: Path, *, as_json: bool) -> int:
    try:
        result = scan_project(path)
    except (FileNotFoundError, NotADirectoryError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"扫描目录：{result.root}")
    print(f"发现文本：{len(result.files)} 个；警告：{len(result.warnings)} 个")
    for source in result.files:
        engine = source.engine_hint or "通用"
        print(
            f"  {source.relative_path} | {engine} | {source.encoding} | "
            f"{source.line_count} 行 | {source.size_bytes} 字节"
        )
    for warning in result.warnings:
        print(f"  警告 {warning.relative_path}：{warning.message}", file=sys.stderr)
    return 0


def _extract_renpy(path: Path, *, output: Path | None) -> int:
    try:
        results = extract_renpy_path(path)
    except (FileNotFoundError, IsADirectoryError, ValueError, UnicodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    jsonl = "".join(
        json.dumps(segment.to_dict(), ensure_ascii=False) + "\n"
        for result in results
        for segment in result.segments
    )
    if output is None:
        print(jsonl, end="")
        destination = "标准输出"
    else:
        resolved_output = output.expanduser().resolve()
        try:
            resolved_output.parent.mkdir(parents=True, exist_ok=True)
            with resolved_output.open("x", encoding="utf-8", newline="\n") as file:
                file.write(jsonl)
        except FileExistsError:
            print(f"错误：输出文件已存在，拒绝覆盖：{resolved_output}", file=sys.stderr)
            return 3
        destination = str(resolved_output)

    summary_stream = sys.stderr if output is None else sys.stdout
    segment_count = sum(len(result.segments) for result in results)
    character_count = sum(len(result.characters) for result in results)
    warning_count = sum(len(result.warnings) for result in results)
    print(
        f"已从 {len(results)} 个文件提取 {segment_count} 条文本，"
        f"识别 {character_count} 个角色定义，警告 {warning_count} 条；输出：{destination}",
        file=summary_stream,
    )
    for result in results:
        for warning in result.warnings:
            print(
                f"  {result.source_file} 第 {warning.line_number} 行：{warning.message}",
                file=sys.stderr,
            )
    return 0


def _check_renpy_sdk(
    sdk: Path,
    project: Path,
    *,
    language: str,
    as_json: bool,
) -> int:
    try:
        result = crosscheck_renpy_sdk(sdk, project, language=language)
    except (FileNotFoundError, NotADirectoryError, UnicodeError, RenpySdkError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Ren'Py SDK：{result.version} ({result.executable})")
        print(
            f"源脚本：{result.source_file_count} 个；官方模板："
            f"{result.template_file_count} 个；lint：命令完成"
        )
        print(
            "对话/旁白："
            f"GalTrans {result.galtrans_dialogue_count} / "
            f"Ren'Py {result.official_dialogue_count}"
        )
        print(
            "菜单字符串："
            f"GalTrans {result.galtrans_string_count} / "
            f"Ren'Py {result.official_string_count}"
        )
        total_segments = result.galtrans_dialogue_count + result.galtrans_string_count
        print(f"逐条映射：{result.mapped_segment_count} / {total_segments}")
        for warning in result.template_warnings:
            print(f"  模板警告：{warning}", file=sys.stderr)
        print("交叉验证：" + ("一致" if result.matches else "不一致"))
    return 0 if result.matches else 4


def _validate_renpy_export(
    sdk: Path,
    project: Path,
    export: Path,
    *,
    language: str,
    as_json: bool,
) -> int:
    try:
        result = validate_renpy_export(
            sdk,
            project,
            export,
            language=language,
        )
    except (FileNotFoundError, NotADirectoryError, UnicodeError, RenpySdkError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Ren'Py SDK：{result.version} ({result.sdk_root})")
        print(
            f"临时副本：源脚本 {result.source_file_count} 个；"
            f"翻译文件 {result.translation_file_count} 个"
        )
        print(
            f"lint：命令完成；compile："
            f"{result.compiled_file_count} 个项目脚本已生成编译文件"
        )
        print("导出验证：通过")
    return 0


def _validate_renpy_launch(
    sdk: Path,
    project: Path,
    export: Path,
    *,
    language: str,
    timeout_seconds: float,
    as_json: bool,
) -> int:
    try:
        result = validate_renpy_launch(
            sdk,
            project,
            export,
            language=language,
            timeout_seconds=timeout_seconds,
        )
    except (
        FileNotFoundError,
        NotADirectoryError,
        UnicodeError,
        ValueError,
        RenpySdkError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Ren'Py SDK：{result.version} ({result.sdk_root})")
        print(
            f"临时副本：源脚本 {result.source_file_count} 个；"
            f"翻译文件 {result.translation_file_count} 个；语言 {result.language}"
        )
        title = result.window_title or "（无标题）"
        print(
            f"显示证据：{title} | {result.client_width} x "
            f"{result.client_height} 客户区"
        )
        print(f"进程收尾：{result.shutdown_method}；启动显示验证：通过")
    return 0


def _automatic_workspace(output: Path, workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace
    return default_automated_workspace(output)


def _translate_renpy(
    sdk: Path,
    project: Path,
    output: Path,
    *,
    workspace: Path | None,
    endpoint: str | None,
    model: str | None,
    api_key_environment: str,
    source_language: str,
    target_language: str,
    batch_size: int,
    provider_timeout_seconds: float,
    sdk_timeout_seconds: float,
    max_attempts: int,
    as_json: bool,
) -> int:
    resolved_endpoint = endpoint or os.environ.get("GALTRANS_API_ENDPOINT")
    resolved_model = model or os.environ.get("GALTRANS_MODEL")
    if not resolved_endpoint:
        print(
            "错误：请用 --endpoint 或 GALTRANS_API_ENDPOINT 配置 Provider URL",
            file=sys.stderr,
        )
        return 2
    if not resolved_model:
        print(
            "错误：请用 --model 或 GALTRANS_MODEL 配置模型名",
            file=sys.stderr,
        )
        return 2
    if _ENVIRONMENT_NAME_RE.fullmatch(api_key_environment) is None:
        print("错误：--api-key-env 不是有效的环境变量名", file=sys.stderr)
        return 2
    api_key = os.environ.get(api_key_environment)
    if not api_key:
        print(
            f"错误：环境变量 {api_key_environment} 没有配置 API key",
            file=sys.stderr,
        )
        return 2

    try:
        backend = OpenAICompatibleChatBackend(
            endpoint=resolved_endpoint,
            model=resolved_model,
            api_key=api_key,
            timeout_seconds=provider_timeout_seconds,
        )
        os.environ.pop(api_key_environment, None)
        try:
            result = run_automated_renpy_translation(
                sdk,
                project,
                output,
                _automatic_workspace(output, workspace),
                backend,
                backend_identity=backend.identity,
                source_language=source_language,
                target_language=target_language,
                batch_size=batch_size,
                max_definitive_attempts=max_attempts,
                sdk_timeout_seconds=sdk_timeout_seconds,
            )
        finally:
            os.environ[api_key_environment] = api_key
    except FileExistsError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 3
    except (
        FileNotFoundError,
        NotADirectoryError,
        OSError,
        UnicodeError,
        AutomatedRenpyTranslationError,
        OpenAICompatibleProviderError,
        RenpyExportError,
        RenpyProposalPreparationError,
        RenpySdkError,
        TranslationExecutionError,
        TranslationSchemaError,
        TranslationStateError,
        TranslationStorageError,
        TranslationValidationError,
    ) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    _print_automated_result(result, as_json=as_json)
    return 0


def _print_automated_result(
    result: AutomatedRenpyTranslationResult,
    *,
    as_json: bool,
) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    print(
        f"自动翻译：{result.segment_count} 条文本，"
        f"{result.batch_count} 个批次；任务 {result.task_id}"
    )
    print(
        f"质量检查：{result.quality_outcome.value}；"
        f"低置信度 {len(result.low_confidence_segment_ids)} 条"
    )
    print(f"独立输出：{result.output_root}")
    print(f"翻译文件：{len(result.translation_files)} 个")
    print(f"Ren'Py {result.sdk_version} lint 与 compile：通过")
    print(f"可恢复工作区：{result.workspace_root}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "scan":
        return _scan(args.path, as_json=args.json)
    if args.command == "extract-renpy":
        return _extract_renpy(args.path, output=args.output)
    if args.command == "check-renpy-sdk":
        return _check_renpy_sdk(
            args.sdk,
            args.project,
            language=args.language,
            as_json=args.json,
        )
    if args.command == "validate-renpy-export":
        return _validate_renpy_export(
            args.sdk,
            args.project,
            args.export,
            language=args.language,
            as_json=args.json,
        )
    if args.command == "validate-renpy-launch":
        return _validate_renpy_launch(
            args.sdk,
            args.project,
            args.export,
            language=args.language,
            timeout_seconds=args.timeout,
            as_json=args.json,
        )
    if args.command == "translate-renpy":
        return _translate_renpy(
            args.sdk,
            args.project,
            args.output,
            workspace=args.workspace,
            endpoint=args.endpoint,
            model=args.model,
            api_key_environment=args.api_key_env,
            source_language=args.source_language,
            target_language=args.language,
            batch_size=args.batch_size,
            provider_timeout_seconds=args.provider_timeout,
            sdk_timeout_seconds=args.sdk_timeout,
            max_attempts=args.max_attempts,
            as_json=args.json,
        )
    raise AssertionError(f"未处理的命令：{args.command}")
