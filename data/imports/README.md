# 规格页导入目录

把浏览器里**另存为**的网页放进这个目录，命名规则：`序号-品类.html`

    01-充电宝.html
    02-电动牙刷.html
    03-降噪耳机.html
    04-净水器.html
    05-精华.html
    06-婴童辅食.html
    07-加湿器.html

然后运行：

    python harness/import_spec.py

产出在 `runs/import-<时间>/review.md`（给人核对的清单）与 `candidates.json`（机器可读）。
**确认之前不会写入 `data/corpus/`。**

另存时选「网页，仅 HTML」即可，不需要图片；`.txt` 也支持（直接把规格段粘进文本文件）。
这些文件里是第三方网页内容，**不要提交到公开仓库**（本目录已在 .gitignore 里排除 `*.html`）。
