from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from galtrans import __version__
from galtrans.adapters.renpy import RenpySdkError, crosscheck_renpy_sdk, extract_renpy_path
from galtrans.scanner import scan_project


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
    raise AssertionError(f"未处理的命令：{args.command}")
