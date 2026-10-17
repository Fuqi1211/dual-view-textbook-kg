from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import numpy as np


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class OutputLimit(RuntimeError):
    pass


def extraction_response_format(payload):
    def object_schema(properties):
        return dict(type="object", properties=properties, required=list(properties), additionalProperties=False)

    text = {"type": "string"}
    strings = dict(type="array", items=text)
    evidence = dict(type="array", items=dict(type="string", enum=[row["id"] for row in payload["source_evidence"]]))
    predicates = [name for name in payload["schema"]["relation_types"] if name != "instance_of"]
    relation = object_schema(dict(head=text, relation=dict(type="string", enum=predicates), tail=text,
                                  conditions=text, evidence_ids=dict(evidence, minItems=1)))
    concept = object_schema(dict(query_id=text, name=text,
        entity_type=dict(type="string", enum=list(payload["schema"]["entity_types"])), aliases=strings,
        definition={"anyOf": [text, {"type": "null"}]},
        definition_status=dict(type="string", enum=["explicit", "summarized", "partial", "not_provided"]),
        scope=text, evidence_ids=dict(evidence, minItems=1), definition_evidence_ids=evidence))
    relation_claim = object_schema(dict(kind=dict(type="string", enum=["relation"]),
        head=text, relation=dict(type="string", enum=predicates), tail=text, conditions=text))
    definition_claim = object_schema(dict(kind=dict(type="string", enum=["node_fact"]), node_id=text,
        field=dict(type="string", enum=["definition", "definition_detail"]), value=text, conditions=text,
        definition_status=dict(type="string", enum=["explicit", "summarized", "partial"])))
    other_claim = object_schema(dict(kind=dict(type="string", enum=["node_fact"]), node_id=text,
        field=dict(type="string", enum=["alias", "scope", "entity_type"]), value=text, conditions=text))
    claim = {"anyOf": [relation_claim, definition_claim, other_claim]}
    claim_ids = [row["id"] for row in payload["local_graph"]["claims"] if row["status"] == "active"]
    revision = object_schema(dict(claim_id=dict(type="string", enum=claim_ids) if claim_ids else text,
        operation=dict(type="string", enum=["revise_claim", "retire_claim"]), before=claim,
        after={"anyOf": [*claim["anyOf"], {"type": "null"}]},
        evidence_ids=dict(evidence, minItems=1), reason=text))
    revisions = dict(type="array", items=revision)
    if not claim_ids:
        revisions["maxItems"] = 0
    schema = object_schema(dict(request_id=dict(type="string", enum=[payload["request_id"]]),
        graph_version=dict(type="integer", enum=[payload["graph_version"]]),
        concepts=dict(type="array", items=concept, maxItems=30),
        relations=dict(type="array", items=relation, maxItems=60), revisions=revisions))
    return dict(type="json_schema", json_schema=dict(name="textbook_concept_graph", strict=True, schema=schema))


def semantic_response_format(payload):
    decisions = {}
    for pair in payload['pairs']:
        ids = sorted({row['id'] for side in ('left', 'right') for row in pair[side]['evidence']})
        evidence = dict(type='array', items=dict(type='string', enum=ids) if ids else dict(type='string'))
        if not ids:
            evidence['maxItems'] = 0
        properties = dict(decision=dict(type='string', enum=['merge', 'separate', 'unresolved']),
                          reason=dict(type='string', minLength=1), evidence_ids=evidence,
                          left_qualifier=dict(type='string', maxLength=60),
                          right_qualifier=dict(type='string', maxLength=60))
        decisions[pair['pair_id']] = dict(type='object', properties=properties,
                                        required=list(properties), additionalProperties=False)
    decisions_schema = dict(type='object', properties=decisions, required=list(decisions), additionalProperties=False)
    schema = dict(type='object', properties=dict(decisions=decisions_schema), required=['decisions'], additionalProperties=False)
    return dict(type='json_schema', json_schema=dict(name='semantic_pairs', strict=True, schema=schema))


