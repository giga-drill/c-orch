from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c_orch.runtime import COrchRuntime


class RuntimeTests(unittest.TestCase):
    def test_runtime_reuses_cached_driver_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            driver = driver_cls.return_value
            driver.__enter__.return_value = driver
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with runtime._driver_context("/bin/codex") as first:
                self.assertIs(first, driver)
            with runtime._driver_context("/bin/codex") as second:
                self.assertIs(second, driver)

            driver_cls.assert_called_once_with(codex_bin="/bin/codex")
            driver.close.assert_not_called()

            runtime.close()

            driver.close.assert_called_once()

    def test_runtime_drops_cached_driver_after_action_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            first_driver = mock.Mock()
            first_driver.__enter__ = mock.Mock(return_value=first_driver)
            first_driver.__exit__ = mock.Mock(return_value=None)
            second_driver = mock.Mock()
            second_driver.__enter__ = mock.Mock(return_value=second_driver)
            second_driver.__exit__ = mock.Mock(return_value=None)
            driver_cls.side_effect = [first_driver, second_driver]
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with self.assertRaises(RuntimeError):
                with runtime._driver_context("/bin/codex"):
                    raise RuntimeError("driver pipe broke")

            first_driver.close.assert_called_once()
            with runtime._driver_context("/bin/codex") as recovered:
                self.assertIs(recovered, second_driver)

            self.assertEqual(driver_cls.call_count, 2)


if __name__ == "__main__":
    unittest.main()
