"""Deterministic offline tests for the post-to-xhs skill.

These tests intentionally exercise pure helpers only.  They must never start
Chrome, connect to Xiaohongshu, publish content, or perform interactions.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import publish_pipeline  # noqa: E402
import xhs_stats_dashboard  # noqa: E402


class TitleLengthTests(unittest.TestCase):
    def test_weighted_title_length_counts_ascii_as_one(self) -> None:
        self.assertEqual(publish_pipeline._weighted_title_length("AI 2026!"), 8)

    def test_weighted_title_length_counts_chinese_as_two(self) -> None:
        self.assertEqual(publish_pipeline._weighted_title_length("小红书，AI!"), 11)

    def test_weighted_title_length_exposes_the_38_unit_boundary(self) -> None:
        self.assertEqual(publish_pipeline._weighted_title_length("中" * 19), 38)
        self.assertEqual(publish_pipeline._weighted_title_length("中" * 19 + "A"), 39)


class TopicExtractionTests(unittest.TestCase):
    def test_extracts_an_all_topic_last_line_and_trailing_blanks(self) -> None:
        content = "第一段\n第二段\n#AI #职场 #小红书\n\n"

        body, topics = publish_pipeline._extract_topic_tags_from_last_line(content)

        self.assertEqual(body, "第一段\n第二段")
        self.assertEqual(topics, ["#AI", "#职场", "#小红书"])

    def test_does_not_extract_a_mixed_text_last_line(self) -> None:
        content = "第一段\n#AI 这不是纯话题行"

        body, topics = publish_pipeline._extract_topic_tags_from_last_line(content)

        self.assertEqual(body, content)
        self.assertEqual(topics, [])


class DashboardSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {"标题": "A", "发布时间": "2026-08-01 10:00", "点赞": "5", "收藏": "5", "观看": "100", "评论": "1", "分享": "0", "曝光": "200", "涨粉": "1"},
            {"标题": "B", "发布时间": "2026-08-02 10:00", "点赞": "6", "收藏": "4", "观看": "200", "评论": "2", "分享": "1", "曝光": "300", "涨粉": "0"},
            {"标题": "C", "发布时间": "2026-08-03 10:00", "点赞": "9", "收藏": "0", "观看": "90", "评论": "0", "分享": "2", "曝光": "150", "涨粉": "2"},
            {"标题": "D", "发布时间": "2026-08-04 10:00", "点赞": "4", "收藏": "4", "观看": "80", "评论": "1", "分享": "0", "曝光": "120", "涨粉": "0"},
            {"标题": "E", "发布时间": "2026-08-05 10:00", "点赞": "3", "收藏": "4", "观看": "70", "评论": "0", "分享": "1", "曝光": "110", "涨粉": "1"},
            {"标题": "F", "发布时间": "2026-08-06 10:00", "点赞": "2", "收藏": "4", "观看": "60", "评论": "1", "分享": "0", "曝光": "100", "涨粉": "0"},
            {"标题": "G", "发布时间": "2026-08-07 10:00", "点赞": "1", "收藏": "4", "观看": "50", "评论": "0", "分享": "3", "曝光": "90", "涨粉": "3"},
        ]

    def test_summarize_totals_top_five_and_latest_note(self) -> None:
        summary = xhs_stats_dashboard.summarize(self.rows)

        self.assertEqual(summary["posts"], 7)
        self.assertEqual(summary["likes"], 30)
        self.assertEqual(summary["favorites"], 25)
        self.assertEqual(summary["likes_favorites"], 55)
        self.assertEqual(summary["views"], 650)
        self.assertEqual(summary["comments"], 5)
        self.assertEqual(summary["shares"], 7)
        self.assertEqual(summary["followers_gained"], 7)
        self.assertEqual([row["标题"] for row in summary["top"]], ["B", "A", "C", "D", "E"])
        self.assertEqual(summary["latest"]["标题"], "G")


class PublishModeTests(unittest.TestCase):
    def test_default_mode_never_clicks_publish(self) -> None:
        self.assertFalse(
            publish_pipeline._should_publish(auto_publish=False, preview=False)
        )

    def test_explicit_auto_publish_enables_click(self) -> None:
        self.assertTrue(
            publish_pipeline._should_publish(auto_publish=True, preview=False)
        )

    def test_preview_overrides_auto_publish_defensively(self) -> None:
        self.assertFalse(
            publish_pipeline._should_publish(auto_publish=True, preview=True)
        )


if __name__ == "__main__":
    unittest.main()
