from __future__ import annotations

import hashlib
import json
import pickle
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from galtrans.adapters.renpy import (
    RENPY_PACKAGED_IMPORT_MANIFEST_SCHEMA_VERSION,
    RenpyImportAuthorization,
    RenpyPackagedImportError,
    extract_renpy_path,
    import_renpy_packaged_sources,
)

_AUTHORIZATION = RenpyImportAuthorization.USER_CONFIRMED_LOCAL_PROCESSING
_RPA_KEY = 0x42424242


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _packaged_root(root: Path) -> Path:
    project = root / "input"
    (project / "game").mkdir(parents=True)
    (project / "renpy").mkdir()
    (project / "lib").mkdir()
    (project / "Example Game.exe").write_bytes(b"self-authored launcher")
    return project


def _write_rpa3(
    path: Path,
    entries: dict[str, bytes],
    *,
    entry_prefix: bytes = b"",
    index_dict_type: type[dict[object, object]] = dict,
    index_list_type: type[list[object]] = list,
) -> None:
    index = index_dict_type()
    with path.open("wb") as stream:
        stream.write(b"RPA-3.0 0000000000000000 00000000\n")
        for name, content in entries.items():
            stream.write(b"Made with Ren'Py.")
            offset = stream.tell()
            stream.write(content)
            segments = index_list_type()
            segments.append(
                (offset ^ _RPA_KEY, len(content) ^ _RPA_KEY, entry_prefix)
            )
            index[name] = segments
        index_offset = stream.tell()
        stream.write(zlib.compress(pickle.dumps(index, pickle.HIGHEST_PROTOCOL)))
        stream.seek(0)
        stream.write(
            f"RPA-3.0 {index_offset:016x} {_RPA_KEY:08x}\n".encode("ascii")
        )


