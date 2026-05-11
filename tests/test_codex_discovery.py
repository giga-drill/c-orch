from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from c_orch import codex_discovery
from c_orch.codex_discovery import inspect_codex_environment


class CodexDiscoveryTests(unittest.TestCase):
    def test_resolution_order_selects_first_usable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = _write_fake_codex(
                root / "explicit-codex",
                models={"models": [{"id": "gpt-5.4"}]},
            )
            env = _write_fake_codex(
                root / "env-codex",
                models={"models": [{"id": "gpt-5.3-codex"}]},
            )
            app = _write_fake_codex(
                root / "app-codex",
                models={"models": [{"id": "gpt-5.4-mini"}]},
            )
            path_a_dir = root / "path-a"
            path_b_dir = root / "path-b"
            path_a_dir.mkdir()
            path_b_dir.mkdir()
            path_a = _write_fake_codex(
                path_a_dir / "codex",
                models={"models": [{"id": "gpt-5.5"}]},
            )
            path_b = _write_fake_codex(
                path_b_dir / "codex",
                models={"models": [{"id": "gpt-5.3-codex-spark"}]},
            )

            with _isolated_discovery_env(env, app, os.pathsep.join([str(path_a_dir), str(path_b_dir)])):
                report = inspect_codex_environment(explicit_codex_bin=explicit)

        self.assertEqual(
            [candidate.path for candidate in report.candidates],
            [explicit, env, app, path_a, path_b],
        )
        self.assertIsNotNone(report.selected)
        self.assertEqual(report.selected.path, explicit)
        self.assertTrue(report.selected.has_model("gpt-5.4"))
        self.assertFalse(report.has_model("gpt-5.5"))

    def test_bad_candidates_keep_errors_and_do_not_block_later_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            broken = _write_broken_codex(root / "broken-codex")
            app = root / "missing-app-codex"
            path_dir = root / "path"
            path_dir.mkdir()
            good = _write_fake_codex(
                path_dir / "codex",
                version="codex 2.0.0",
                models={"models": [{"id": "gpt-5.4"}]},
            )

            with _isolated_discovery_env(None, str(app), str(path_dir)):
                report = inspect_codex_environment(explicit_codex_bin=broken)

        self.assertEqual(len(report.candidates), 3)
        self.assertFalse(report.candidates[0].usable)
        self.assertIn("--version: exit 7", report.candidates[0].error or "")
        self.assertIn("debug models: invalid JSON", report.candidates[0].error or "")
        self.assertFalse(report.candidates[1].usable)
        self.assertIn("not found", report.candidates[1].error or "")
        self.assertIsNotNone(report.selected)
        self.assertEqual(report.selected.path, good)
        self.assertEqual(report.selected.version, "codex 2.0.0")

    def test_nested_model_json_and_to_dict_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = root / "missing-app-codex"
            path_dir = root / "path"
            path_dir.mkdir()
            codex = _write_fake_codex(
                path_dir / "codex",
                models={
                    "data": [
                        {"id": "gpt-5.4-mini"},
                        {"slug": "gpt-5.3-codex-spark"},
                        {"model": "gpt-5.5"},
                        {"slug": "gpt-5.5-codex-preview"},
                    ],
                    "notes": "ignore prose mentioning gpt-5.2-codex",
                },
            )

            with _isolated_discovery_env(None, str(app), str(path_dir)):
                report = inspect_codex_environment()

        self.assertIsNotNone(report.selected)
        self.assertEqual(report.selected.path, codex)
        self.assertEqual(
            report.selected.interesting_models,
            (
                "gpt-5.4-mini",
                "gpt-5.3-codex-spark",
                "gpt-5.5",
                "gpt-5.5-codex-preview",
            ),
        )
        self.assertEqual(report.to_dict()["selected"]["path"], codex)
        self.assertEqual(report.candidates[-1].to_dict()["interesting_models"][0], "gpt-5.4-mini")


def _isolated_discovery_env(
    env_codex_bin: str | None,
    macos_codex_path: str,
    path_value: str,
) -> "_DiscoveryEnvPatch":
    env = {"PATH": path_value, codex_discovery.C_ORCH_CODEX_BIN_ENV: ""}
    if env_codex_bin is not None:
        env[codex_discovery.C_ORCH_CODEX_BIN_ENV] = env_codex_bin
    return _DiscoveryEnvPatch(env, macos_codex_path)


class _DiscoveryEnvPatch:
    def __init__(self, env: dict, macos_codex_path: str) -> None:
        self._env_patch = mock.patch.dict(os.environ, env, clear=False)
        self._app_patch = mock.patch.object(codex_discovery, "MACOS_CODEX_PATH", macos_codex_path)

    def __enter__(self) -> "_DiscoveryEnvPatch":
        self._env_patch.__enter__()
        self._app_patch.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._app_patch.__exit__(exc_type, exc, traceback)
        self._env_patch.__exit__(exc_type, exc, traceback)


def _write_fake_codex(
    path: Path,
    version: str = "codex 1.0.0",
    models: object = None,
) -> str:
    if models is None:
        models = {"models": []}
    models_json = json.dumps(models)
    script = f"""\
#!{sys.executable}
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

if sys.argv[1:] == ["debug", "models"]:
    print({models_json!r})
    raise SystemExit(0)

print("unexpected args", file=sys.stderr)
raise SystemExit(2)
"""
    return _write_executable(path, script)


def _write_broken_codex(path: Path) -> str:
    script = f"""\
#!{sys.executable}
import sys

if sys.argv[1:] == ["--version"]:
    print("bad version", file=sys.stderr)
    raise SystemExit(7)

if sys.argv[1:] == ["debug", "models"]:
    print("not-json")
    raise SystemExit(0)

raise SystemExit(2)
"""
    return _write_executable(path, script)


def _write_executable(path: Path, script: str) -> str:
    path.write_text(textwrap.dedent(script), encoding="utf-8")
    path.chmod(0o755)
    return str(path)


if __name__ == "__main__":
    unittest.main()
