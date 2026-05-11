from __future__ import annotations

import unittest

from c_orch.drivers import DriverError, coerce_session_result


class DriverTests(unittest.TestCase):
    def test_coerce_structured_content(self) -> None:
        result = coerce_session_result(
            {"structuredContent": {"threadId": "thr_1", "content": "done"}}
        )
        self.assertEqual(result.thread_id, "thr_1")
        self.assertEqual(result.content, "done")

    def test_missing_thread_id_raises(self) -> None:
        with self.assertRaises(DriverError):
            coerce_session_result({"structuredContent": {"content": "done"}})


if __name__ == "__main__":
    unittest.main()

