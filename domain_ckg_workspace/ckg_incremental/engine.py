from __future__ import annotations

import copy
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .runtime import ConceptIndex, OutputLimit, digest, dumps, save, semantic_fields
from .sources import SourceCorpus
from .graph_merge import graph_plan
from .semantic_merge import SemanticMerger
from .store import GraphStore, StaleGraphError, GraphValidationError


# 先完成计算机组成/系统结构相关教材，再继续原清单中的其他教材。
# 这些 ID 来自已冻结的 24 本输入；输入指纹不变，已有检查点仍可复用。
PRIORITY_BOOK_IDS = (
    "book_49bb8d31ddb9",  # 计算机组成与设计（李伯成、顾新）
    "book_bcdf0a6ec957",  # 微型计算机原理及接口技术
    "book_25f718562685",  # 计算机组成与系统结构（裘雪红）
    "book_efaf8522b95d",  # 计算机系统结构
    "book_bef9d155e6d8",  # 深入理解计算机系统（第 3 版）
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def exact_ids(rows, expected, field):
    require(isinstance(rows, list) and all(isinstance(r, dict) for r in rows), "Expected object array")
    ids = [r.get(field) for r in rows]
    require(len(ids) == len(set(ids)) and set(ids) == set(expected), f"{field} must match all input IDs exactly once")


def refs(rows, allowed, field="evidence_ids", required=False):
    for row in rows:
        ids = row.get(field, [])
        require(isinstance(ids, list) and all(isinstance(x, str) for x in ids), f"{field} must be a string array")
        unknown = sorted(set(ids) - allowed)
        require(not unknown and (bool(ids) or not required),
                f"{field} contains unknown evidence IDs {unknown[:12]}; cite only IDs present in the supplied source_evidence and remove or correct these references")


def claim_nodes(claim):
    content = claim.get("content", claim)
    return {content.get("node_id")} if content.get("kind") == "node_fact" else {content.get("head"), content.get("tail")}


class IncrementalEngine:
    def __init__(self, corpus, output, schema, config, runtime, projection=None):
        self.root, self.schema, self.config, self.runtime = Path(output), schema, config, runtime
        self.source = SourceCorpus(corpus)
        self.store = GraphStore(self.root / "graph.sqlite")
        self._analysis_snapshot = None
        self.projection = projection
        self.index = ConceptIndex(runtime)
        self.semantic = SemanticMerger(runtime, self.source.evidence, {b["id"]: b for b in corpus["books"]})
        self.last_projection = 0.0
        self.started = time.time()
        self._maintenance_future = None
        self._maintenance_batch = []
        self._maintenance_dirty = set()
        self.fingerprint = digest({"sources": corpus["fingerprint"], "schema": schema,
            "runtime": runtime.fingerprint, "domain": config["domain"],
            "engine": {p.name: p.read_text("utf-8") for p in Path(__file__).parent.glob("*.py")},
            "semantic_config": {k: v for k, v in config.items() if k not in {"workers", "projection_seconds"}}})
        meta = self.root / "run_manifest.json"
        self.compatible_fingerprints = {self.fingerprint}
        if meta.exists():
            manifest = json.loads(meta.read_text("utf-8"))
            manifest_fingerprint = manifest["fingerprint"]
            if manifest_fingerprint != self.fingerprint:
                require(os.environ.get("CKG_ALLOW_COMPATIBLE_RESUME") == "1",
                        "Inputs, prompts or semantic code changed; use a new output directory")
                # Reviewed code changes preserve completed chunks and their original fingerprints.
                self.compatible_fingerprints.add(manifest_fingerprint)
                self.compatible_fingerprints.update(manifest.get("compatible_fingerprints", []))
                manifest["compatible_fingerprints"] = sorted(self.compatible_fingerprints)
                manifest["fingerprint"] = self.fingerprint
                save(meta, manifest)
            # 兼容来源仅由显式迁移写入；完成片段保留原指纹，不重标历史数据。
            self.compatible_fingerprints.update(manifest.get("compatible_fingerprints", []))
        else:
            save(meta, {"fingerprint": self.fingerprint, "model": config["llm"]["model"],
                        "embedding_model": config["embedding"]["model"], "books": corpus["books"],
                        "graph_kind": "working", "human_accepted": False})
        self.store.add_sources(corpus["books"], corpus["evidence"])

    def _snapshot(self):
        # 分析任务共享只读快照；提交改变版本后再读取新图。
        if self._analysis_snapshot is None or self._analysis_snapshot["version"] != self.store.version:
            previous = self._analysis_snapshot
            current = self.store.snapshot(include_sources=False, share_records=True)
            if previous is not None:
                # 跨版本复用未变化的只读记录，避免并发任务各保留一份整图数据。
                for table in ("nodes", "claims", "books"):
                    old = previous[table]
                    for identifier, record in current[table].items():
                        if old.get(identifier) == record:
                            current[table][identifier] = old[identifier]
            self._analysis_snapshot = current
        return self._analysis_snapshot

    def _batch_metadata(self, chunk):
        return {key: chunk.get(key) for key in (
            "book_id", "title_path", "section_id", "section_segment_id", "part_index", "part_count",
            "evidence_ids", "context_evidence_ids", "core_chars", "context_chars", "context_is_partial_paragraph",
            "split_parent_id", "split_part_index", "split_part_count", "source_chunks"
        ) if key in chunk}

    def _batched_chunk(self, group):
        if len(group) == 1:
            return group[0]
        primary = list(dict.fromkeys(eid for chunk in group for eid in chunk["evidence_ids"]))
        context = list(dict.fromkeys(eid for chunk in group for eid in chunk.get("context_evidence_ids", [])))
        primary_set = set(primary)
        context = [eid for eid in context if eid not in primary_set]
        title_path = list(group[0]["title_path"])
        for chunk in group[1:]:
            shared = 0
            for left, right in zip(title_path, chunk["title_path"]):
                if left != right:
                    break
                shared += 1
            title_path = title_path[:shared]
        return {"id": "source_batch_" + digest([chunk["id"] for chunk in group])[:28],
                "book_id": group[0]["book_id"], "chapter_id": group[0]["chapter_id"],
                "title_path": title_path, "evidence_ids": primary, "context_evidence_ids": context,
                "core_chars": sum(chunk["core_chars"] for chunk in group),
                "context_chars": sum(len(self.source.evidence[eid]["text"]) for eid in context),
                "source_chunks": [{"chunk_id": chunk["id"], "title_path": chunk["title_path"],
                                   "section_segment_id": chunk["section_segment_id"],
                                   "evidence_ids": chunk["evidence_ids"]} for chunk in group]}

    def _source_rows(self, chunk, rows=None):
        primary = set(chunk["evidence_ids"])
        overlap = set(chunk.get("context_evidence_ids", [])) - primary
        if rows is None:
            rows = self.source.rows(list(dict.fromkeys(chunk.get("context_evidence_ids", []) + chunk["evidence_ids"])))
        unique = {row["id"]: row for row in rows}
        ordered = sorted(unique.values(), key=lambda row: self.source.source_order[row["id"]])
        return [dict(row, role="primary" if row["id"] in primary else
                     "overlap_context" if row["id"] in overlap else "expanded_context") for row in ordered]

    def _pending_targets(self, chunk, snapshot):
        baseline = chunk.get("target_claim_revisions", {})
        result = []
        for cid in chunk.get("target_claim_ids", []):
            require(cid in snapshot["claims"], "Targeted claim is absent from the graph")
            claim = snapshot["claims"][cid]
            if claim["status"] == "active" and claim.get("revision", 0) <= baseline.get(cid, claim.get("revision", 0)):
                result.append(cid)
        return result

    def _split_analysis(self, chunk, snapshot):
        ids = chunk["evidence_ids"]
        if len(ids) < 2:
            raise OutputLimit("A single primary evidence unit exceeds model output capacity")
        cut = len(ids) // 2
        children = []
        for index, (suffix, part, context) in enumerate((
            ("_a", ids[:cut], chunk.get("context_evidence_ids", [])),
            ("_b", ids[cut:], ids[max(0, cut - 1):cut]),
        ), 1):
            section = self.source.section_positions[part[0]][0]
            context = [eid for eid in context if eid not in part and self.source.section_positions[eid][0] == section]
            child = dict(chunk, id=chunk["id"] + suffix, evidence_ids=part,
                         context_evidence_ids=context, split_parent_id=chunk["id"], split_part_index=index, split_part_count=2,
                         core_chars=sum(len(self.source.evidence[eid]["text"]) for eid in part),
                         context_chars=sum(len(self.source.evidence[eid]["text"]) for eid in context))
            if chunk.get("source_chunks"):
                child["source_chunks"] = [dict(source, evidence_ids=[eid for eid in source["evidence_ids"] if eid in part])
                                          for source in chunk["source_chunks"] if set(source["evidence_ids"]) & set(part)]
            # 定向核查依赖完整证据，待子片段处理后单独落实，避免强迫每半段重复裁决。
            for key in ("target_claim_ids", "target_claim_revisions", "review_results"):
                child.pop(key, None)
            children.append(child)
        return {"chunk": chunk, "split_plans": [self.analyze(child, snapshot) for child in children]}

    def _progress(self, phase, **extra):
        stats = {"phase": phase, "graph_version": self.store.version, "updated_unix": time.time(),
                 "elapsed_seconds": round(time.time() - self.started), "usage": self.runtime.stats(), **extra}
        save(self.root / "run_state.json", stats)
        print(json.dumps(stats, ensure_ascii=True), flush=True)

    def seed(self):
        if self._snapshot()["nodes"]:
            return
        payload = {"domain": self.config["domain"], "schema": self.schema,
                   "max_concepts": self.config.get("prior_concepts", 60),
                   "max_relations": self.config.get("prior_relations", 100)}

        def validate(result):
            nodes = result.get("concepts", [])
            require(bool(nodes), "G0 needs at least one concept")
            ids = [n["id"] for n in nodes]
            require(len(ids) == len(set(ids)), "Duplicate G0 concept ID")
            for n in nodes:
                require(n.get("entity_type") in self.schema["entity_types"], "Unknown G0 entity type")
                require(not n.get("evidence_ids"), "G0 must not claim textbook evidence")
                require(isinstance(n.get("aliases", []), list), "Aliases must be an array")
            for r in result.get("relations", []):
                require(r["head"] in ids and r["tail"] in ids, "Invalid G0 endpoints")
                require(not r.get("evidence_ids"), "G0 must not claim textbook evidence")
        prior = copy.deepcopy(self.runtime.call("prior", payload, validate))
        nodes = {node["id"]: node for node in prior["concepts"]}
        relations, omitted = [], []
        for relation in prior.get("relations", []):
            content = {key: relation[key] for key in ("head", "relation", "tail", "conditions") if key in relation}
            try:
                GraphStore._validate_content(dict(content, kind="relation"), nodes,
                                             set(self.schema["entity_types"]), set(self.schema["relation_types"]))
            except GraphValidationError as exc:
                omitted.append(dict(relation=relation, reason=str(exc)))
            else:
                relations.append(relation)
        prior["relations"] = relations
        if omitted:
            save(self.root / "g0_omitted_relations.json", omitted)
        save(self.root / "g0.json", prior)
        self.store.seed_prior(prior, self.schema)

    def _context(self, snapshot, nodes):
        ids = {n["id"] for n in nodes}
        claims = [c for c in snapshot["claims"].values()
                  if c.get("status") != "retired" and claim_nodes(c) & ids]
        # 输入当前可讨论的断言，避免把全图堆入每次提示词。
        claims = claims[:int(self.config.get("context_claims", 100))]
        ids.update(nid for c in claims for nid in claim_nodes(c) if nid)
        return {"nodes": [snapshot["nodes"][nid] for nid in sorted(ids) if nid in snapshot["nodes"]],
                "claims": claims}

    def _concepts(self, evidence, snapshot, request_id, retry_note=None, source_batch=None):
        text = "\n".join(e["text"] for e in evidence)
        context = self._context(snapshot, self.index.for_text(text))
        short_by_id = {row["id"]: f"e{index + 1}" for index, row in enumerate(evidence)}
        id_by_short = {short: source_id for source_id, short in short_by_id.items()}

        def short_ids(value):
            if isinstance(value, dict):
                return {key: short_ids(item) for key, item in value.items()}
            if isinstance(value, list):
                return [short_ids(item) for item in value]
            return short_by_id.get(value, value) if isinstance(value, str) else value

        prompt_evidence = [dict(row, id=short_by_id[row["id"]]) for row in evidence]
        payload = {"request_id": request_id, "graph_version": snapshot["version"], "schema": self.schema,
                   "source_evidence": prompt_evidence, "local_graph": context, "max_concepts": 60}
        if source_batch:
            payload["source_batch"] = short_ids(source_batch)
            payload["coverage_instruction"] = (
                "完整处理 role=primary 的正文；overlap_context 与 expanded_context 用于跨块理解和关系端点。"
                "仅在上下文出现且与本段知识无关的概念不必重复抽取；上下文原文仍可作为跨块关系的证据。"
            )
            if source_batch.get("source_chunks"):
                payload["coverage_instruction"] += (
                    "source_chunks 是一次请求中的多个独立教材内容节点；逐节点阅读，"
                    "按各自 evidence_ids 引用原文，不把相邻节点或目录顺序当成语义关系。"
                )
        if retry_note:
            payload["completion_instruction"] = short_ids(retry_note)
        allowed = set(id_by_short)

        def validate(result):
            returned_id = result.get("request_id")
            if returned_id != request_id:
                require(isinstance(returned_id, str) and request_id.startswith(returned_id + "_")
                        and result.get("graph_version") == snapshot["version"], "Return the supplied request_id")
            nodes = result.get("concepts")
            require(isinstance(nodes, list), "concepts must be an array")
            ids = [n["query_id"] for n in nodes]
            require(len(ids) == len(set(ids)), "Duplicate query_id")
            refs(nodes, allowed, required=True)
            refs(nodes, allowed, "definition_evidence_ids")
            normalized = []
            for n in nodes:
                require(n["entity_type"] in self.schema["entity_types"], "Unknown entity type")
                require(isinstance(n["name"], str) and bool(n["name"].strip()), "Empty concept name")
                require(isinstance(n.get("aliases"), list) and all(isinstance(a, str) for a in n["aliases"]), "Invalid aliases")
                require(n.get("definition_status") in {"explicit", "summarized", "partial", "not_provided"}, "Invalid definition status")
                if (n["definition_status"] == "partial" and
                        (n.get("definition") is None or n.get("definition") == "") and
                        not n.get("definition_evidence_ids")):
                    # 无定义文本和定义证据时仅保留提及；原响应仍在 attempts 中。
                    normalized.append({"query_id": n["query_id"], "from_status": "partial", "to_status": "not_provided"})
                    n["definition"] = None
                    n["definition_status"] = "not_provided"
                require(set(n.get("definition_evidence_ids", [])) <= set(n["evidence_ids"]), "Definition evidence must support this concept")
                if n["definition_status"] == "not_provided":
                    require(n.get("definition") is None and not n.get("definition_evidence_ids"), "Mention is not definition evidence")
                else:
                    require(isinstance(n.get("definition"), str) and bool(n.get("definition_evidence_ids")), "Definition needs text and evidence")
            if normalized:
                result["normalization_audit"] = {"policy": "empty_partial_definition_to_mention_v1",
                                                 "normalized_concepts": normalized}
            refs(result.get("context_requests", []), allowed, required=True)
            coverage = result.get("coverage", {})
            require(set(coverage.get("remaining_evidence_ids", [])) <= allowed, "Unknown remaining evidence")
            if returned_id != request_id:
                # 仅在子请求的证据和版本均已通过校验后修正父请求 ID。
                result["request_id"] = request_id
                result.setdefault("normalization_audit", {})["request_id_correction"] = {
                    "policy": "parent_request_id_suffix_v1", "returned": returned_id, "expected": request_id}
        try:
            result = self.runtime.call("concepts", payload, validate)
            limited = result.get("coverage", {}).get("output_limit_reached") or result.get("coverage", {}).get("remaining_evidence_ids")
        except OutputLimit:
            result, limited = None, True
        if limited:
            if len(evidence) < 2:
                raise OutputLimit("A single evidence unit still exceeds output capacity")
            cut = len(evidence) // 2
            left = self._concepts(evidence[:cut], snapshot, request_id + "_a", retry_note, source_batch)
            right = self._concepts(evidence[cut:], snapshot, request_id + "_b", retry_note, source_batch)
            result = {"concepts": left["concepts"] + right["concepts"],
                      "context_requests": left.get("context_requests", []) + right.get("context_requests", [])}
        else:
            result = copy.deepcopy(result)
            for item in result["concepts"] + result.get("context_requests", []):
                item["evidence_ids"] = [id_by_short[eid] for eid in item["evidence_ids"]]
                if "definition_evidence_ids" in item:
                    item["definition_evidence_ids"] = [id_by_short[eid] for eid in item["definition_evidence_ids"]]
        for i, node in enumerate(result["concepts"]):
            node["query_id"] = request_id + ":q" + str(i)
        return result

    def _identity_response(self, payload, local=False):
        byid = {query["query_id"]: query for query in payload["queries"]}

        def validate(response):
            rows = response.get("results")
            require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "Expected object array")
            kept, rejected, duplicates = {}, 0, set()
            for row in rows:
                query_id = row.get("query_id")
                if not isinstance(query_id, str) or query_id not in byid or query_id in duplicates:
                    rejected += 1
                    continue
                if query_id in kept:
                    kept.pop(query_id)
                    duplicates.add(query_id)
                    rejected += 2
                    continue
                query = byid[query_id]
                try:
                    require(row["decision"] in {"reuse", "new", "unresolved"}, "Unknown identity decision")
                    if row["decision"] == "reuse":
                        candidates = {candidate["id"] for candidate in query["candidates"]}
                        message = "Invalid batch candidate" if local else "Identity selected ID outside its candidates"
                        require(row.get("canonical_id") in candidates,
                                f"{message}; query_id={query_id}; allowed canonical_id={sorted(candidates)}")
                    elif not local:
                        require(row.get("canonical_id") is None, "Only reuse may select an ID")
                    refs([row], {e["id"] for e in query["evidence"]}, required=True)
                except (ValueError, KeyError, TypeError):
                    rejected += 1
                    continue
                kept[query_id] = row
            require(kept or len(byid) > 1, "No valid identity result for requested ID")
            response["results"] = list(kept.values())
            missing = [query_id for query_id in byid if query_id not in kept]
            if rejected or missing:
                response["normalization_audit"] = {
                    "policy": "retain_valid_identity_results_v1", "rejected_count": rejected,
                    "retry_query_ids": missing,
                }

        try:
            result = self.runtime.call("identity", payload, validate)
        except (ValueError, OutputLimit):
            queries = payload["queries"]
            if len(queries) < 2:
                raise
            # 完全失败时缩小批次；每个请求项仍由模型判断。
            half = len(queries) // 2
            rows = []
            for index, part in enumerate((queries[:half], queries[half:])):
                smaller = dict(payload, queries=part, request_id=payload["request_id"] + f"_retry{index}")
                rows.extend(self._identity_response(smaller, local)["results"])
            return {"request_id": payload["request_id"], "graph_version": payload["graph_version"], "results": rows}
        missing = [query for query in payload["queries"] if query["query_id"] not in
                   {row["query_id"] for row in result["results"]}]
        if missing:
            if len(missing) == len(payload["queries"]):
                half = len(missing) // 2
                parts = (missing[:half], missing[half:])
            else:
                parts = (missing,)
            for index, part in enumerate(parts):
                smaller = dict(payload, queries=part, request_id=payload["request_id"] + f"_missing{index}")
                result["results"].extend(self._identity_response(smaller, local)["results"])
        exact_ids(result["results"], byid, "query_id")
        return result

    def _identity(self, concepts, evidence, snapshot, request_id, radius=0):
        candidates = self.index.retrieve(concepts, self.config.get("identity_top_k", 8) * (radius + 1))
        queries = []
        for node, recalled in zip(concepts, candidates):
            options = [candidate for candidate in recalled if candidate["id"] in snapshot["nodes"]]
            evidence_limit = 2 * (radius + 1)
            ids = list(dict.fromkeys(node["evidence_ids"] +
                [eid for c in options for eid in c.get("definition_evidence_ids", [])[:evidence_limit]]))
            ids = [eid for eid in ids if eid in self.source.evidence]
            extra = self.source.expand(node["evidence_ids"], max(1, radius * 2))
            rows = {e["id"]: e for e in self.source.rows(ids) + extra}
            if radius:
                rows.update({e["id"]: e for e in self.source.search(node["name"], 4, evidence[0]["book_id"])})
            queries.append({"query_id": node["query_id"], "concept": node, "evidence": list(rows.values()),
                            "candidates": options, "canonical_map": {}, "recall_status": "completed",
                            "evidence_scope": "候选自身原文及同小节相邻上下文；已有概念定义证据按需补读，并非全部历史证据"})
        results = []
        batch_size = int(self.config.get("identity_batch_size", 10))
        for offset in range(0, len(queries), batch_size):
            batch = queries[offset:offset + batch_size]
            payload = {"request_id": request_id + f"_i{offset}_{radius}", "graph_version": snapshot["version"], "queries": batch}
            response = self._identity_response(payload)
            results.extend(response["results"])
        unresolved = {r["query_id"] for r in results if r["decision"] == "unresolved"}
        if unresolved:
            if radius < int(self.config.get("context_rounds", 3)):
                replacements, _ = self._identity([c for c in concepts if c["query_id"] in unresolved],
                                                 evidence, snapshot, request_id, radius + 1)
                results = [r for r in results if r["query_id"] not in unresolved] + replacements
        signatures = {q["query_id"]: digest([semantic_fields(c) | {"id": c["id"]} for c in q["candidates"]]) for q in queries}
        return results, signatures

    def _within_chunk(self, concepts, evidence, snapshot, request_id, source_chunks=None):
        """仅对同批相似候选追加一次身份判断，避免拆片后重复建点。"""
        if len(concepts) < 2:
            return concepts
        vectors = self.runtime.embed([dumps(semantic_fields(c))[:7000] for c in concepts])
        owners = None
        if source_chunks:
            evidence_owner = {eid: index for index, source in enumerate(source_chunks)
                              for eid in source["evidence_ids"]}
            owners = [{evidence_owner[eid] for eid in concept["evidence_ids"] if eid in evidence_owner}
                      for concept in concepts]
        queries, links = [], {}
        for i, node in enumerate(concepts):
            if not i:
                continue
            scores = vectors[:i] @ vectors[i]
            eligible = (range(i) if owners is None else
                        [j for j in range(i) if owners[i] and owners[j] & owners[i]])
            positions = sorted(eligible, key=lambda j: -float(scores[j]))[:4]
            positions = [j for j in positions if float(scores[j]) >= 0.55 or concepts[j]["name"] == node["name"]]
            if not positions:
                continue
            options = []
            for j in positions:
                cid = "batch_" + str(j)
                links[cid] = j
                options.append(dict(concepts[j], id=cid, node_kind="concept"))
            related_ids = list(dict.fromkeys(node["evidence_ids"] + [eid for option in options for eid in option["evidence_ids"]]))
            query_evidence = self.source.expand(related_ids, 1)
            queries.append({"query_id": node["query_id"], "concept": node, "candidates": options,
                            "canonical_map": {}, "recall_status": "completed", "evidence": query_evidence})
        if not queries:
            return concepts
        payload = {"request_id": request_id + "_within_chunk", "graph_version": snapshot["version"], "queries": queries}
        response = self._identity_response(payload, local=True)
        decisions = {r["query_id"]: r for r in response["results"]}
        kept, merged_indices = {}, set()
        for i, node in enumerate(concepts):
            row = decisions.get(node["query_id"], {})
            target = links.get(row.get("canonical_id")) if row.get("decision") == "reuse" else None
            # 只接受直接指向仍独立保留的概念，禁止把链式相似关系传递合并。
            if target is not None and target in kept and target not in merged_indices:
                chosen = kept[target]
                chosen["evidence_ids"] = list(dict.fromkeys(chosen["evidence_ids"] + node["evidence_ids"]))
                chosen["aliases"] = list(dict.fromkeys(chosen.get("aliases", []) + node.get("aliases", []) +
                                        ([node["name"]] if node["name"] != chosen["name"] else [])))
                merged_indices.add(i)
            else:
                kept[i] = copy.deepcopy(node)
        return list(kept.values())

    def _allowed(self, concepts, identity, snapshot, request_id):
        byid = {r["query_id"]: r for r in identity}
        allowed, source_nodes = {}, []
        for c in concepts:
            decision = byid[c["query_id"]]
            if decision["decision"] == "unresolved":
                continue
            if decision["decision"] == "reuse":
                nid = decision["canonical_id"]
                node = copy.deepcopy(snapshot["nodes"][nid])
                node.update(lifecycle="existing", identity_status="resolved")
                # 本段提及与旧定义证据分别保留，Store 仅追加当前来源的提及。
                node["evidence_ids"] = list(dict.fromkeys(node.get("evidence_ids", []) + c["evidence_ids"]))
            else:
                nid = "temp_" + digest([request_id, c["query_id"]])[:24]
                node = dict(c, id=nid, lifecycle="provisional", identity_status="resolved", node_kind="concept")
            if nid in allowed:
                node["evidence_ids"] = list(dict.fromkeys(allowed[nid].get("evidence_ids", []) + node.get("evidence_ids", [])))
            allowed[nid] = node
            source_nodes.append(dict(c, resolved_id=nid))
        return list(allowed.values()), source_nodes

    def _updates(self, concepts, identity, evidence, snapshot, request_id, completion=None, target_claim_ids=(), source_batch=None):
        allowed, source_nodes = self._allowed(concepts, identity, snapshot, request_id)
        context = self._context(snapshot, [n for n in allowed if n["lifecycle"] == "existing"])
        known_claims = {c["id"] for c in context["claims"]}
        for cid in target_claim_ids:
            if cid in snapshot["claims"] and cid not in known_claims:
                claim = snapshot["claims"][cid]
                context["claims"].append(claim)
                context["nodes"].extend(snapshot["nodes"][nid] for nid in claim_nodes(claim))
                known_claims.add(cid)
        # 邻接概念可作为端点，但仍需要当前原文支持关系。
        present = {n["id"] for n in allowed}
        for n in context["nodes"]:
            if n["id"] not in present:
                allowed.append(dict(n, lifecycle="existing", identity_status="resolved"))
                present.add(n["id"])
        old_ids = list(dict.fromkeys(eid for c in context["claims"] for eid in c.get("evidence_ids", [])))
        reference = self.source.rows([eid for eid in old_ids if eid in self.source.evidence][:int(self.config.get("reference_evidence_limit", 30))])
        payload = {"request_id": request_id, "graph_version": snapshot["version"], "schema": self.schema,
            "source_evidence": evidence, "reference_evidence": reference, "allowed_nodes": allowed,
            "source_concepts": source_nodes, "existing_claims": context["claims"], "max_operations": 150}
        if source_batch:
            payload["source_batch"] = source_batch
            if source_batch.get("source_chunks"):
                payload["source_batch_instruction"] = (
                    "本请求合并了多个独立内容节点。每个节点的标题和原文 ID 见 source_chunks；"
                    "仅凭同章相邻或一起提交不能建立概念关系，关系必须由引用的原文直接支持。"
                )
        if target_claim_ids:
            payload["target_claim_ids"] = list(target_claim_ids)
            payload["target_instruction"] = "逐项落实指定断言的复核结论；没有充分证据的项保留原断言并说明具体判断，不编造肯定更新。"
        if completion:
            payload["completion_instruction"] = completion
        source_ids, reference_ids = {e["id"] for e in evidence}, {e["id"] for e in reference}

        def validate(result):
            require(result.get("request_id") == request_id, "Update request_id differs")
            require(result.get("base_graph_version") == snapshot["version"], "Update uses wrong graph version")
            changes = result.get("changes")
            require(isinstance(changes, list), "changes must be an array")
            ids = [p["proposal_id"] for p in changes]
            require(len(ids) == len(set(ids)), "Duplicate proposal_id")
            for p in changes:
                require(p["operation"] in {"add_node", "add_claim", "attach_evidence", "revise_claim", "retire_claim"}, "Unknown graph operation")
                require(isinstance(p.get("payload"), dict), "Update payload must be an object")
                require(isinstance(p.get("group_id"), str) and bool(p["group_id"]), "Each update requires a group_id")
                if p["operation"] in {"attach_evidence", "revise_claim", "retire_claim"}:
                    require(p.get("target_id") in snapshot["claims"] and
                            snapshot["claims"][p["target_id"]]["status"] == "active", "Update requires an existing active claim")
            refs(changes, source_ids, required=True)
            refs(changes, reference_ids, "compared_evidence_ids")
            refs(result.get("follow_up", []), source_ids, required=True)
            if result.get("coverage", {}).get("output_limit_reached") or result.get("coverage", {}).get("remaining_evidence_ids"):
                raise OutputLimit("Update output requires a smaller source batch")
            created = {p["target_id"] for p in changes if p["operation"] == "add_node"}
            followed = {nid for f in result.get("follow_up", []) for nid in f.get("related_ids", [])}
            provisional = {n["id"]: n for n in allowed if n["lifecycle"] == "provisional"}
            claim_groups = {}
            for change in changes:
                if change["operation"] != "add_claim":
                    continue
                for endpoint in claim_nodes(change["payload"].get("claim", {})) & provisional.keys():
                    claim_groups.setdefault(endpoint, set()).add(change["group_id"])
            rejected = {nid for item in result.get("follow_up", [])
                        if item.get("kind") in {"node_not_admitted", "identity_unresolved", "identity_split_required"}
                        for nid in item.get("related_ids", [])}
            generated = []
            existing_ids = set(ids)
            for nid, groups in sorted(claim_groups.items()):
                if nid in created or nid in rejected or len(groups) != 1:
                    continue
                evidence_ids = sorted(set(provisional[nid].get("evidence_ids", [])) & source_ids)
                if not evidence_ids:
                    continue
                proposal_id = "implied_node_" + nid
                if proposal_id in existing_ids:
                    continue
                generated.append({"proposal_id": proposal_id, "group_id": next(iter(groups)),
                                  "operation": "add_node", "target_id": nid, "payload": {},
                                  "evidence_ids": evidence_ids, "compared_evidence_ids": [],
                                  "reason": "Introduce the source-evidenced provisional endpoint used by this model-proposed claim."})
                created.add(nid)
            if generated:
                # Raw provider output remains in attempts; only the validated derived response is normalized.
                result["changes"] = generated + changes
                result["normalization_audit"] = {"policy": "implied_claim_endpoint_node_v1",
                    "generated_changes": copy.deepcopy(generated), "raw_change_count": len(changes)}
            missing = set(provisional) - created - followed
            require(not missing, "Unintroduced provisional nodes need add_node or specific follow_up: " + ", ".join(sorted(missing)))
            if target_claim_ids:
                addressed = {p.get("target_id") for p in changes}
                require(set(target_claim_ids) <= addressed | followed,
                        "The targeted backward-review claim must actually be addressed")
        proposal = self.runtime.call("updates", payload, validate)
        return {"proposal": proposal, "allowed": allowed, "reference_ids": list(reference_ids),
                "payload": payload, "evidence": evidence, "concepts": concepts, "identity": identity}

    def _review(self, items, snapshot, request_id):
        payload = {"request_id": request_id, "graph_version": snapshot["version"],
                   "schema": self.schema, "review_items": items}
        expected = {i["item_id"]: i for i in items}

        def validate(result):
            exact_ids(result.get("results"), expected, "item_id")
            for row in result["results"]:
                require(row["verdict"] in {"supported", "contradicted", "insufficient", "not_covered", "different_scope", "source_conflict"}, "Unknown evidence verdict")
                require(row["recommendation"] in {"keep", "attach_evidence", "revise", "retire", "split_identity", "retain_source_variants", "defer"}, "Unknown evidence recommendation")
                refs([row], {e["id"] for e in expected[row["item_id"]]["evidence"]})
                if row["recommendation"] == "retire":
                    require(row["verdict"] == "contradicted" and bool(row["evidence_ids"]), "Retirement requires explicit contradiction evidence")
        try:
            return self.runtime.call("review", payload, validate)["results"]
        except OutputLimit:
            if len(items) < 2:
                raise
            cut = len(items) // 2
            return (self._review(items[:cut], snapshot, request_id + "_a") +
                    self._review(items[cut:], snapshot, request_id + "_b"))

    def analyze(self, chunk, snapshot):
        chunk = dict(chunk)
        if chunk.get("target_claim_ids") and "target_claim_revisions" not in chunk:
            chunk["target_claim_revisions"] = {cid: snapshot["claims"][cid].get("revision", 0)
                                               for cid in chunk["target_claim_ids"]}
        try:
            return self._analyze(chunk, snapshot)
        except OutputLimit:
            return self._split_analysis(chunk, snapshot)

    def _analyze(self, chunk, snapshot):
        evidence = self._source_rows(chunk)
        context = self._context(snapshot, self.index.for_text("\n".join(row["text"] for row in evidence)))
        targets = [snapshot["claims"][cid] for cid in chunk.get("target_claim_ids", [])]
        if not chunk.get("target_claim_ids"):
            targets = [c for c in context["claims"] if c["status"] == "active" and
                       c["origin"] == "parametric_prior" and not c["evidence_ids"]][:16]
        if targets:
            target_ids = {c["id"] for c in targets}
            context["claims"] = targets + [c for c in context["claims"] if c["id"] not in target_ids]
            ids = {n["id"] for n in context["nodes"]} | {nid for c in targets for nid in claim_nodes(c)}
            context["nodes"] = [snapshot["nodes"][nid] for nid in sorted(ids)]
        source_ids = {row["id"]: f"e{i + 1}" for i, row in enumerate(evidence)}
        original_ids = {short: eid for eid, short in source_ids.items()}
        reference_ids = list(dict.fromkeys(eid for claim in context["claims"] for eid in claim.get("evidence_ids", [])))
        references = self.source.rows(reference_ids)
        request_id = chunk["id"] + f"_extract_v{snapshot['version']}"

        def short_ids(value):
            if isinstance(value, dict):
                return {key: short_ids(item) for key, item in value.items()}
            if isinstance(value, list):
                return [short_ids(item) for item in value]
            return source_ids.get(value, value) if isinstance(value, str) else value

        payload = dict(request_id=request_id, graph_version=snapshot["version"], schema=self.schema,
                       source_evidence=[dict(id=source_ids[row["id"]], text=row["text"], role=row["role"])
                                        for row in evidence],
                       source_batch=short_ids(self._batch_metadata(chunk)),
                       local_graph={"nodes": [{key: node[key] for key in
                                              ("id", "name", "entity_type", "aliases", "definition", "scope")
                                              if key in node} for node in context["nodes"]],
                                    "claims": [{key: claim[key] for key in ("id", "content", "evidence_ids", "status")}
                                               for claim in context["claims"]]},
                       reference_evidence=[{key: row[key] for key in ("id", "text", "book_id", "title_path")
                                            if key in row} for row in references])
        if targets:
            payload["target_claim_ids"] = [c["id"] for c in targets]
            payload["task"] = (("从 G0 先验反查教材：本次只抽取与 target_claim_ids 相关的概念和关系。"
                                if chunk.get("target_claim_ids") else
                                "完整抽取当前教材，同时用原文补全 target_claim_ids 中的 G0 先验。") +
                               "原文支持的知识直接在 concepts/relations 中给出；明确纠正的知识写入 revisions。"
                               "原文未支持的先验保持原样，不补造证据，不要求每个目标都有输出。")
        supplied_claims = {claim["id"]: claim for claim in context["claims"] if claim["status"] == "active"}
        normalization = dict(duplicate_concepts=0, self_relations=0, unsupported_definitions=0)

        def validate(result):
            require(result.get("request_id") == request_id, "Return the supplied request_id")
            require(type(result.get("graph_version")) is int and result["graph_version"] == snapshot["version"],
                    "Return the supplied integer graph_version")
            concepts, relations = result.get("concepts"), result.get("relations")
            require(isinstance(concepts, list) and isinstance(relations, list), "Expected concepts and relations arrays")
            unique_concepts = list({dumps(node): node for node in concepts}.values())
            normalization["duplicate_concepts"] = len(concepts) - len(unique_concepts)
            concepts = result["concepts"] = unique_concepts
            ids = [node["query_id"] for node in concepts]
            require(len(ids) == len(set(ids)) and all(isinstance(qid, str) and qid for qid in ids), "Invalid query_id")
            refs(concepts, set(original_ids), required=True)
            refs(concepts, set(original_ids), "definition_evidence_ids")
            for node in concepts:
                require(node.get("entity_type") in self.schema["entity_types"], "Unknown entity type")
                require(isinstance(node.get("name"), str) and bool(node["name"].strip()), "Empty concept name")
                require(isinstance(node.get("scope"), str), "scope must be a string")
                require(isinstance(node.get("aliases"), list) and all(isinstance(a, str) and a.strip() for a in node["aliases"]),
                        "Invalid aliases")
                status = node.get("definition_status")
                require(status in {"explicit", "summarized", "partial", "not_provided"}, "Invalid definition status")
                require(set(node["definition_evidence_ids"]) <= set(node["evidence_ids"]), "Definition evidence must support this concept")
                if status != "not_provided" and (not node.get("definition") or not node["definition_evidence_ids"]):
                    normalization["unsupported_definitions"] += 1
                    node.update(definition=None, definition_status="not_provided", definition_evidence_ids=[])
                    status = "not_provided"
                if status == "not_provided":
                    if node.get("definition") is not None or node["definition_evidence_ids"]:
                        normalization["unsupported_definitions"] += 1
                        node.update(definition=None, definition_evidence_ids=[])
                else:
                    require(isinstance(node.get("definition"), str) and bool(node["definition"].strip()) and
                            bool(node["definition_evidence_ids"]), "Definition needs text and evidence")
            endpoints = set(ids) | {node["id"] for node in context["nodes"]}
            require(not (set(ids) & {node["id"] for node in context["nodes"]}), "query_id overlaps an existing node ID")
            refs(relations, set(original_ids), required=True)
            grounded_relations = []
            for relation in relations:
                require(relation.get("relation") in self.schema["relation_types"], "Invalid relation type")
                require(isinstance(relation.get("conditions", ""), str), "Relation conditions must be text")
                if relation.get("head") not in endpoints or relation.get("tail") not in endpoints:
                    normalization.setdefault("unknown_endpoint_relations", []).append(dict(relation))
                    continue
                grounded_relations.append(relation)
            result["relations"] = [row for row in grounded_relations if row["head"] != row["tail"]]
            normalization["self_relations"] = len(grounded_relations) - len(result["relations"])
            revisions = result.get("revisions", [])
            require(isinstance(revisions, list), "revisions must be an array")
            revision_ids = [row["claim_id"] for row in revisions]
            require(len(revision_ids) == len(set(revision_ids)), "Duplicate revised claim")
            refs(revisions, set(original_ids), required=True)
            for row in revisions:
                require(row["claim_id"] in supplied_claims, "Unknown revised claim")
                require(row.get("operation") in {"revise_claim", "retire_claim"}, "Unknown revision operation")
                require(row.get("before") == supplied_claims[row["claim_id"]]["content"], "Revision before differs")
                require(isinstance(row.get("reason"), str) and bool(row["reason"].strip()), "Revision needs a reason")
                if row["operation"] == "revise_claim":
                    after = row.get("after")
                    require(isinstance(after, dict), "Revision after must be an object")
                    keys = ("node_id",) if after.get("kind") == "node_fact" else ("head", "tail")
                    require(all(after.get(key) in endpoints for key in keys), "Unknown revision endpoint")

        extracted = copy.deepcopy(self.runtime.call("extract_graph", payload, validate))
        for row in extracted.get("revisions", []):
            row["observed_revision"] = supplied_claims[row["claim_id"]]["revision"]
        for row in extracted["concepts"] + extracted["relations"] + extracted.get("revisions", []):
            row["evidence_ids"] = [original_ids[eid] for eid in row["evidence_ids"]]
            if "definition_evidence_ids" in row:
                row["definition_evidence_ids"] = [original_ids[eid] for eid in row["definition_evidence_ids"]]
        self.semantic.match(extracted["concepts"], snapshot, extracted["relations"])
        return dict(chunk=chunk, extracted=extracted, evidence=evidence, context_nodes=context["nodes"],
                    snapshot_version=snapshot["version"], normalization=normalization)

    def _rebase(self, plan):
        # 版本变化只重新归并节点和事实，不重复抽取或调用模型。
        snapshot = self._snapshot()
        mapping, audit = self.semantic.match(plan["extracted"]["concepts"], snapshot,
                                              plan["extracted"]["relations"], judge=False)
        rebased = graph_plan(plan["extracted"], plan["chunk"], plan["evidence"], snapshot,
                             plan["context_nodes"], mapping)
        rebased["semantic_matches"] = audit
        qualifiers = self.semantic.name_qualifiers(audit)
        rebased["name_qualifiers"] = {node['id']: qualifiers.get(node.get('query_id'), qualifiers.get(node['id']))
                                      for node in rebased['allowed']
                                      if qualifiers.get(node.get('query_id'), qualifiers.get(node['id']))}
        return rebased

    def _check_revisions(self, plan, depth=0):
        if "split_plans" in plan:
            return plan
        snapshot = self._snapshot()
        risky = [p for p in plan["proposal"]["changes"] if p["operation"] in {"revise_claim", "retire_claim"}]
        if not risky:
            return plan
        items = []
        for p in risky:
            claim = snapshot["claims"][p["target_id"]]
            ids = list(dict.fromkeys(p["evidence_ids"] + claim.get("evidence_ids", [])))
            items.append({"item_id": p["proposal_id"], "proposal": p, "claim": claim,
                "concepts": [snapshot["nodes"][nid] for nid in claim_nodes(claim)],
                "evidence": self.source.rows([eid for eid in ids if eid in self.source.evidence])})
        decisions = self._review(items, snapshot, plan["proposal"]["request_id"] + "_review")
        plan.setdefault("review_decisions", []).append({"kind": "revision_review", "items": items, "results": decisions})
        byid = {d["item_id"]: d for d in decisions}
        acceptable = all(
            (p["operation"] == "retire_claim" and byid[p["proposal_id"]]["verdict"] == "contradicted"
             and byid[p["proposal_id"]]["recommendation"] == "retire") or
            (p["operation"] == "revise_claim" and byid[p["proposal_id"]]["verdict"] in {"supported", "contradicted", "different_scope"}
             and byid[p["proposal_id"]]["recommendation"] == "revise")
            for p in risky)
        if acceptable:
            for p in risky:
                old = snapshot["claims"][p["target_id"]].get("evidence_ids", [])
                # 独立复核已经读到全部旧原文，完整记录实际对照的来源。
                p["compared_evidence_ids"] = list(dict.fromkeys(p.get("compared_evidence_ids", []) + old))
                plan["reference_ids"] = list(dict.fromkeys(plan["reference_ids"] + old))
            return plan
        if depth >= int(self.config.get("context_rounds", 3)):
            # 未获复核支持的原子组保持原图，独立的确定更新仍可提交。
            rejected = {p["group_id"] for p in risky if not (
                (p["operation"] == "retire_claim" and byid[p["proposal_id"]]["verdict"] == "contradicted"
                 and byid[p["proposal_id"]]["recommendation"] == "retire") or
                (p["operation"] == "revise_claim" and byid[p["proposal_id"]]["verdict"] in {"supported", "contradicted", "different_scope"}
                 and byid[p["proposal_id"]]["recommendation"] == "revise"))}
            removed = [p for p in plan["proposal"]["changes"] if p["group_id"] in rejected]
            plan["proposal"]["changes"] = [p for p in plan["proposal"]["changes"] if p["group_id"] not in rejected]
            for p in removed:
                if p["operation"] == "add_node":
                    plan["proposal"].setdefault("follow_up", []).append({
                        "kind": "insufficient_evidence", "related_ids": [p["target_id"]],
                        "statement": p.get("reason", ""), "evidence_ids": p["evidence_ids"],
                        "next_step": "已完成定向复核，证据尚不支持该原子组；保留模型判断与原图。",
                    })
            plan["review_decisions"].append({"kind": "retained_existing_assertions", "removed_proposals": removed,
                                            "results": decisions, "action": "retain_previous_graph"})
            for p in plan["proposal"]["changes"]:
                if p["operation"] in {"revise_claim", "retire_claim"}:
                    old = snapshot["claims"][p["target_id"]].get("evidence_ids", [])
                    p["compared_evidence_ids"] = list(dict.fromkeys(p.get("compared_evidence_ids", []) + old))
                    plan["reference_ids"] = list(dict.fromkeys(plan["reference_ids"] + old))
            return plan
        # 复核结论连同原提案反馈给更新阶段，禁止默默跳过这些知识。
        try:
            revised = self._updates(plan["concepts"], plan["identity"], plan["evidence"], snapshot,
                plan["proposal"]["request_id"] + "_corrected", {"review_results": decisions, "previous_proposal": plan["proposal"],
                "original_directed_review": plan["chunk"].get("review_results")},
                self._pending_targets(plan["chunk"], snapshot), self._batch_metadata(plan["chunk"]))
        except OutputLimit:
            return self._split_analysis(plan["chunk"], snapshot)
        revised.update(chunk=plan["chunk"], snapshot_version=snapshot["version"], candidate_signatures=plan["candidate_signatures"],
                       review_decisions=plan.get("review_decisions", []))
        if revised["proposal"].get("follow_up"):
            chunk = dict(plan["chunk"], review_results=decisions)
            return self._check_revisions(self.analyze(chunk, snapshot), depth + 1)
        return self._check_revisions(revised, depth + 1)

    def _commit_split(self, plan):
        changed, applied, node_map = set(), 0, {}
        for child in plan["split_plans"]:
            result = self._commit(child)
            changed.update(result.get("changed_node_ids", []))
            applied += result.get("applied", 0)
            node_map.update(result.get("node_map", {}))
        fingerprint = (self.store.chunk_state(plan["chunk"]["id"]) or {}).get("fingerprint", self.fingerprint)
        self.store.record_chunk(plan["chunk"]["id"], fingerprint, "complete", {
            "version": self.store.version, "evidence_ids": plan["chunk"]["evidence_ids"], "split": True,
            "child_chunk_ids": [child["chunk"]["id"] for child in plan["split_plans"]]})
        return dict(version=self.store.version, applied=applied, node_map=node_map, changed_node_ids=sorted(changed))

    def _commit(self, plan):
        done = self.store.chunk_state(plan["chunk"]["id"])
        if done and done["status"] == "complete":
            require(done["fingerprint"] in self.compatible_fingerprints, "Completed chunk belongs to different inputs")
            return {"version": done["details"]["version"], "applied": 0, "node_map": {}, "changed_node_ids": []}
        if "split_plans" in plan:
            return self._commit_split(plan)
        normalization = plan.get("normalization", {})
        plan = self._rebase(plan)
        result = self.store.commit(plan["proposal"], plan["allowed"],
                                   [row["id"] for row in plan["evidence"]], plan["reference_ids"], self.schema,
                                   name_qualifiers=plan["name_qualifiers"])
        save(self.root / "decisions" / (plan["chunk"]["id"] + ".json"),
             dict(proposal=plan["proposal"], source_batch=self._batch_metadata(plan["chunk"]), result=result,
                  deferred_revisions=plan["deferred_revisions"], omitted_relations=plan["omitted_relations"],
                  normalization=normalization, semantic_matches=plan["semantic_matches"]))
        self.store.record_chunk(plan["chunk"]["id"], done["fingerprint"] if done else self.fingerprint, "complete", {
            "version": result["version"], "evidence_ids": plan["chunk"]["evidence_ids"],
            "context_evidence_ids": plan["chunk"].get("context_evidence_ids", [])})
        self._maintenance_dirty.update(result["changed_node_ids"])
        if hasattr(self, "_maintenance_state"):
            self._save_maintenance()
        return result

    def _start_maintenance(self, snapshot):
        state_path, dirty_path = self.root / "online_maintenance.json", self.root / "online_dirty_nodes.json"
        self._maintenance_state = (json.loads(state_path.read_text("utf-8")) if state_path.exists() else
                                   dict(completed_batches=0, merged_nodes=0, renamed_nodes=0, redirected_claims=0))
        self._maintenance_dirty.update(json.loads(dirty_path.read_text("utf-8")) if dirty_path.exists()
                                       else snapshot["nodes"])
        self._save_maintenance()

    def _save_maintenance(self):
        self._maintenance_state.update(pending_nodes=len(self._maintenance_dirty),
                                       in_flight_nodes=len(self._maintenance_batch), graph_version=self.store.version,
                                       updated_unix=time.time())
        save(self.root / "online_maintenance.json", self._maintenance_state)
        save(self.root / "online_dirty_nodes.json", sorted(self._maintenance_dirty | set(self._maintenance_batch)))

    def _maintain(self, pool):
        if self._maintenance_future and self._maintenance_future.done():
            mapping, audit, version = self._maintenance_future.result()
            current = self._snapshot()
            if current["version"] != version:
                mapping, audit = self.semantic.reconcile(current, self._maintenance_batch, judge=False)
            result = self.store.merge_nodes(mapping, self.schema, current["version"], self.semantic.name_qualifiers(audit))
            self._maintenance_state["completed_batches"] += 1
            for key in ("merged_nodes", "renamed_nodes", "redirected_claims"):
                self._maintenance_state[key] += result[key]
            self._maintenance_dirty = {mapping.get(nid, nid) for nid in self._maintenance_dirty}
            self._maintenance_dirty.update(row["source_id"] for row in audit if row["method"] == "pending_flash")
            report = dict(result=result, matches=audit)
            save(self.root / "semantic_merge_online_latest.json", report)
            save(self.root / "decisions" / f"online_merge_{self._maintenance_state['completed_batches']}.json", report)
            self._maintenance_future, self._maintenance_batch = None, []
            self._save_maintenance()
        if self._maintenance_future is None and self._maintenance_dirty:
            snapshot = self._snapshot()
            self._maintenance_dirty.intersection_update(snapshot["nodes"])
            self._maintenance_batch = sorted(self._maintenance_dirty)[:32]
            self._maintenance_dirty.difference_update(self._maintenance_batch)
            if self._maintenance_batch:
                def analyze_batch(current, ids):
                    mapping, audit = self.semantic.reconcile(current, ids)
                    return mapping, audit, current["version"]
                self._maintenance_future = pool.submit(analyze_batch, snapshot, list(self._maintenance_batch))
            self._save_maintenance()

    def project(self, force=False):
        if self.projection and (force or time.time() - self.last_projection >= self.config.get("projection_seconds", 300)):
            result = self.projection.sync_store(self.store)
            save(self.root / "database_state.json", result)
            self.last_projection = time.time()

    def _pending_units(self, book_chunks):
        units, group = [], []
        chunk_limit = int(self.config.get("chunk_chars", 4500))
        for chunk in book_chunks:
            state = self.store.chunk_state(chunk["id"])
            require(not state or state["fingerprint"] in self.compatible_fingerprints,
                    "Checkpoint belongs to different inputs")
            complete = bool(state and state["status"] in {"complete", "skipped"})
            short_node = (chunk.get("part_count") == 1 and bool(chunk.get("chapter_id"))
                          and chunk["core_chars"] <= chunk_limit)
            if group and (complete or not short_node or chunk["chapter_id"] != group[0]["chapter_id"]
                          or len(group) == 3 or sum(c["core_chars"] for c in group) + chunk["core_chars"] > chunk_limit):
                units.append((self._batched_chunk(group), group))
                group = []
            if complete:
                continue
            if short_node:
                group.append(chunk)
            else:
                units.append((chunk, [chunk]))
        if group:
            units.append((self._batched_chunk(group), group))
        return units

    def _record_source_batch(self, chunk, source_chunks):
        if len(source_chunks) < 2:
            return
        self.store.record_chunks([
            (source["id"], (self.store.chunk_state(source["id"]) or {}).get("fingerprint", self.fingerprint), "complete",
             {"version": self.store.version, "batch_id": chunk["id"], "evidence_ids": source["evidence_ids"]})
            for source in source_chunks
        ])

    def backward_check(self):
        snapshot = self._snapshot()
        unverified = [c for c in snapshot["claims"].values()
                      if c.get("status") == "active" and c.get("origin") == "parametric_prior" and not c.get("evidence_ids")]
        skipped_total = 0
        for offset in range(0, len(unverified), 8):
            batch, items = unverified[offset:offset + 8], []
            for claim in batch:
                names = [snapshot["nodes"][nid]["name"] for nid in claim_nodes(claim)]
                rows = self.source.search(" ".join(names), 6)
                items.append({"item_id": claim["id"], "claim": claim,
                    "concepts": [snapshot["nodes"][nid] for nid in claim_nodes(claim)], "evidence": rows})
            rid = "backward_" + digest([i["item_id"] for i in items])[:20]
            done = self.store.chunk_state(rid)
            if done and done.get("status") in {"complete", "processed_with_skips"}:
                skipped_total += len(done["details"].get("skipped", []))
                continue
            skipped = []
            try:
                decisions = self._review(items, self._snapshot(), rid)
            except (ValueError, OutputLimit):
                decisions = []
                for item in items:
                    try:
                        decisions.extend(self._review([item], self._snapshot(), rid + "_" + item["item_id"]))
                    except (ValueError, OutputLimit) as exc:
                        skipped.append({"item_id": item["item_id"], "stage": "review",
                                        "error_type": type(exc).__name__, "error": str(exc)[:500]})
            for decision in decisions:
                if decision["verdict"] in {"supported", "contradicted", "different_scope"} and decision["evidence_ids"]:
                    chunk = {"id": rid + "_" + decision["item_id"], "book_id": "targeted_sources", "evidence_ids": decision["evidence_ids"],
                             "target_claim_ids": [decision["item_id"]], "review_results": [decision]}
                    current = self._snapshot()
                    self.index.refresh(current)
                    try:
                        self._commit(self.analyze(chunk, current))
                    except (ValueError, OutputLimit) as exc:
                        if isinstance(exc, StaleGraphError):
                            raise
                        skipped.append({"item_id": decision["item_id"], "stage": "update",
                                        "error_type": type(exc).__name__, "error": str(exc)[:500]})
            self.store.record_chunk(rid, self.fingerprint,
                                    "processed_with_skips" if skipped else "complete",
                                    {"decisions": decisions, "skipped": skipped})
            skipped_total += len(skipped)
        return skipped_total

    def complete_prior(self, book_ids=None):
        snapshot = self._snapshot()
        unverified = [c for c in snapshot["claims"].values()
                      if c["status"] == "active" and c["origin"] == "parametric_prior" and not c["evidence_ids"]]
        chunks = [c for c in self.source.bundle["chunks"] if not book_ids or c["book_id"] in book_ids]
        owners = {eid: chunk for chunk in chunks for eid in chunk["evidence_ids"]}
        source = SourceCorpus(dict(self.source.bundle, evidence=[e for e in self.source.evidence.values() if e["id"] in owners]))
        groups, not_found = {}, []
        for claim in unverified:
            names = " ".join(snapshot["nodes"][nid]["name"] for nid in sorted(claim_nodes(claim)))
            rows = source.search(names, 1)
            if not rows:
                not_found.append(claim["id"])
                continue
            chunk = owners[rows[0]["id"]]
            groups.setdefault(chunk["id"], (chunk, []))[1].append(claim["id"])
        pending = []
        for original, targets in groups.values():
            for offset in range(0, len(targets), 16):
                ids = sorted(targets[offset:offset + 16])
                chunk = dict(original, id="prior_completion_" + digest([original["id"], ids])[:28], target_claim_ids=ids)
                if (self.store.chunk_state(chunk["id"]) or {}).get("status") != "complete":
                    pending.append(chunk)
        completed, failed = [], []
        self._progress("prior_completion", target_claims=len(unverified), requests=len(pending))
        with ThreadPoolExecutor(max_workers=int(self.config.get("workers", 16))) as pool:
            remaining, active = iter(pending), {}

            def submit_next():
                chunk = next(remaining, None)
                if chunk is None:
                    return False
                current = self._snapshot()
                self.index.refresh(current)
                active[pool.submit(self.analyze, chunk, current)] = (chunk, current["version"])
                return True

            while len(active) < int(self.config.get("workers", 16)) and submit_next():
                pass
            while active:
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    chunk, version = active.pop(future)
                    try:
                        plan = future.result()
                        save(self.root / "analysis" / (chunk["id"] + f"_v{version}.json"), plan)
                        self._commit(plan)
                    except (ValueError, OutputLimit) as exc:
                        item = dict(chunk_id=chunk["id"], target_claim_ids=chunk["target_claim_ids"],
                                    error_type=type(exc).__name__, error=str(exc)[:500])
                        failed.append(item)
                        self.store.record_chunk(chunk["id"], self.fingerprint, "failed", item)
                    else:
                        completed.append(chunk["id"])
                    while len(active) < int(self.config.get("workers", 16)) and submit_next():
                        pass
                    self.project()
                    self._progress("prior_completion", target_claims=len(unverified), requests=len(pending),
                                   completed_requests=len(completed), failed_requests=len(failed))
        current = self._snapshot()
        untouched = [c["id"] for c in unverified if current["claims"][c["id"]]["status"] == "active"
                     and not current["claims"][c["id"]]["evidence_ids"]]
        save(self.root / "prior_completion_report.json", dict(target_claims=len(unverified),
             requests=len(pending), completed_requests=len(completed), failed=failed,
             no_text_match=not_found, remaining_unverified=untouched, book_ids=sorted(book_ids) if book_ids else None,
             graph_version=self.store.version))
        return len(failed)

    def run(self, max_chunks=None):
        fresh_prior = not self._snapshot()["nodes"]
        self._progress("generating_prior")
        self.seed()
        snapshot = self._snapshot()
        # 接续图谱由后台分批维护，抽取无需等待整轮归并。
        if fresh_prior and snapshot["nodes"]:
            self._progress("semantic_deduplication")
            mapping, audit = self.semantic.deduplicate(snapshot)
            result = self.store.merge_nodes(mapping, self.schema, snapshot["version"], self.semantic.name_qualifiers(audit))
            save(self.root / "semantic_merge_initial.json", dict(result=result, matches=audit))
        chunks = self.source.bundle["chunks"]
        selected = chunks if max_chunks is None else chunks[:max_chunks]
        completed_total = sum(
            1 for chunk in selected
            if (self.store.chunk_state(chunk["id"]) or {}).get("status") == "complete"
        )
        skipped_total = sum(
            1 for chunk in selected
            if (self.store.chunk_state(chunk["id"]) or {}).get("status") == "skipped"
        )
        workers = int(self.config.get("workers", 8))
        books = list(self.source.bundle.get("books", []))
        original_order = {book["id"]: index for index, book in enumerate(books)}
        priority_order = {book_id: index for index, book_id in enumerate(PRIORITY_BOOK_IDS)}
        ordered_books = sorted(
            books,
            key=lambda book: (
                priority_order.get(book["id"], len(PRIORITY_BOOK_IDS)),
                original_order[book["id"]],
            ),
        )
        self._start_maintenance(self._snapshot())
        self._progress("syncing_graph")
        self.project(force=True)
        with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=1) as maintenance:
            # 教材按书顺序处理；同一本书内部仍保持并发，避免后一本书
            # 在前一本书尚未补全时提前改变全局概念边界。
            selected_by_book = {book["id"]: [] for book in books}
            for chunk in selected:
                selected_by_book.setdefault(chunk["book_id"], []).append(chunk)
            for book in ordered_books:
                book_id = book["id"]
                book_chunks = selected_by_book.get(book_id, [])
                units = self._pending_units(book_chunks)
                if not units:
                    continue
                pending = []
                for chunk, source_chunks in units:
                    batch_done = self.store.chunk_state(chunk["id"]) if len(source_chunks) > 1 else None
                    if batch_done and batch_done["status"] == "complete":
                        require(batch_done["fingerprint"] in self.compatible_fingerprints,
                                "Completed source batch belongs to different inputs")
                        self._record_source_batch(chunk, source_chunks)
                        completed_total += len(source_chunks)
                    else:
                        pending.append((chunk, source_chunks))
                if not pending:
                    self.project(force=True)
                    continue
                book_completed = sum(
                    (self.store.chunk_state(chunk["id"]) or {}).get("status") == "complete"
                    for chunk in book_chunks
                )
                book_skipped = sum(
                    (self.store.chunk_state(chunk["id"]) or {}).get("status") == "skipped"
                    for chunk in book_chunks
                )
                self._progress("book_start", book_id=book_id, book_title=book.get("title"),
                               book_completed=book_completed, book_total=len(book_chunks),
                               book_skipped=book_skipped, completed=completed_total,
                               skipped=skipped_total, total_chunks=len(chunks))
                remaining = iter(pending)
                active = {}

                def submit_next():
                    try:
                        chunk, source_chunks = next(remaining)
                    except StopIteration:
                        return False
                    snapshot = self._snapshot()
                    self.index.refresh(snapshot)
                    active[pool.submit(self.analyze, chunk, snapshot)] = (chunk, source_chunks, snapshot["version"])
                    return True

                while len(active) < workers and submit_next():
                    pass
                self._progress("constructing", book_id=book_id,
                               completed=completed_total, book_completed=book_completed,
                               book_skipped=book_skipped, skipped=skipped_total,
                               book_total=len(book_chunks), selected_chunks=len(selected), total_chunks=len(chunks))
                while active:
                    self._maintain(maintenance)
                    waiting = set(active)
                    if self._maintenance_future:
                        waiting.add(self._maintenance_future)
                    finished, _ = wait(waiting, return_when=FIRST_COMPLETED)
                    if self._maintenance_future in finished:
                        finished.remove(self._maintenance_future)
                        self._maintain(maintenance)
                    for future in finished:
                        chunk, source_chunks, version = active.pop(future)
                        completed_now = 0
                        skipped_now = 0
                        try:
                            plan = future.result()
                            save(self.root / "analysis" / (chunk["id"] + f"_v{version}.json"), plan)
                            self._commit(plan)
                        except (ValueError, OutputLimit) as exc:
                            self.store.record_chunks([(source["id"], (self.store.chunk_state(source["id"]) or {}).get("fingerprint", self.fingerprint), "failed", {
                                "evidence_ids": source["evidence_ids"], "error_type": type(exc).__name__,
                                "error": str(exc)[:500], "version": self.store.version,
                            }) for source in source_chunks])
                            skipped_now = len(source_chunks)
                        else:
                            self._record_source_batch(chunk, source_chunks)
                            completed_now = len(source_chunks)
                        completed_total += completed_now
                        book_completed += completed_now
                        skipped_total += skipped_now
                        book_skipped += skipped_now
                        # 每次提交后立即用最新图谱补满任务，避免其他提交期间 worker 空闲。
                        while len(active) < workers and submit_next():
                            pass
                        self._maintain(maintenance)
                        self.project()
                        self._progress("constructing", book_id=book_id,
                                       completed=completed_total, book_completed=book_completed,
                                       skipped=skipped_total, book_skipped=book_skipped,
                                       book_total=len(book_chunks), selected_chunks=len(selected),
                                       total_chunks=len(chunks))
                self.project(force=True)
                self._progress("book_processed_with_skips" if book_skipped else "book_complete",
                               book_id=book_id, book_title=book.get("title"),
                               book_completed=book_completed, book_skipped=book_skipped,
                               book_total=len(book_chunks), completed=completed_total,
                               skipped=skipped_total, total_chunks=len(chunks))
        prior_skipped = self.complete_prior() if max_chunks is None else 0
        if max_chunks is None:
            self._progress("semantic_deduplication_final")
            with ThreadPoolExecutor(max_workers=1) as maintenance:
                while self._maintenance_dirty or self._maintenance_future:
                    self._maintain(maintenance)
                    if self._maintenance_future:
                        wait([self._maintenance_future], timeout=30)
                    self._progress("semantic_deduplication_final", online_maintenance=self._maintenance_state)
            save(self.root / "semantic_merge_final.json", dict(
                result=dict(self._maintenance_state, version=self.store.version), mode="incremental_batches"))
        self.store.export_snapshot(self.root / "graph.json")
        self.project(force=True)
        graph = self._snapshot()
        phase = ("complete_with_skips" if skipped_total or prior_skipped else "complete") if max_chunks is None else "partial"
        self._progress(phase, completed=completed_total, skipped=skipped_total,
                       skipped_prior_checks=prior_skipped,
                       total_chunks=len(chunks), concepts=len(graph["nodes"]),
                       active_claims=sum(c["status"] == "active" for c in graph["claims"].values()),
                       database_written=bool(self.projection), human_accepted=False)
        return graph
