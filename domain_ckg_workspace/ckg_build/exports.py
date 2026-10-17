from __future__ import annotations

import csv
import json
from collections import Counter

from .runtime import digest, dumps, save


def jsonl(path, rows):
    path.write_text("".join(dumps(row) + "\n" for row in rows), encoding="utf-8")


def csv_file(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # 避免表格打开时将源文本解释为公式。
            values = {k: dumps(row.get(k)) if isinstance(row.get(k), (dict, list)) else row.get(k, "") for k in columns}
            values = {k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in values.items()}
            writer.writerow(values)


def export_all(engine):
    s, root = engine.state, engine.root
    recovery_nodes = engine.recovery.get("nodes", {})
    recovery_summary = [{k: v for k, v in node.items() if k != "payload"} for node in recovery_nodes.values()]
    cached_leaf_ids = sorted({pid for node in recovery_nodes.values() if node.get("status") == "validated" and not node.get("children")
                              for pid in node["paragraph_ids"]})
    known_mention_issues = [{"window_id": decision["window_id"], "candidate_id": mention["id"],
                             "evidence": mention.get("evidence"), "support_issues": mention.get("support_issues", [])}
                            for decision in s["decisions"] if decision.get("stage") == "sweep"
                            for mention in decision.get("known_mentions", []) if mention.get("support_status") == "not_demonstrated"]
    quarantined_candidates = [{"window_id": decision["window_id"], **candidate}
                               for decision in s["decisions"] if decision.get("stage") == "sweep"
                               for candidate in decision.get("quarantined_candidates", [])]
    concepts, relations, mentions, definitions, evidence, queue = [], [], [], [], [], []
    for group in s["groups"].values():
        rep = s["admission"][group["seed"]]
        members = [s["admission"][mid] for mid in group["members"]]
        aliases = sorted({str(alias) for m in members for alias in [m["name"]] + (m.get("aliases") or [])} - {rep["name"]})
        row = {"id": group["id"], "name": rep["name"], "entity_type": group.get("canonical_entity_type", rep["entity_type"]), "scope": rep.get("scope", ""),
               "type_corrections": group.get("type_corrections", []),
               "aliases": aliases, "definition": rep.get("definition", ""), "member_ids": group["members"],
               "definition_status": "missing", "definition_source_member_ids": [], "definition_evidence": [],
               "auto_status": "included", "human_status": "unreviewed", "evidence": []}
        for m in members:
            row["evidence"].extend(m.get("evidence", []))
            for e in m.get("evidence", []):
                record = {"concept_id": group["id"], "candidate_id": m["id"], **e}
                mentions.append(record)
                evidence.append({"kind": "existence", **record})
            definitions.append({"concept_id": group["id"], "candidate_id": m["id"], "definition": m.get("definition", ""),
                                "status": m.get("definition_status", "missing"), "proposal": m.get("definition_proposal", ""),
                                "evidence": m.get("definition_evidence", []), "human_status": "unreviewed"})
            evidence.extend({"kind": "definition", "concept_id": group["id"], "candidate_id": m["id"], **e} for e in m.get("definition_evidence", []))
        row["evidence"] = list({dumps(e): e for e in row["evidence"]}.values())
        supported_definitions = {" ".join(m.get("definition", "").split()) for m in members if m.get("definition_status") == "supported"}
        if len(supported_definitions) > 1:
            row["definition"] = ""
            row["definition_status"] = "unresolved_conflict"
            queue.append({"kind": "definition_conflict", "id": group["id"], "reason": "Multiple supported definitions require scope/compatibility adjudication", "definitions": sorted(supported_definitions), "human_status": "unreviewed"})
        elif supported_definitions:
            row["definition"] = next(iter(supported_definitions))
            row["definition_status"] = "supported"
            supporting_members = [m for m in members if m.get("definition_status") == "supported" and " ".join(m.get("definition", "").split()) == row["definition"]]
            row["definition_source_member_ids"] = [m["id"] for m in supporting_members]
            row["definition_evidence"] = list({dumps(e): e for m in supporting_members for e in m.get("definition_evidence", [])}.values())
        else:
            row["definition"] = ""
        concepts.append(row)
    buckets = {}
    for rid, r in s["relation_review"].items():
        consistency = s.get("consistency", {}).get(rid, {"decision": "unresolved", "reason": "Consistency stage pending"})
        if r.get("counterevidence_qualified") is True:
            evidence.extend({"kind": "counterevidence", "relation_candidate_id": rid, **e} for e in r.get("evidence", []))
        if r["decision"] != "include" or consistency["decision"] != "consistent" or r.get("polarity") != "positive":
            queue.append({"kind": "relation_model_issue" if r.get("model_issues") else "relation", "id": rid,
                          "decision": r, "consistency": consistency, "human_status": "unreviewed"})
            continue
        head, tail, relation_type = r["head"], r["tail"], r["relation"]
        if relation_type == "consists_of":
            head, tail, relation_type = tail, head, "part_of"
        key = (head, tail, relation_type, " ".join(r.get("conditions", "").split()))
        buckets.setdefault(key, []).append(r)
    for key, rows in buckets.items():
        rid = "relation_" + digest(key)[:24]
        items = list({dumps(e): e for r in rows for e in r["evidence"]}.values())
        relations.append({"id": rid, "head": key[0], "tail": key[1], "relation": key[2], "conditions": key[3],
                          "evidence": items, "member_ids": [r["id"] for r in rows],
                          "original_claims": [{"id": r["id"], "head": r["head"], "tail": r["tail"], "relation": r["relation"], "conditions": r.get("conditions", "")} for r in rows],
                          "semantic_checks": [{"member_id": r["id"], "semantic_check": r.get("semantic_check")} for r in rows],
                          "auto_status": "included", "human_status": "unreviewed"})
        evidence.extend({"kind": "relation", "relation_id": rid, **e} for e in items)
    for cid, c in s["candidates"].items():
        admission = s["admission"].get(cid)
        if not admission or admission["decision"] != "include" or cid not in s["candidate_map"]:
            queue.append({"kind": "concept", "id": cid, "candidate": c, "admission": admission, "human_status": "unreviewed"})
        if admission and admission.get("definition_status") in {"unsupported", "missing"}:
            queue.append({"kind": "definition", "id": cid, "decision": admission, "human_status": "unreviewed"})
        comparisons = s["identity"].get(cid, [])
        if any(r["decision"] == "unresolved" for r in comparisons) or sum(r["decision"] == "merge" for r in comparisons) > 1:
            queue.append({"kind": "identity", "id": cid, "comparisons": comparisons, "human_status": "unreviewed"})
    for key, failure in s["failures"].items():
        queue.append({"kind": "failed_batch", "id": key, "reason": failure, "human_status": "unreviewed"})
    for conflict in s.get("identity_conflicts", []):
        queue.append({"kind": "identity_conflict", "id": digest(conflict)[:24], **conflict, "human_status": "unreviewed"})
    for candidate in quarantined_candidates:
        queue.append({"kind": "sweep_candidate_quarantine", "id": digest(candidate)[:24], "candidate": candidate, "human_status": "unreviewed"})
    for index, relation in enumerate(engine.prepared.get("prior_relations", [])):
        queue.append({"kind": "prior_relation_pending", "id": relation.get("id", "prior_relation_" + str(index)),
                      "candidate": relation, "reason": "Prior-only relation has not undergone current-source evidence mapping and claim adjudication", "human_status": "unreviewed"})
    coverage = {"status": s["status"], "source_units_total": len(engine.paragraphs), "source_units_reviewed": len(s["sweep"]),
                "source_units": [{"id": pid, **s["sweep"].get(pid, {"status": "pending"})} for pid in engine.paragraphs],
                "legacy_import_coverage": engine.prepared.get("coverage", {}), "candidates_total": len(s["candidates"]),
                "candidates_adjudicated": len(s["admission"]), "identity_processed": len(s["candidate_map"]),
                "relation_candidates_total": len(s["relations"]), "relation_candidates_adjudicated": len(s["relation_review"]),
                "consistency_processed": len(s.get("consistency", {})), "failed_batches": s["failures"],
                "pause_reason": s.get("pause_reason"), "semantic_completeness_verified": False,
                "pipeline_processing_complete": s["status"] == "complete",
                "original_source_windows_total": len(engine.sweep_windows()),
                "original_source_windows_completed": len(s["completed"].get("sweep", [])),
                "known_mention_issue_count": len(known_mention_issues),
                "known_mention_issues": known_mention_issues,
                "quarantined_candidate_count": len(quarantined_candidates),
                "quarantined_candidates": quarantined_candidates,
                "qualified_counterevidence_count": sum(r.get("counterevidence_qualified") is True for r in s["relation_review"].values()),
                "relation_model_issue_count": sum(bool(r.get("model_issues")) for r in s["relation_review"].values()),
                "sweep_recovery": {"nodes": recovery_summary, "cached_validated_leaf_paragraph_ids": cached_leaf_ids,
                                   "meaning": "Validated child output is cached; source status is applied only after all descendants of the original window validate."},
                "prior_relations_pending": len(engine.prepared.get("prior_relations", [])),
                "identity_retrieval": s.get("identity_retrieval"),
                "context_selection": s.get("context_selection", {}),
                "source_issues": {"status": "nonparagraph_attachments_pending", "details": engine.prepared.get("source_manifest", {}).get("nonprose_source"),
                                  "source_graph_path": engine.prepared.get("source_manifest", {}).get("source_graph_path"), "full_pdf_semantic_coverage_verified": False},
                "completeness_meaning": "All source units swept and candidate stages processed; not a guarantee of exhaustive semantic recall."}
    graph = {"book_id": engine.prepared.get("book_id"), "book_title": engine.prepared.get("book_title"),
             "status": s["status"], "schema": engine.schema, "concepts": concepts, "relations": relations,
             "human_status": "unreviewed", "source_manifest": engine.prepared.get("source_manifest", {})}
    save(root / "graph_auto.json", graph)
    save(root / "coverage.json", coverage)
    save(root / "manifest.json", {"fingerprint": engine.fingerprint, "status": s["status"], "book_id": engine.prepared.get("book_id"),
                                 "source_manifest": engine.prepared.get("source_manifest", {}), "schema_sha256": digest(engine.schema),
                                 "model": engine.config.get("llm", {}).get("model"), "human_status": "unreviewed"})
    if not engine.requests.path.exists():
        save(engine.requests.path, engine.requests.usage)
    for name, rows in [("mentions", mentions), ("definitions", definitions), ("evidence", evidence),
                       ("decisions", s["decisions"]), ("review_queue", queue),
                       ("sources", list(engine.paragraphs.values())), ("source_mappings", engine.prepared.get("source_mappings", [])),
                       ("prior_candidates", [{"kind": "concept", **c} for c in engine.prepared.get("prior_concepts", [])] + [{"kind": "relation", **r} for r in engine.prepared.get("prior_relations", [])])]:
        jsonl(root / (name + ".jsonl"), rows)
    csv_file(root / "concepts.csv", concepts, ["id", "name", "entity_type", "scope", "definition", "aliases", "member_ids", "auto_status", "human_status"])
    csv_file(root / "relations.csv", relations, ["id", "head", "tail", "relation", "conditions", "member_ids", "auto_status", "human_status"])
    counts = Counter(row["kind"] for row in queue)
    report = f"""# 自动概念图谱质量报告

状态：{s['status']}。人工审核状态：unreviewed。

- 原文单元：{coverage['source_units_reviewed']} / {coverage['source_units_total']} 已扫描。
- 候选概念：{len(s['admission'])} / {len(s['candidates'])} 已裁决；自动规范概念：{len(concepts)}。
- 候选关系：{len(s['relation_review'])} / {len(s['relations'])} 已裁决；自动规范关系：{len(relations)}。
- 请求累计：{engine.requests.usage['requests']}；累计计费用量（未知用量保守预留）：{engine.requests.usage['charged_tokens']}。
- 待人工处理记录：{len(queue)}；分类：{dumps(dict(counts))}。
- 失败批次：{len(s['failures'])}；暂停原因：{s.get('pause_reason', '')}。
- 原始窗口完成：{coverage['original_source_windows_completed']} / {coverage['original_source_windows_total']}；自适应恢复节点：{len(recovery_nodes)}。
- 已知候选的未证实提及：{len(known_mention_issues)}；原始引文与失败原因保留于coverage.json/decisions.jsonl，未作为纳入依据。
- 一次结构修复后隔离的新候选：{len(quarantined_candidates)}；合格反证：{coverage['qualified_counterevidence_count']}；关系模型输出问题：{coverage['relation_model_issue_count']}。

自动纳入要求当前原文存在性支持；定义缺失不导致概念自动排除。模糊对齐只提供候选上下文。
同名和相似度不构成合并证明；规范组保留成员、代表和决策。关系端点均须独立纳入。
BGE-M3仅用于候选召回；同义组要求每对成员明确同义裁决。身份未决候选不进入自动图。
冲突关系不按多数票纳入，条件差异不自动视为冲突。当前导出仅聚合条件文本规范化后完全相同的重复关系。
全量处理完成不代表语义召回率达到100%。候选检索范围有限；超长单段保留完整文本并可能触发请求大小限制。
失败窗口可按完整段落递归拆分，父失败及费用保留。单段失败或达到最大拆分深度仍明确未完成；跨拆分边界的语义召回可能受限。
代码、公式、图表标题等非段落附件仍待单独处理；不能据此声称覆盖整本PDF的全部语义。
所有人工状态保持未审，不改写历史人工验收文件。
"""
    (root / "quality_report.md").write_text(report, encoding="utf-8")
    viewer_data = dumps({"graph": graph, "sources": engine.paragraphs, "coverage": {k: v for k, v in coverage.items() if k != "source_units"}}).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = r"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>教材概念图谱</title>
<style>body{font:16px system-ui;margin:2rem;background:#f6f7f9;color:#18212c}input,button{font:inherit;padding:.6rem}article{background:white;border:1px solid #ddd;padding:1rem;margin:.6rem 0}pre{white-space:pre-wrap}small{color:#536477}h1{font-size:1.5rem}</style>
<h1>教材概念图谱 · 自动结果 / 人工未审</h1><p id="status"></p><input id="q" placeholder="搜索概念、关系或原文"><button id="concepts">概念</button><button id="relations">关系</button><button id="sources">原文</button><p id="count"></p><main></main>
<script id="data" type="application/json">DATA</script><script>
const D=JSON.parse(document.getElementById('data').textContent),G=D.graph;let mode='concepts';
const names=Object.fromEntries(G.concepts.map(c=>[c.id,c.name]));
document.getElementById('status').textContent=(G.book_title||'')+' · '+G.status+' · '+D.coverage.source_units_reviewed+'/'+D.coverage.source_units_total+' 原文单元已扫描';
function add(parent,tag,text){const el=document.createElement(tag);el.textContent=text;parent.append(el);return el}
function render(){const q=document.getElementById('q').value.toLowerCase(),root=document.querySelector('main');root.replaceChildren();
const rows=(mode==='sources'?Object.values(D.sources):G[mode]).filter(x=>JSON.stringify(x).toLowerCase().includes(q)||((names[x.head]||'')+' '+(names[x.tail]||'')).toLowerCase().includes(q));
document.getElementById('count').textContent=rows.length+' 条结果（显示前200条，请搜索缩小范围）';
rows.slice(0,200).forEach(x=>{const a=add(root,'article','');add(a,'h3',mode==='concepts'?x.name:mode==='relations'?(names[x.head]||x.head)+' → '+x.relation+' → '+(names[x.tail]||x.tail):(x.path_titles||[]).join(' / '));
add(a,'small',x.id);add(a,'pre',mode==='sources'?x.text:mode==='concepts'?(x.definition||'定义待补充')+'\n范围：'+(x.scope||''):'条件：'+(x.conditions||'未指定'));
(x.evidence||[]).forEach(e=>{const d=add(a,'details','');add(d,'summary','原文证据 · '+e.paragraph_id);add(d,'pre',e.text||e.quote);const p=D.sources[e.paragraph_id];if(p)add(d,'pre',p.text)});});}
['concepts','relations','sources'].forEach(m=>document.getElementById(m).onclick=()=>{mode=m;render()});document.getElementById('q').oninput=render;render();</script></html>""".replace("DATA", viewer_data)
    (root / "viewer.html").write_text(html, encoding="utf-8")