def _write_raw_index_rpa3(path: Path, index_pickle: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(b"RPA-3.0 0000000000000000 00000000\n")
        index_offset = stream.tell()
        stream.write(zlib.compress(index_pickle))
        stream.seek(0)
        stream.write(
            f"RPA-3.0 {index_offset:016x} {_RPA_KEY:08x}\n".encode("ascii")
        )


class _MaliciousIndexValue:
    def __init__(self, target: Path) -> None:
        self.target = target

    def __reduce__(self) -> tuple[object, tuple[str]]:
        expression = (
            "__import__('pathlib').Path("
            f"{str(self.target)!r}"
            ").write_text('bad')"
        )
        return eval, (expression,)


class RenpyPackagedImportTests(unittest.TestCase):
    def test_imports_plain_rpa3_sources_with_closed_audit_manifest(self) -> None:
        script = b'label start:\n    "Hello"\n'
        module = b"init python:\n    value = 1\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            archive = project / "game" / "archive.rpa"
            _write_rpa3(
                archive,
                {
                    "script.rpy": script,
                    "module/helpers.rpym": module,
                    "script.rpyc": b"compiled script marker",
                    "tl/schinese/script.rpy": b"existing translation",
                    "images/background.png": b"self-authored image marker",
                },
            )
            before = _snapshot(project)
            output = root / "imported"

            result = import_renpy_packaged_sources(
                project,
                output,
                authorization=_AUTHORIZATION,
            )
            second = import_renpy_packaged_sources(
                project,
                root / "imported-again",
                authorization=_AUTHORIZATION,
            )

            after = _snapshot(project)
            imported_script = (output / "game" / "script.rpy").read_bytes()
            imported_module = (output / "game" / "module" / "helpers.rpym").read_bytes()
            extracted_text = tuple(
                segment.source_text
                for extraction in extract_renpy_path(output)
                for segment in extraction.segments
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            staging_paths = tuple(root.glob(".galtrans-renpy-import-*"))

        self.assertEqual(after, before)
        self.assertEqual(imported_script, script)
        self.assertEqual(imported_module, module)
        self.assertEqual(extracted_text, ("Hello",))
        self.assertEqual(result.root, output)
        self.assertEqual(
            [path.relative_to(output).as_posix() for path in result.source_files],
            ["game/module/helpers.rpym", "game/script.rpy"],
        )
        self.assertEqual(staging_paths, ())
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "import_id",
                "importer",
                "authorization",
                "source_project_root",
                "compatibility_report_schema_version",
                "archives",
                "source_files",
                "ignored_compiled_scripts",
                "ignored_translation_files",
            },
        )
        self.assertEqual(
            manifest["schema_version"],
            RENPY_PACKAGED_IMPORT_MANIFEST_SCHEMA_VERSION,
        )
        self.assertTrue(manifest["import_id"].startswith("renpy_import_"))
        self.assertEqual(manifest["import_id"], second.manifest.import_id)
        self.assertEqual(manifest["archives"][0]["format"], "RPA-3.0")
        self.assertEqual(
            manifest["archives"][0]["sha256"],
            before["game/archive.rpa"][0],
        )
        self.assertEqual(manifest["archives"][0]["entry_count"], 5)
        self.assertEqual(manifest["archives"][0]["source_entry_count"], 2)
        self.assertEqual(
            manifest["ignored_compiled_scripts"],
            ["game/archive.rpa!script.rpyc"],
        )
        self.assertEqual(
            manifest["ignored_translation_files"],
            ["game/archive.rpa!tl/schinese/script.rpy"],
        )
        source_hashes = {
            item["relative_path"]: item["sha256"] for item in manifest["source_files"]
        }
        self.assertEqual(source_hashes["game/script.rpy"], hashlib.sha256(script).hexdigest())
        self.assertEqual(
            source_hashes["game/module/helpers.rpym"],
            hashlib.sha256(module).hexdigest(),
        )

    def test_accepts_only_the_explicit_authorization_enum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(project / "game" / "archive.rpa", {"script.rpy": b"pass\n"})
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "明确确认"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization="user_confirmed_local_processing",  # type: ignore[arg-type]
                )

            output_exists = output.exists()

        self.assertFalse(output_exists)

    def test_public_limits_can_only_be_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(project / "game" / "archive.rpa", {"script.rpy": b"pass\n"})
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "不能超过"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization=_AUTHORIZATION,
                    max_archives=129,
                )

            output_exists = output.exists()

        self.assertFalse(output_exists)

    def test_refuses_compiled_only_archives_without_decompiling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(
                project / "game" / "archive.rpa",
                {"script.rpyc": b"not real compiled data"},
            )
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "不反编译"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization=_AUTHORIZATION,
                )

            output_exists = output.exists()

        self.assertFalse(output_exists)

    def test_refuses_unknown_or_custom_archive_header_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            archive = project / "game" / "archive.rpa"
            _write_rpa3(archive, {"script.rpy": b"pass\n"})
            data = archive.read_bytes()
            archive.write_bytes(b"ALT-1.0 " + data[8:])
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "RPA-3.0"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization=_AUTHORIZATION,
                )

            output_exists = output.exists()

        self.assertFalse(output_exists)

    def test_refuses_compatibility_prefix_and_compressed_index_tail(self) -> None:
        cases = ("prefix", "tail")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    project = _packaged_root(root)
                    archive = project / "game" / "archive.rpa"
                    _write_rpa3(
                        archive,
                        {"script.rpy": b"pass\n"},
                        entry_prefix=b"legacy" if case == "prefix" else b"",
                    )
                    if case == "tail":
                        archive.write_bytes(archive.read_bytes() + b"trailing")
                    output = root / "imported"

                    with self.assertRaises(RenpyPackagedImportError):
                        import_renpy_packaged_sources(
                            project,
                            output,
                            authorization=_AUTHORIZATION,
                        )

                    self.assertFalse(output.exists())

    def test_restricted_index_never_executes_pickle_globals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            sentinel = root / "should-not-exist"
            malicious_index = pickle.dumps(
                {"script.rpy": _MaliciousIndexValue(sentinel)},
                pickle.HIGHEST_PROTOCOL,
            )
            _write_raw_index_rpa3(project / "game" / "archive.rpa", malicious_index)
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "不允许"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization=_AUTHORIZATION,
                )

            sentinel_exists = sentinel.exists()
            output_exists = output.exists()

        self.assertFalse(sentinel_exists)
        self.assertFalse(output_exists)

    def test_accepts_official_renpy_revertable_index_containers_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            renpy_module = types.ModuleType("renpy")
            revertable_module = types.ModuleType("renpy.revertable")
            revertable_dict = type(
                "RevertableDict",
                (dict,),
                {"__module__": "renpy.revertable"},
            )
            revertable_list = type(
                "RevertableList",
                (list,),
                {"__module__": "renpy.revertable"},
            )
            revertable_module.RevertableDict = revertable_dict  # type: ignore[attr-defined]
            revertable_module.RevertableList = revertable_list  # type: ignore[attr-defined]
            renpy_module.revertable = revertable_module  # type: ignore[attr-defined]
            with patch.dict(
                sys.modules,
                {"renpy": renpy_module, "renpy.revertable": revertable_module},
            ):
                _write_rpa3(
                    project / "game" / "archive.rpa",
                    {"script.rpy": b"label start:\n    pass\n"},
                    index_dict_type=revertable_dict,
                    index_list_type=revertable_list,
                )
            output = root / "imported"

            result = import_renpy_packaged_sources(
                project,
                output,
                authorization=_AUTHORIZATION,
            )

            content = result.source_files[0].read_bytes()

        self.assertEqual(content, b"label start:\n    pass\n")

    def test_refuses_unsafe_paths_and_bounded_source_overflow(self) -> None:
        cases = (
            ({"../escape.rpy": b"pass\n"}, {}, "安全相对路径"),
            (
                {"script.rpy": b"label start:\n    pass\n"},
                {"max_total_source_bytes": 4},
                "总量超过",
            ),
            (
                {"script.rpy": b"label start:\n    pass\n"},
                {"max_archive_bytes": 1},
                "单个 RPA 安全上限",
            ),
        )
        for entries, limits, message in cases:
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    project = _packaged_root(root)
                    _write_rpa3(project / "game" / "archive.rpa", entries)
                    output = root / "imported"

                    with self.assertRaisesRegex(RenpyPackagedImportError, message):
                        import_renpy_packaged_sources(
                            project,
                            output,
                            authorization=_AUTHORIZATION,
                            **limits,
                        )

                    self.assertFalse(output.exists())
                    self.assertFalse((root / "escape.rpy").exists())

    def test_refuses_windows_path_collisions_across_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(
                project / "game" / "a.rpa",
                {"Script.rpy": b"label first:\n    pass\n"},
            )
            _write_rpa3(
                project / "game" / "b.rpa",
                {"script.rpy": b"label second:\n    pass\n"},
            )
            output = root / "imported"

            with self.assertRaisesRegex(RenpyPackagedImportError, "同一 Windows 路径"):
                import_renpy_packaged_sources(
                    project,
                    output,
                    authorization=_AUTHORIZATION,
                )

            output_exists = output.exists()

        self.assertFalse(output_exists)

    def test_failure_after_staging_cleans_temporary_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(project / "game" / "archive.rpa", {"script.rpy": b"pass\n"})
            output = root / "imported"

            with patch(
                "galtrans.adapters.renpy.packaged_import._verify_archive_snapshots",
                side_effect=RenpyPackagedImportError("simulated input change"),
            ):
                with self.assertRaisesRegex(RenpyPackagedImportError, "simulated"):
                    import_renpy_packaged_sources(
                        project,
                        output,
                        authorization=_AUTHORIZATION,
                    )

            output_exists = output.exists()
            staging_paths = tuple(root.glob(".galtrans-renpy-import-*"))

        self.assertFalse(output_exists)
        self.assertEqual(staging_paths, ())

    def test_refuses_existing_or_input_overlapping_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = _packaged_root(root)
            _write_rpa3(project / "game" / "archive.rpa", {"script.rpy": b"pass\n"})
            existing = root / "existing"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
                import_renpy_packaged_sources(
                    project,
                    existing,
                    authorization=_AUTHORIZATION,
                )
            with self.assertRaisesRegex(RenpyPackagedImportError, "不得与原游戏"):
                import_renpy_packaged_sources(
                    project,
                    project / "imported",
                    authorization=_AUTHORIZATION,
                )

            sentinel_contents = sentinel.read_text(encoding="utf-8")
            overlapping_exists = (project / "imported").exists()

        self.assertEqual(sentinel_contents, "keep")
        self.assertFalse(overlapping_exists)


if __name__ == "__main__":
    unittest.main()