class ModelRuntime:
    FILES = {
        "semantic_identity": ("07_同义概念判定_system.txt", "07_同义概念判定_user.txt"),
        "extract_graph": ("06_片段图谱抽取_system.txt", "06_片段图谱抽取_user.txt"),
        "prior": ("01_G0生成_system.txt", "01_G0生成_user.txt"),
        "concepts": ("02_片段概念识别_system.txt", "02_片段概念识别_user.txt"),
        "identity": ("00_教材共同_system.txt", "03_批量概念身份判断_user.txt"),
        "updates": ("00_教材共同_system.txt", "04_关系与图谱增量更新_user.txt"),
        "review": ("00_教材共同_system.txt", "05_定向证据与冲突复核_user.txt"),
    }

    def __init__(self, root, prompts, config):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.templates = {name: (Path(prompts) / name).read_text("utf-8-sig")
                          for pair in self.FILES.values() for name in pair}
        self.fingerprint = digest({"templates": {k: v for k, v in self.templates.items()
                                               if k not in self.FILES["semantic_identity"]}, "llm": config["llm"],
                                   "embedding": config["embedding"]})
        self.semantic_fingerprint = digest({"templates": {k: self.templates[k] for k in self.FILES["semantic_identity"]},
                                            "llm": config["llm"]})
        self.db = sqlite3.connect(self.root / "model_cache.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS semantic_decisions(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS responses(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, key TEXT, stage TEXT,
            created REAL, status TEXT, value TEXT);
          CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY, value BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS attempts_key_id ON attempts(key,id DESC);
        """)
        self.lock = threading.RLock()
        self.gate = threading.BoundedSemaphore(int(config.get("workers", 8)))
        self.embedding_gate = threading.BoundedSemaphore(2)
        self.embedding_key_locks = [threading.Lock() for _ in range(1024)]

    def semantic_decision(self, pair_id):
        key = digest([self.semantic_fingerprint, pair_id])
        with self.lock:
            row = self.db.execute("SELECT value FROM semantic_decisions WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_semantic_decision(self, pair_id, decision):
        self.save_semantic_decisions({pair_id: decision})

    def save_semantic_decisions(self, decisions):
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO semantic_decisions VALUES (?,?)",
                ((digest([self.semantic_fingerprint, pair_id]), dumps(decision))
                 for pair_id, decision in decisions.items()))

    def _get(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM responses WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def _record(self, key, stage, status, value):
        # 只保存模型结果和错误类别，绝不保存请求头或进程环境。
        with self.lock, self.db:
            self.db.execute("INSERT INTO attempts(key,stage,created,status,value) VALUES(?,?,?,?,?)",
                            (key, stage, time.time(), status, dumps(value)))

    def _raw_cached(self, key):
        with self.lock:
            row = self.db.execute(
                "SELECT status,value FROM attempts WHERE key=? "
                "AND status IN ('response','invalid_response','output_limit') ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row and row[0] == "invalid_response" and json.loads(row[1]).get("source") == "content":
                # 重放旧内容可重建同样的修复请求，继续复用后续成功响应。
                row = self.db.execute(
                    "SELECT status,value FROM attempts WHERE key=? AND status='response' ORDER BY id DESC LIMIT 1", (key,)
                ).fetchone()
        if not row:
            return None
        if row[0] == "output_limit":
            raise OutputLimit("Model output needs smaller source or query batches")
        if row[0] == "invalid_response":
            raise ValueError("Cached response envelope is malformed")
        return json.loads(row[1])

    def _post(self, endpoint, body, options, key, stage):
        credential = os.environ.get(options.get("api_key_env", "SILICONFLOW_API_KEY"))
        if not credential:
            raise RuntimeError("Required model credential environment variable is absent")
        url = options["base_url"].rstrip("/") + endpoint
        for attempt in range(int(self.config.get("service_retries", 6)) + 1):
            request = urllib.request.Request(url, data=dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
            self._record(key, stage, "request_started", {"attempt": attempt + 1})
            try:
                with urllib.request.urlopen(request, timeout=int(options.get("timeout_seconds", 180))) as response:
                    raw = response.read().decode("utf-8")
                if credential in raw:
                    raise RuntimeError("Provider response unexpectedly contains a credential; not persisted")
                try:
                    result = json.loads(raw)
                except ValueError:
                    self._record(key, stage, "invalid_response", {"error": "non_json_envelope", "raw": raw})
                    raise
                self._record(key, stage, "response", result)
                return result
            except urllib.error.HTTPError as exc:
                code = exc.code
                self._record(key, stage, "http_error", {"status_code": code})
                if code not in {408, 409, 429, 500, 502, 503, 504} or attempt == int(self.config.get("service_retries", 6)):
                    raise RuntimeError(f"Model service returned HTTP {code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                self._record(key, stage, "transport_error", {"type": type(exc).__name__})
                if attempt == int(self.config.get("service_retries", 6)):
                    raise RuntimeError("Model service transport retries exhausted") from None
            time.sleep(min(2 ** attempt + random.random(), 45))

    def call(self, stage, payload, validator):
        system_name, user_name = self.FILES[stage]
        placeholder = "{G0_INPUT_JSON}" if stage == "prior" else "{TASK_INPUT_JSON}"
        user = self.templates[user_name]
        task_input = payload
        if stage in {"concepts", "extract_graph"}:
            user = user.replace("{SCHEMA_JSON}", dumps(payload["schema"]), 1)
            # 模式在独立区块展示，不在任务数据中重复发送。
            task_input = {key: value for key, value in payload.items() if key != "schema"}
        user = user.replace(placeholder, dumps(task_input), 1)
        messages = [{"role": "system", "content": self.templates[system_name]},
                    {"role": "user", "content": user}]
        key = digest({"runtime": self.semantic_fingerprint if stage == "semantic_identity" else self.fingerprint,
                      "stage": stage, "payload": payload})
        opts = self.config["llm"]
        with self.gate:
            cached = self._get(key)
            if cached is not None:
                try:
                    validator(cached)
                    return cached
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    self._record(key, stage, "invalid_response", {"error": type(exc).__name__, "source": "clean_cache"})
                    with self.lock, self.db:
                        self.db.execute("DELETE FROM responses WHERE key=?", (key,))
            for repair in range(int(self.config.get("response_repairs", 2)) + 1):
                for fresh in range(2):
                    attempt_key = key + ":" + str(repair) + (":clean" if fresh else "")
                    body = dict(opts.get("extra_body", {}))
                    body.update(model=opts["model"], messages=messages,
                        temperature=opts.get("temperature", 0), max_tokens=opts.get("max_tokens", 12000),
                        response_format=extraction_response_format(payload) if stage == "extract_graph"
                                        else semantic_response_format(payload) if stage == "semantic_identity"
                                        else {"type": "json_object"})
                    try:
                        raw = self._raw_cached(attempt_key)
                        if raw is None:
                            raw = self._post("/chat/completions", body, opts, attempt_key, stage)
                        choice = raw["choices"][0]
                        if choice.get("finish_reason") == "length":
                            self._record(attempt_key, stage, "output_limit", {"finish_reason": "length"})
                            raise OutputLimit("Model output needs smaller source or query batches")
                        content = choice["message"]["content"]
                        if not isinstance(content, str):
                            raise TypeError("Model message content must be text")
                    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                        self._record(attempt_key, stage, "invalid_response", {"error": type(exc).__name__, "source": "envelope"})
                        if fresh:
                            raise ValueError(f"{stage}: malformed response after one fresh retry") from None
                        # 坏信封不能作为 assistant 消息，重试仅发送原始输入。
                        messages = messages[:2]
                        continue
                    break
                try:
                    clean = json.loads(content)
                    if not isinstance(clean, dict):
                        raise ValueError("Output must be a JSON object")
                    validator(clean)
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    self._record(attempt_key, stage, "invalid_response", {"error": type(exc).__name__, "source": "content"})
                    if repair == int(self.config.get("response_repairs", 2)):
                        raise ValueError(f"{stage}: response still invalid after repair: {exc}") from None
                    messages = messages[:2] + [{"role": "assistant", "content": content},
                        {"role": "user", "content": "只依据原输入修正以下输出问题，返回完整 JSON，不添加新知识：" + str(exc)}]
                    continue
                with self.lock, self.db:
                    self.db.execute("INSERT OR REPLACE INTO responses VALUES(?,?)", (key, dumps(clean)))
                return clean

    def embed(self, texts):
        if not texts:
            return np.empty((0, 1024), dtype=np.float32)
        opts = self.config["embedding"]
        keys = [digest({"model": opts["model"], "base_url": opts["base_url"], "text": t}) for t in texts]
        vectors, missing, seen = {}, [], set()
        with self.lock:
            for key, text in zip(keys, texts):
                row = self.db.execute("SELECT value FROM embeddings WHERE key=?", (key,)).fetchone()
                if row:
                    vectors[key] = np.frombuffer(row[0], dtype=np.float32).copy()
                elif key not in seen:
                    missing.append((key, text))
                    seen.add(key)
        size = int(opts.get("batch_size", 16))
        if size < 1:
            raise ValueError("Embedding batch size must be positive")
        for offset in range(0, len(missing), size):
            batch = missing[offset:offset + size]
            # 按固定顺序锁住同键；不同批次仍可并发，重叠批次不会重复请求。
            stripes = sorted({int(key[:8], 16) % len(self.embedding_key_locks) for key, _ in batch})
            with ExitStack() as stack:
                for stripe in stripes:
                    stack.enter_context(self.embedding_key_locks[stripe])
                stack.enter_context(self.embedding_gate)
                remaining = []
                with self.lock:
                    for key, text in batch:
                        row = self.db.execute("SELECT value FROM embeddings WHERE key=?", (key,)).fetchone()
                        if row:
                            vectors[key] = np.frombuffer(row[0], dtype=np.float32).copy()
                        else:
                            remaining.append((key, text))
                batch = remaining
                if not batch:
                    continue
                body = {"model": opts["model"], "input": [t for _, t in batch], "encoding_format": "float"}
                request_key = digest(body)
                for fresh in range(2):
                    attempt_key = request_key + (":clean" if fresh else "")
                    try:
                        raw = self._raw_cached(attempt_key)
                        if raw is None:
                            raw = self._post("/embeddings", body, opts, attempt_key, "embedding")
                        rows = sorted(raw["data"], key=lambda r: r["index"])
                        if [r["index"] for r in rows] != list(range(len(batch))):
                            raise ValueError("Embedding response indices differ from the requested batch")
                        matrix = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
                        if matrix.shape != (len(batch), 1024) or not np.isfinite(matrix).all():
                            raise ValueError("Expected finite 1024-dimensional BGE-M3 vectors")
                        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                        if np.any(norms <= 0):
                            raise ValueError("Embedding contains a zero vector")
                        matrix /= norms
                    except (ValueError, KeyError, TypeError, IndexError) as exc:
                        self._record(attempt_key, "embedding", "invalid_response", {"error": type(exc).__name__})
                        if fresh:
                            raise ValueError("Embedding response still invalid after one fresh retry") from None
                        continue
                    break
                with self.lock, self.db:
                    for (key, _), vector in zip(batch, matrix):
                        self.db.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?)", (key, vector.tobytes()))
                        vectors[key] = vector
        return np.asarray([vectors[key] for key in keys], dtype=np.float32)

    def stats(self):
        with self.lock:
            return {f"{stage}:{status}": n for stage, status, n in
                    self.db.execute("SELECT stage,status,COUNT(*) FROM attempts GROUP BY stage,status")}

    def close(self):
        self.db.close()


def semantic_fields(node):
    return {k: node.get(k) for k in ("name", "aliases", "entity_type", "definition", "scope", "node_kind")}


def normalized(name):
    # 保留大小写以免把有区分意义的符号合并；此处只做召回，不做合并。
    return " ".join(str(name).split())


class ConceptIndex:
    def __init__(self, runtime, top_k=8):
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self.runtime, self.top_k = runtime, top_k
        self.nodes, self.ids, self.fingerprints, self.vectors = {}, [], {}, {}
        self.matrix = np.empty((0, 1024), dtype=np.float32)
        self.names = {}
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.version = None
        self._ann, self._ann_ids, self._ann_labels, self._ann_capacity = None, [], {}, 0
        try:
            import hnswlib
        except (ImportError, OSError):
            self._hnswlib, self.backend = None, "numpy_exact"
        else:
            self._hnswlib, self.backend = hnswlib, "hnswlib"

    def refresh(self, snapshot):
        with self.refresh_lock:
            version = snapshot["version"]
            if self.version == version:
                return
            nodes = snapshot["nodes"]
            removed = set(self.nodes) - set(nodes)
            fingerprints = {nid: digest(semantic_fields(node)) for nid, node in nodes.items()}
            changed = [nid for nid, node in nodes.items()
                       if self.fingerprints.get(nid) != fingerprints[nid]]
            # 网络编码不持有读锁；请求仍可读取上一份完整索引。
            embedded = (self.runtime.embed([dumps(semantic_fields(nodes[nid]))[:7000] for nid in changed])
                        if changed else [])
            with self.lock:
                self.nodes = nodes
                if changed or removed:
                    for nid, vector in zip(changed, embedded):
                        self.vectors[nid] = vector
                        self.fingerprints[nid] = fingerprints[nid]
                    for nid in removed:
                        self.vectors.pop(nid, None)
                        self.fingerprints.pop(nid, None)
                    self.ids = sorted(nodes)
                    if self.backend == "hnswlib":
                        try:
                            self._refresh_ann(changed, bool(removed))
                        except (RuntimeError, ValueError, AttributeError):
                            # 可选本地索引不可用时继续精确召回，backend 明确反映实际实现。
                            self._ann, self.backend = None, "numpy_exact"
                    self.matrix = (np.asarray([self.vectors[nid] for nid in self.ids], dtype=np.float32)
                                   if self.backend == "numpy_exact" and self.ids else np.empty((0, 1024), dtype=np.float32))
                    self.names = {}
                    for nid, node in nodes.items():
                        for name in [node["name"]] + node.get("aliases", []):
                            self.names.setdefault(normalized(name), set()).add(nid)
                self.version = version

    def _refresh_ann(self, changed, removed):
        if not self.ids:
            self._ann, self._ann_ids, self._ann_labels = None, [], {}
            return
        if self._ann is None or removed:
            self._ann = self._hnswlib.Index(space="cosine", dim=1024)
            self._ann_capacity = max(1024, len(self.ids) * 2)
            self._ann.init_index(max_elements=self._ann_capacity, ef_construction=200, M=16, random_seed=17)
            self._ann.set_num_threads(1)
            self._ann_ids, self._ann_labels = [], {}
            changed = self.ids
        for nid in changed:
            if nid not in self._ann_labels:
                self._ann_labels[nid] = len(self._ann_ids)
                self._ann_ids.append(nid)
        if len(self._ann_ids) > self._ann_capacity:
            self._ann_capacity = max(len(self._ann_ids), self._ann_capacity * 2)
            self._ann.resize_index(self._ann_capacity)
        if changed:
            self._ann.add_items(np.asarray([self.vectors[nid] for nid in changed], dtype=np.float32),
                                np.asarray([self._ann_labels[nid] for nid in changed], dtype=np.int64))
        self._ann.set_ef(max(100, self.top_k * 4))

    def retrieve(self, concepts, top_k=None):
        if not concepts:
            return []
        with self.lock:
            if not self.ids:
                return [[] for _ in concepts]
        queries = self.runtime.embed([dumps(semantic_fields(c))[:7000] for c in concepts])
        requested = self.top_k if top_k is None else top_k
        if requested < 1:
            raise ValueError("top_k must be positive")
        with self.lock:
            if not self.ids:
                return [[] for _ in concepts]
            k = min(requested, len(self.ids))
            neighbor_ids = []
            if self.backend == "hnswlib":
                self._ann.set_ef(max(100, k * 4))
                labels, _ = self._ann.knn_query(queries, k=k, num_threads=1)
                neighbor_ids = [[self._ann_ids[int(label)] for label in row] for row in labels]
            else:
                for vector in queries:
                    scores = self.matrix @ vector
                    positions = np.argpartition(scores, -k)[-k:]
                    ordered = sorted(positions, key=lambda i: (-float(scores[i]), self.ids[i]))
                    neighbor_ids.append([self.ids[i] for i in ordered])
            result = []
            for concept, neighbors in zip(concepts, neighbor_ids):
                exact = set()
                for name in [concept["name"]] + concept.get("aliases", []):
                    exact.update(self.names.get(normalized(name), set()))
                ids = list(dict.fromkeys(sorted(exact) + neighbors))
                result.append([self.nodes[nid] for nid in ids])
            return result

    def for_text(self, text, top_k=12):
        return self.retrieve([{"name": "当前教材片段", "definition": text[:7000], "scope": ""}], top_k)[0]
