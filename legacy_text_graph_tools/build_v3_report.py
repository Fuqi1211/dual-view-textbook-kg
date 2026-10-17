"""生成独立的 v3 修复结果页；只读取修复图与证据。"""
import argparse
import hashlib
import html
import json
import os
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
SAMPLES = Path("研究方案_20261002/文本图人工核查包_20261002/核查样本.json")
DISPLAY_LIMIT = 1600


def esc(value):
    return html.escape(str(value), quote=True)


def relative_url(path, output, root):
    # 只生成项目内文件链接，不把数据当作URL执行。
    target = path if isinstance(path, Path) else root / path
    target = target.resolve()
    target.relative_to(root.resolve())
    return quote(Path(os.path.relpath(target, output.resolve())).as_posix(), safe="/._-")


def link(path, label, output, root, page=None):
    href = relative_url(path, output, root)
    if page is not None:
        href += "#page=" + str(int(page))
    return '<a href="' + esc(href) + '">' + esc(label) + '</a>'


def text_panel(value, label):
    value = str(value)
    note = f"{len(value):,} 字符"
    if len(value) > DISPLAY_LIMIT:
        value = value[:1200] + "\n\n……【中间内容未显示】……\n\n" + value[-400:]
        note += "；已截取首 1,200 与末 400 字符，完整原文见对应 JSON"
    return '<div class="text-label">' + esc(label) + ' <span>' + esc(note) + '</span></div><pre>' + esc(value) + '</pre>'


def page_label(node):
    pages = node.get("exact_pdf_pages")
    if pages:
        return "全书 PDF 第 " + "、".join(map(str, pages)) + " 页（修复证据定位）"
    start, end = node.get("pdf_page_start"), node.get("pdf_page_end")
    if start is None and end is None:
        return "未记录该节点精确 PDF 页码"
    return f"全书 PDF 章节范围 {start if start is not None else '?'}–{end if end is not None else '?'} 页；不是段落精确定位"


