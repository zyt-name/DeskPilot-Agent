import unittest

from xm_agent.summary import FINISH_LABELS, final_summary, one_line


class OneLineTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self):
        self.assertEqual(one_line("  换行\n文本\t结束 "), "换行 文本 结束")
        self.assertEqual(one_line("abcdef", 4), "abc…")
        self.assertEqual(one_line("abcdef", 10), "abcdef")


class FinalSummaryTests(unittest.TestCase):
    def test_done_prefers_model_summary(self):
        summary = final_summary("done", "已在桌面创建 hello.txt。", [object()])
        self.assertEqual(summary, "已在桌面创建 hello.txt。")

    def test_done_keeps_markdown_line_breaks(self):
        text = "已完成：文件已保存。\n\n**当前状态**\n- 编辑区显示 hello"
        self.assertEqual(final_summary("done", text, []), text)

    def test_chinese_failure_keeps_markdown_line_breaks(self):
        text = "未能完成：登录页未打开。\n\n- 网络不可达\n- 请稍后重试"
        self.assertEqual(final_summary("failed", text, []), text)

    def test_done_without_reason_has_default(self):
        self.assertEqual(final_summary("done", "", []), "任务已完成。")

    def test_failed_translates_known_cause(self):
        summary = final_summary(
            "failed",
            "max steps reached",
            [object(), object()],
        )
        self.assertIn("已达到最大执行步数", summary)
        self.assertIn("已执行 2 步", summary)
        self.assertIn("max steps reached", summary)

    def test_failed_keeps_chinese_model_declaration(self):
        declared = "未能完成：目标窗口未打开，请先登录后再试。"
        self.assertEqual(final_summary("failed", declared, []), declared)

    def test_lock_busy_needs_human_is_explained(self):
        summary = final_summary(
            "needs_human",
            "desktop lock is held by another process",
            [],
        )
        self.assertIn("桌面正被另一个进程控制", summary)

    def test_finish_labels_cover_result_statuses(self):
        for status in ("done", "failed", "aborted", "needs_human"):
            self.assertIn(status, FINISH_LABELS)


if __name__ == "__main__":
    unittest.main()
