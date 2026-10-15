"""规格页导入的测试：另存 HTML → 抽取候选事实。

这里锁的是"抽取要准、不确定要标出来"这两件事。抽取错的代价是把错值写进语料，
而语料是判定基准——所以宁可标"未识别"，也不要猜。

用仿真另存页驱动，不依赖网络，也不依赖任何真实站点。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HARNESS = ROOT / "harness"
for path in (SRC, HARNESS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import import_spec  # noqa: E402


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>示例充电宝 20000mAh 规格参数</title>
<link rel="canonical" href="https://shop.example.test/item/x1">
<meta name="publisher" content="示例商城">
<meta property="article:published_time" content="2026-03-11">
<script>var hidden = "30000mAh";</script>
</head><body>
<h1>示例充电宝</h1>
<ul>
<li>电池容量：20000mAh</li>
<li>额定能量:74Wh 输入功率:18W，输出功率:22.5W</li>
<li>净重：434g</li>
<li>尺寸：150.6 x 72.2 x 26.9 mm</li>
<li>防水等级：IPX4</li>
<li>续航：40 小时</li>
<li>废水比：1:1</li>
</ul>
<style>.x { content: "99Wh"; }</style>
</body></html>
"""


class ExtractionTests(unittest.TestCase):
    def test_values_are_extracted_with_units(self) -> None:
        values = import_spec.extract_values(PAGE)
        texts = {item["text"] for item in values}
        for expected in ("20000mAh", "74Wh", "18W", "22.5W", "434g", "40小时", "1:1"):
            self.assertIn(expected, texts, expected)

    def test_alphanumeric_grades_are_extracted(self) -> None:
        """IPX4 这类"字母+数字"没有单位，但是可比对的规格，不能漏。"""

        values = {item["text"] for item in import_spec.extract_values(PAGE)}
        self.assertIn("IPX4", values)

    def test_script_and_style_content_is_not_extracted(self) -> None:
        """脚本与样式里的数值不算规格：那是页面逻辑，不是参数。

        走的是与采集同一条路径（`read_page` 会先剥掉 script/style），
        而不是直接把整份 HTML 丢给抽取器。
        """

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.html"
            path.write_text(PAGE, encoding="utf-8")
            text, _ = import_spec.read_page(path)
        texts = {item["text"] for item in import_spec.extract_values(text)}
        self.assertNotIn("30000mAh", texts)
        self.assertNotIn("99Wh", texts)

    def test_label_adjacent_value_is_marked_as_high_certainty(self) -> None:
        values = {item["text"]: item for item in import_spec.extract_values(PAGE)}
        self.assertEqual(values["74Wh"]["metric"], "额定能量")
        self.assertEqual(values["74Wh"]["metric_certainty"], "label")
        self.assertEqual(values["20000mAh"]["metric"], "容量")

    def test_crowded_line_does_not_steal_the_previous_label(self) -> None:
        """同一行里多个"标签:数值"时，各自认自己的标签。"""

        values = {item["text"]: item for item in import_spec.extract_values(PAGE)}
        self.assertEqual(values["18W"]["metric"], "快充功率")
        self.assertEqual(values["22.5W"]["metric"], "快充功率")

    def test_unknown_metric_is_left_empty_rather_than_guessed(self) -> None:
        """认不出的数值要标"未识别"，不能硬猜一个指标名塞进语料。"""

        values = import_spec.extract_values("本产品采用食品级材料。共 250ml。")
        unknown = [item for item in values if item["metric_certainty"] == "none"]
        self.assertTrue(unknown, "认不出的数值必须标成未识别")
        for item in unknown:
            self.assertEqual(item["metric"], "")

    def test_ratio_is_extracted(self) -> None:
        values = import_spec.extract_values("废水比 1:1，净水流量 1.5L/min。")
        self.assertIn("1:1", {item["text"] for item in values})

    def test_context_is_single_line(self) -> None:
        values = import_spec.extract_values(PAGE)
        for item in values:
            self.assertNotIn("\n", item["context"])


class ImportPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "01-充电宝.html").write_text(PAGE, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_import_reads_meta_and_values(self) -> None:
        payload = import_spec.import_dir(self.dir, {"示例充电宝": ["示例充电宝 20000mAh"]})
        self.assertEqual(len(payload["pages"]), 1)
        page = payload["pages"][0]
        self.assertEqual(page["category"], "充电宝")
        self.assertEqual(page["meta"]["url"], "https://shop.example.test/item/x1")
        self.assertEqual(page["meta"]["publisher"], "示例商城")
        self.assertEqual(page["meta"]["date"], "2026-03-11")
        self.assertTrue(page["meta"]["sha256"])
        self.assertTrue(page["values"])

    def test_category_comes_from_file_name_prefix(self) -> None:
        self.assertEqual(import_spec.category_of(Path("07-加湿器.html")), "加湿器")
        self.assertEqual(import_spec.category_of(Path("净水器.html")), "净水器")

    def test_review_markdown_flags_unidentified_values(self) -> None:
        (self.dir / "02-未知.html").write_text("<p>净含量 250ml</p>", encoding="utf-8")
        payload = import_spec.import_dir(self.dir, {})
        review = import_spec.render_review(payload)
        self.assertIn("规格页导入确认清单", review)
        self.assertIn("标签紧邻（可采信）", review)
        self.assertIn("需要你补指标名", review)
        self.assertIn("确认之前不会写入", review)

    def test_handles_gbk_saved_page(self) -> None:
        """有些站点的另存页是 GBK，编码猜错会得到乱码与抽取失败。"""

        raw = "<html><head><title>国标页</title></head><body><p>额定能量 74Wh</p></body></html>".encode(
            "gb18030"
        )
        path = self.dir / "03-国标.html"
        path.write_bytes(raw)
        text, charset = import_spec.read_page(path)
        self.assertIn("74Wh", text)
        self.assertIn(charset.lower(), ("gb18030", "gbk", "gb2312"))

    def test_plain_text_file_is_supported(self) -> None:
        path = self.dir / "04-纯文本.txt"
        path.write_text("电池容量：20000mAh\n额定能量：74Wh\n", encoding="utf-8")
        text, _ = import_spec.read_page(path)
        values = {item["text"] for item in import_spec.extract_values(text)}
        self.assertIn("20000mAh", values)
        self.assertIn("74Wh", values)


if __name__ == "__main__":
    unittest.main()