def render_documents(manifest, plan, samples, graphs, output, root, evidence):
    outcomes = manifest.get("sample_outcomes", plan.get("sample_outcomes", []))
    if not outcomes or len({x["sample_id"] for x in outcomes}) != len(outcomes):
        raise ValueError("Missing or duplicate sample outcomes")
    if plan.get("sample_outcomes") != outcomes:
        raise ValueError("Manifest and plan sample outcomes differ")
    counts = Counter()
    by_sample = {}
    for book_id, graph in graphs.items():
        nodes = {x["id"]: x for x in graph["nodes"]}
        texts = {x["id"]: x["text"] for x in graph["paragraph_texts"]}
        for repair in graph.get("repairs", []):
            role = "pdf-repairs" if repair.get("evidence") else "rule-repairs"
            counts[role] += 1
            if repair["output_record_id"] not in nodes:
                raise ValueError("Missing actual output record: " + repair["output_record_id"])
            if repair.get("sample_id"):
                by_sample.setdefault(repair["sample_id"], []).append((book_id, repair, nodes, texts))
    cards = []
    status_labels = {"audited_defect_repaired": "所核查缺陷已定向修复", "partially_repaired_quarantined": "已隔离；仍有未解决边界"}
    for outcome in outcomes:
        sid = outcome["sample_id"]
        if sid not in samples or sid not in by_sample:
            raise ValueError("Missing sample or actual repairs: " + sid)
        sample = samples[sid]
        repairs = by_sample[sid]
        pieces = []
        ev_paths = []
        for book_id, repair, nodes, texts in repairs:
            node = nodes[repair["output_record_id"]]
            if node.get("repair_id") != repair["id"]:
                raise ValueError("Output repair identity mismatch: " + sid)
            if node.get("canonical_text_id") not in texts:
                raise ValueError("Missing actual canonical text: " + sid)
            parent = nodes.get(node.get("heading_id"), {})
            title_path = node.get("title_path", parent.get("title_path", []))
            route = "默认正文检索：纳入" if node.get("retrieval_eligible") else "默认正文检索：排除"
            graph_path = output / book_id / "text_graph.json"
            body = '<p class="path">' + esc(" › ".join(title_path)) + '</p>'
            body += '<p>' + esc(page_label(node)) + '</p><p class="tag">' + esc(node.get("type", "") + " / " + node.get("content_kind", "") + " · " + route) + '</p>'
            body += text_panel(texts[node["canonical_text_id"]], "实际修复后输出文本")
            body += '<p class="record">输出节点：' + esc(node["id"]) + '<br>修复编号：' + esc(repair["id"]) + '<br>原记录：' + esc("、".join(node.get("source_record_ids", repair.get("record_ids", [])))) + '</p>'
            body += '<p>' + link(graph_path, "查看本书完整 text_graph.json", output, root) + '</p>'
            entries = sorted((n for n in nodes.values() if n.get("parent_block_id") == node["id"]), key=lambda n: n.get("index", 0))
            if entries:
                body += '<details><summary>展开此字段实例的 ' + str(len(entries)) + ' 条书目元数据</summary>'
                for entry in entries:
                    body += '<div class="entry"><strong>' + esc(entry.get("reference_label", entry["id"])) + '</strong><p>' + esc(page_label(entry)) + '</p>'
                    body += text_panel(texts[entry["canonical_text_id"]], "实际书目条目") + '</div>'
                body += '</details>'
            pieces.append('<details class="occurrence"' + (' open' if len(repairs) == 1 else '') + '><summary>修复输出 ' + str(len(pieces) + 1) + ' · ' + esc(node.get("type", "")) + '</summary>' + body + '</details>')
            ev_paths.extend(repair.get("evidence", []))
        evidence_html = []
        for ev_path in dict.fromkeys(ev_paths):
            meta = evidence.get(ev_path, {})
            links = [link(ev_path, "证据元数据", output, root)]
            if meta.get("source_pdf"):
                links.append(link(meta["source_pdf"], "原 PDF · 全书第 " + str(meta.get("global_pdf_page_1_based", "?")) + " 页 / 分卷第 " + str(meta.get("local_pdf_page_1_based", "?")) + " 页", output, root, meta.get("local_pdf_page_1_based")))
            if meta.get("render_path"):
                links.append(link(meta["render_path"], "查看已核验的原页图片", output, root))
            evidence_html.append('<li>' + " · ".join(links) + '</li>')
        limitations = list(outcome.get("limitations") or [])
        scope_notes = []
        if sid in ("R061", "R106"):
            scope_notes = [note for note in limitations
                           if (sid == "R061" and "跨页自然段" in note and "本补丁未处理" in note)
                           or (sid == "R106" and "仅修" in note)]
            limitations = [note for note in limitations if note not in scope_notes]
        final_state = {
            "R061": "跨页自然段与 I/O 已修；原树第6章章名未补全",
            "R106": "空格、章节归属与示例图片分类均已处理；习题完整终页未核",
        }.get(sid)
        if sid == "R097":
            limitations.insert(0, "38k 书目长块仍未可靠拆开；已从默认正文检索隔离，原文及续片保留。这不等于书目逐条恢复完成。")
        if sid == "R098":
            limitations.insert(0, "36 条书目元数据按已核对标签分界；重复字段中的副本不增加条目种类。全文 OCR 未逐字验证，仍从默认正文检索排除。")
        origin = sample.get("source_origin", {})
        source = '<p class="path">原路径：' + esc(" › ".join(sample.get("title_path", []))) + '</p>'
        source += text_panel(sample["text"], "原核查样本文字")
        source += '<p class="record">原记录：' + esc(sample["record_id"]) + '<br>JSON 字段：' + esc(origin.get("json_pointer", "未提供")) + '<br>原范围：' + esc(f"[{sample.get('field_start', '?')}, {sample.get('field_end', '?')})") + '</p>'
        if origin.get("input_path"):
            source += '<p>' + link(origin["input_path"], "原 content_tree.json", output, root) + '</p>'
        card = '<section class="sample" id="' + esc(sid) + '"><div class="sample-head"><span class="sample-id">' + esc(sid) + '</span><h2>' + esc(sample["book_title"]) + '</h2><span class="status">' + esc(status_labels.get(outcome["status"], outcome["status"])) + '</span></div>'
        card += '<p class="muted">' + str(len(repairs)) + ' 个实际修复操作；父子字段复写只表示 JSON 字段实例，不表示独立物理证据。</p>'
        card += '<details><summary>查看原核查文本与字段位置</summary>' + source + '</details>'
        card += ''.join(pieces)
        if final_state:
            card += '<p><strong>合并后状态：</strong>' + esc(final_state) + '</p>'
        if scope_notes:
            card += '<details><summary>原文字子补丁的范围说明（不是最终未解决项）</summary><ul>' + ''.join('<li>' + esc(note) + '</li>' for note in scope_notes) + '</ul><p>上述文字仅描述早期文字子补丁的范围，最终状态以本页活动输出及合并后状态为准。</p></details>'
        if limitations:
            card += '<div class="limit"><strong>保留的限制</strong><ul>' + ''.join('<li>' + esc(x) + '</li>' for x in limitations) + '</ul></div>'
        card += '<details><summary>原 PDF 与核验图片（' + str(len(evidence_html)) + ' 页证据）</summary><ul>' + ''.join(evidence_html) + '</ul></details></section>'
        cards.append(card)
    book_rows = []
    markdown_rows = []
    for row in manifest["books"]:
        bid = row["book_id"]
        graph_path, index_path = output / bid / "text_graph.json", output / bid / "retrieval_index.jsonl"
        book_rows.append('<tr><td>' + esc(row["book_title"]) + '</td><td>' + link(graph_path, "text_graph.json", output, root) + '</td><td>' + link(index_path, "retrieval_index.jsonl", output, root) + '</td></tr>')
        markdown_rows.append('- ' + row["book_title"].replace('\n', ' ') + ': [text_graph.json](' + relative_url(graph_path, output, root) + ') · [retrieval_index.jsonl](' + relative_url(index_path, output, root) + ')')
    partial = sum(x["status"] == "partially_repaired_quarantined" for x in outcomes)
    intro = f"本页记录 {len(outcomes)} 个已发现问题核查窗口的修复结果，并直接读取 v3 导出节点展示。修复依据来自原 PDF 视觉核验；其中 {partial} 个窗口仍有未解决边界并已隔离。它不代表 24 本书已完成全面校勘。"
    validation = manifest.get("_independent_validation", {})
    validation_status = validation.get("status")
    validation_label = {"passed": "独立结构验收：已通过", "failed": "独立结构验收：未通过"}.get(validation_status, "独立结构验收：待验收结果确认")
    validation_note = validation_label + "。结构验收不代表全文 OCR 或语义正确性已核验。"
    validation_html = esc(validation_note)
    if validation:
        validation_html += " " + link(output / "独立验收.json", "查看独立验收记录", output, root)
    rules = "规则标题分类只匹配现有编号标题，不计作 PDF 核验。操作数包含父子字段副本，不是独立原书证据数。AI 辅助修复没有代填人工核查标签，human_verified 仍为 false。"
    usage = "检索只读取本目录各书的 v3 retrieval_index.jsonl。nodes 保存当前活动正文与元数据，paragraph_texts 是共享文本库；repairs.output_record_id 指向实际输出，source_record_ids 指向原记录。source_record_archive 和 heading.source_text_layout 用于复原原始字段，原文布局不代表修正后的阅读顺序；阅读连接以活动图的 contains / next 为准。来源 offset 锚定原文，新文字使用 text_ref，不可把新文字长度套用到旧 offset。"
    css = """*{box-sizing:border-box}body{margin:0;background:#f4f3ef;color:#25332d;font:16px/1.65 system-ui,'Microsoft YaHei',sans-serif}a{color:#276958;text-underline-offset:3px}main{max-width:1060px;margin:auto;padding:40px 24px 80px}header{border-bottom:2px solid #315d4b;padding-bottom:24px}h1{font-size:32px;letter-spacing:.02em;margin:4px 0 14px}h2{font-size:19px;margin:0;flex:1}h3{font-size:20px}.eyebrow,.muted,.text-label span{color:#65746b;font-size:13px}.summary{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}.metric{background:#e5ece4;padding:16px 22px;min-width:200px;border-radius:8px}.metric strong{display:block;font-size:30px;line-height:1.2}.metric span{font-size:13px}nav{display:flex;gap:8px;flex-wrap:wrap;padding:12px 0 24px}nav a{padding:3px 9px;border:1px solid #becabe;border-radius:4px;text-decoration:none;font-size:13px}.sample{background:white;border:1px solid #d9dfd6;border-radius:10px;margin:22px 0;padding:22px;scroll-margin-top:15px}.sample-head{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.sample-id{font:700 16px ui-monospace,monospace;color:#42694e}.status,.tag{font-size:12px;color:#48634f}.status{background:#eff3e9;padding:4px 8px;border-radius:4px}details{border-top:1px solid #e7e9e2;padding:13px 0}summary{cursor:pointer;font-weight:600}details[open]>summary{margin-bottom:14px}.occurrence{margin-left:12px}.path{color:#48694f}.record{font:12px/1.7 ui-monospace,monospace;overflow-wrap:anywhere;color:#617367}.text-label{font-size:13px;font-weight:600;margin-top:15px}.text-label span{font-weight:400;display:block}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.75 ui-monospace,'Microsoft YaHei',monospace;padding:18px;background:#f6f7f2;border:1px solid #e4e7de;border-radius:6px;max-height:600px;overflow:auto}.limit{background:#fff7e5;padding:13px 18px;margin:14px 0;font-size:14px;border-left:3px solid #ad8653}.limit ul{margin:6px 0}.entry{padding:12px;border-bottom:1px solid #e5e7df}table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;border-bottom:1px solid #d8ded5;padding:10px}footer{font-size:13px;color:#5c6e60;margin-top:35px;overflow-wrap:anywhere}@media(max-width:600px){main{padding:24px 12px}.sample{padding:16px}.occurrence{margin-left:0}h1{font-size:27px}td{padding:7px;font-size:12px}}"""
    page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'self\' data:; base-uri \'none\'; form-action \'none\'"><title>24 本文本图 · v3 修复结果</title><style>' + css + '</style></head><body><main>'
    page += '<header><div class="eyebrow">文本图 · 证据修复记录</div><h1>v3 修复结果</h1><p>' + esc(intro) + '</p><p class="muted">' + esc(rules) + '</p><p class="muted">' + validation_html + '</p></header>'
    page += '<div class="summary"><div class="metric"><strong data-count="samples">' + str(len(outcomes)) + '</strong><span>问题核查窗口</span></div><div class="metric"><strong data-count="pdf-repairs">' + str(counts['pdf-repairs']) + '</strong><span>PDF 证据修复操作</span></div><div class="metric"><strong data-count="rule-repairs">' + str(counts['rule-repairs']) + '</strong><span>规则标题分类操作</span></div></div>'
    page += '<nav aria-label="核查窗口">' + ''.join('<a href="#' + esc(x['sample_id']) + '">' + esc(x['sample_id']) + '</a>' for x in outcomes) + '</nav>'
    page += ''.join(cards) + '<section><h3>每书产物与读取方式</h3><p>' + esc(usage) + '</p><table><thead><tr><th>书名</th><th>活动图与原文出处</th><th>默认检索入口</th></tr></thead><tbody>' + ''.join(book_rows) + '</tbody></table></section>'
    page += '<footer>本页为离线静态结果展示，不发起联网请求、不收集人工标签。证据链接指向已有本地文件。<br>源 manifest SHA256：' + esc(plan.get('source_manifest_sha256', '未提供')) + '<br>修复计划 SHA256：' + esc(manifest.get('repair_plan', {}).get('sha256', '未提供')) + '</footer></main></body></html>'
    readme = '# 文本图 v3 修复结果\n\n' + intro + '\n\n双击 [修复结果.html](修复结果.html) 查看逐条原文、实际输出及 PDF 证据。\n\n' + validation_note + '\n\n' + f'- PDF 证据修复操作：{counts["pdf-repairs"]}\n- 规则标题分类操作：{counts["rule-repairs"]}\n\n' + rules + '\n\nR097 的长书目仍未可靠拆开，已经隔离；R098 形成 36 条书目元数据，但全文 OCR 未逐字核验。两者均不进入默认教学正文检索。\n\n' + usage + '\n\n## 每书文件\n\n' + '\n'.join(markdown_rows) + '\n\n本报告只展示已有输出，不修改图、源文字、旧审计结果或人工标签。\n'
    return page, readme


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_report(output, plan_path):
    output, plan_path = output.resolve(), plan_path.resolve()
    output.relative_to(ROOT)
    plan_path.relative_to(ROOT)
    manifest = read_json(output / "manifest.json")
    plan = read_json(plan_path)
    if manifest.get("schema_version") != "paragraph-text-graph-manifest-v3":
        raise ValueError("A completed v3 manifest is required")
    if manifest.get("repair_plan", {}).get("sha256") != sha_file(plan_path):
        raise ValueError("Repair plan hash does not match export")
    package = read_json(ROOT / SAMPLES)
    if package["source_manifest_sha256"] != plan["source_manifest_sha256"]:
        raise ValueError("Sample package and plan source identity differ")
    validation_path = output / "独立验收.json"
    if validation_path.is_file():
        manifest["_independent_validation"] = read_json(validation_path)
    graphs = {}
    evidence = {}
    for row in manifest["books"]:
        graph_path = output / row["book_id"] / "text_graph.json"
        artifacts = {a["path"]: a for a in row["artifacts"]}
        if sha_file(graph_path) != artifacts[graph_path.relative_to(output).as_posix()]["sha256"]:
            raise ValueError("Graph hash differs from manifest: " + row["book_id"])
        graph = read_json(graph_path)
        # 只保留结果页需要的节点，避免同时常驻24本原图。
        wanted = {p["output_record_id"] for p in graph["repairs"]}
        wanted.update(n["id"] for n in graph["nodes"] if n.get("parent_block_id") in wanted)
        parents = {n.get("heading_id") for n in graph["nodes"] if n["id"] in wanted}
        kept = [n for n in graph["nodes"] if n["id"] in wanted or n["id"] in parents]
        text_ids = {n.get("canonical_text_id") for n in kept}
        graphs[row["book_id"]] = {"nodes": kept, "paragraph_texts": [t for t in graph["paragraph_texts"] if t["id"] in text_ids], "repairs": graph["repairs"], "summary": graph["summary"]}
        for repair in graph["repairs"]:
            for value in repair.get("evidence", []):
                if value in evidence:
                    continue
                path = (ROOT / value).resolve()
                path.relative_to(ROOT)
                if sha_file(path) != manifest["evidence_metadata_sha256"][value]:
                    raise ValueError("Evidence metadata hash mismatch: " + value)
                evidence[value] = read_json(path)
        del graph
    page, readme = render_documents(manifest, plan, {s["sample_id"]: s for s in package["items"]}, graphs, output, ROOT, evidence)
    for filename, value in (("修复结果.html", page), ("README.md", readme)):
        with (output / filename).open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
    print(json.dumps({"report": str(output / "修复结果.html"), "books": len(manifest["books"]), "sample_outcomes": len(manifest["sample_outcomes"]), "html_bytes": len(page.encode("utf-8"))}, ensure_ascii=False))


