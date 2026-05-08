import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from step7_generate_docs import (  # noqa: E402
    build_day_report_markdown,
    build_day_sidebar_heading,
    update_sidebar,
)


class Step7GenerateDocsTest(unittest.TestCase):
    def test_build_day_report_markdown_for_empty_day(self):
        content = build_day_report_markdown("20260401", [], [])

        self.assertIn("# 日报 · 2026-04-01", content)
        self.assertIn("- 当次推荐总数：0", content)
        self.assertIn("- 精读区：0", content)
        self.assertIn("- 速读区：0", content)
        self.assertIn("## 今日简报（AI）", content)
        self.assertIn("今天已完成抓取与筛选，但没有论文进入推荐列表。", content)
        self.assertIn("## 精读区", content)
        self.assertIn("- 本次无精读推荐。", content)
        self.assertIn("## 速读区", content)
        self.assertIn("- 本次无速读推荐。", content)

    def test_update_sidebar_adds_report_link_for_empty_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sidebar_path = Path(tmpdir) / "_sidebar.md"
            sidebar_path.write_text("* [首页](/)\n* Daily Papers\n", encoding="utf-8")

            update_sidebar(sidebar_path, "20260401", [], [])

            content = sidebar_path.read_text(encoding="utf-8")
            self.assertIn("* [首页](/)", content)
            self.assertIn("* Daily Papers", content)
            self.assertIn("  * [2026-04-01](/2026/04/01/README) <!--dpr-date:20260401-->", content)
            self.assertNotIn("    * 精读区", content)
            self.assertNotIn("    * 速读区", content)

    def test_build_day_sidebar_heading_uses_report_link(self):
        heading = build_day_sidebar_heading("20260501")
        self.assertEqual(
            heading,
            "  * [2026-05-01](/2026/05/01/README) <!--dpr-date:20260501-->\n",
        )


if __name__ == "__main__":
    unittest.main()
