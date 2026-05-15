from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from c_orch.dev_ui import build_api_command, build_vite_command, source_snapshot


class DevUiTests(unittest.TestCase):
    def test_build_api_command_uses_api_only_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_api_command(
                cwd=root,
                config_path=".c-orch.toml",
                runs_dir=root / "runs",
                queue_path=root / ".c-orch" / "tasks" / "queue.json",
                host="127.0.0.1",
                port=8765,
                python_executable="python3.11",
            )

        self.assertEqual(command[0], "python3.11")
        self.assertIn("ui", command)
        self.assertIn("--api-only", command)
        self.assertIn("--config", command)
        self.assertIn(".c-orch.toml", command)
        self.assertIn("--queue-file", command)

    def test_build_vite_command_targets_web_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_vite_command(cwd=root, host="127.0.0.1", port=5173)

        self.assertEqual(command[:4], ["pnpm", "--dir", str(root / "web"), "run"])
        self.assertIn("dev", command)
        self.assertIn("--host", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--port", command)
        self.assertIn("5173", command)

    def test_source_snapshot_tracks_python_changes_and_skips_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "c_orch"
            package.mkdir(parents=True)
            module = package / "module.py"
            module.write_text("value = 1\n", encoding="utf-8")
            pycache = package / "__pycache__"
            pycache.mkdir()
            cached = pycache / "module.py"
            cached.write_text("ignored = True\n", encoding="utf-8")

            first = source_snapshot((package,))
            time.sleep(0.001)
            module.write_text("value = 2\n", encoding="utf-8")
            second = source_snapshot((package,))

        self.assertNotEqual(first, second)
        self.assertTrue(all("__pycache__" not in path for path, _mtime in second))


if __name__ == "__main__":
    unittest.main()