class ReportTests(unittest.TestCase):
    def fixture(self):
        manifest = {"books": [{"book_id": "b1", "book_title": "测试书"}],
                    "summary": {}, "sample_outcomes": [{"sample_id": "R097", "status": "partially_repaired_quarantined", "limitations": []}]}
        plan = {"sample_outcomes": manifest["sample_outcomes"], "patches": [{"id": "p1", "sample_id": "R097", "replacement_text": "计划文字不可冒充实际输出"}]}
        samples = {"R097": {"sample_id": "R097", "book_id": "b1", "book_title": "测试书", "record_id": "old",
                             "text": "<script>alert(1)</script>" + "长" * 2500, "title_path": ["旧章"], "source_origin": {}}}
        graph = {"nodes": [{"id": "h", "type": "section", "title_path": ["测试书", "参考文献"]},
                           {"id": "new", "type": "bibliography_block", "heading_id": "h", "canonical_text_id": "t",
                            "exact_pdf_pages": [921, 925], "retrieval_eligible": False, "repair_id": "p1"}],
                 "paragraph_texts": [{"id": "t", "text": "实际导出 & <b>书目</b>"}],
                 "source_record_archive": [{"id": "old"}],
                 "repairs": [{"id": "p1", "sample_id": "R097", "output_record_id": "new", "record_ids": ["old"], "evidence": ["evidence.json"]},
                             {"id": "rule1", "output_record_id": "new", "record_ids": ["other"], "rule_evidence_heading_ids": ["h"], "evidence": []}],
                 "summary": {"pdf_evidence_repair_count": 1, "rule_role_repair_count": 1}}
        return manifest, plan, samples, {"b1": graph}

    def test_actual_output_escape_and_counts(self):
        self.assertTrue(callable(globals().get("render_documents")), "实现前报告生成器尚不存在")
        page, readme = render_documents(*self.fixture(), Path("/project/out"), Path("/project"), {})
        self.assertIn("实际导出 &amp; &lt;b&gt;书目&lt;/b&gt;", page)
        self.assertNotIn("计划文字不可冒充实际输出", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn('data-count="pdf-repairs">1<', page)
        self.assertIn('data-count="rule-repairs">1<', page)
        self.assertIn("已截取", page)
        self.assertIn("未可靠拆开", page)
        self.assertIn("retrieval_index.jsonl", readme)

    def test_missing_actual_record_fails(self):
        self.assertTrue(callable(globals().get("render_documents")), "实现前报告生成器尚不存在")
        args = self.fixture()
        args[3]["b1"]["repairs"][0]["output_record_id"] = "missing"
        with self.assertRaises(ValueError):
            render_documents(*args, Path("/project/out"), Path("/project"), {})

    def test_independent_acceptance_is_explicit(self):
        args = self.fixture()
        args[0]["_independent_validation"] = {"status": "passed"}
        page, _ = render_documents(*args, Path("/project/out"), Path("/project"), {})
        self.assertIn("独立结构验收：已通过", page)
        self.assertIn("不代表全文 OCR 或语义正确性已核验", page)
        args[0]["_independent_validation"] = {"status": "failed", "errors": ["test error"]}
        page, _ = render_documents(*args, Path("/project/out"), Path("/project"), {})
        self.assertIn("独立结构验收：未通过", page)
        self.assertNotIn("独立结构验收：已通过", page)

    def test_subpatch_notes_do_not_hide_final_repairs(self):
        cases = [("R061", "跨页自然段合并及章节名称缺失属于结构修复，本补丁未处理。", "跨页自然段与 I/O 已修；原树第6章章名未补全"),
                 ("R106", "本项仅修空格。", "空格、章节归属与示例图片分类均已处理；习题完整终页未核")]
        for sid, old_note, final_state in cases:
            args = self.fixture()
            args[0]["sample_outcomes"][0].update(sample_id=sid, limitations=[old_note])
            sample = args[2].pop("R097")
            sample["sample_id"] = sid
            args[2][sid] = sample
            args[3]["b1"]["repairs"][0]["sample_id"] = sid
            page, _ = render_documents(*args, Path("/project/out"), Path("/project"), {})
            self.assertIn(final_state, page)
            self.assertIn("原文字子补丁的范围说明（不是最终未解决项）", page)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为已完成的v3导出生成离线修复结果页")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        unittest.main(argv=[__file__])
    elif not args.output or not args.plan:
        parser.error("--output and --plan are required unless --self-test is used")
    else:
        build_report(args.output, args.plan)
