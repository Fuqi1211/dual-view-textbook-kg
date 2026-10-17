"""将已提交的本地快照幂等投影到 Neo4j 工作图，保留断言与证据历史。"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Iterator
from urllib.parse import urlsplit


RELATION_TYPES = frozenset({
    "is_a", "part_of", "prerequisite_of", "causes", "affects", "used_for",
    "consists_of", "compared_with", "instance_of", "evaluated_by",
    "implemented_by", "derived_from",
})
_LABELS = ("CKGBuild", "CKGConcept", "CKGAssertion", "CKGEvidence", "CKGBook", "CKGRevision")
_PROJECTION = "ckg_incremental_v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).iterencode(value):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def _text(value: Any) -> str:
    return value if isinstance(value, str) else _json(value)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _records(value: Any, field: str) -> dict[str, dict[str, Any]]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, list):
        items = ((item.get("id") if isinstance(item, Mapping) else None, item) for item in value)
    else:
        raise ValueError(f"{field} must be a mapping or list")
    result: dict[str, dict[str, Any]] = {}
    for key, item in items:
        key = _identifier(key, f"{field}.id")
        if not isinstance(item, Mapping) or item.get("id", key) != key or key in result:
            raise ValueError(f"Invalid or duplicate record in {field}")
        result[key] = {**item, "id": key}
    return result


def _properties(record: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for field in fields:
        if field in record:
            value = record[field]
            if value is None or isinstance(value, (str, bool, int, float)):
                props[field] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                props[field] = value
            else:
                props[field] = _json(value)
    props["source_json"] = _json(record)
    props["payload_digest"] = _digest(record)
    return props


class Neo4jProjection:
    """先分批预写不可变来源，再在一个事务中发布该 run_id 的工作图版本。"""

    def __init__(self, driver: Any, run_id: str, database: str = "neo4j", batch_size: int = 200):
        self.driver = driver
        self.run_id = _identifier(run_id, "run_id")
        self.database = _identifier(database, "database")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.batch_size = batch_size
        self._schema_ready = False

    @classmethod
    def from_env(cls, run_id: str, database: str = "neo4j", batch_size: int = 200) -> "Neo4jProjection":
        values = {key: os.environ.get(key, "") for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        uri = urlsplit(values["NEO4J_URI"])
        if uri.username is not None or uri.password is not None:
            raise ValueError("NEO4J_URI must not contain credentials; use the dedicated environment variables")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError("Neo4j projection requires the neo4j Python driver (version 5 or 6)") from exc
        driver = GraphDatabase.driver(values["NEO4J_URI"], auth=(values["NEO4J_USERNAME"], values["NEO4J_PASSWORD"]))
        try:
            return cls(driver, run_id, database, batch_size)
        except Exception:
            driver.close()
            raise

    def close(self) -> None:
        self.driver.close()

    def _batches(self, rows: list[Any]) -> Iterator[list[Any]]:
        for start in range(0, len(rows), self.batch_size):
            yield rows[start:start + self.batch_size]

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self.driver.session(database=self.database) as session:
            for label in _LABELS:
                # 标签和约束名只来自固定白名单，数据一律使用参数。
                query = (
                    f"CREATE CONSTRAINT {label.lower()}_run_id_id IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE (n.run_id, n.id) IS UNIQUE"
                )
                session.execute_write(lambda tx, statement: tx.run(statement).consume(), query)
        self._schema_ready = True

    def sync(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        prepared = self._prepare(snapshot)
        self._ensure_schema()
        self._stage_sources(prepared)
        with self.driver.session(database=self.database) as session:
            return session.execute_write(self._sync_tx, prepared)

    def sync_store(self, store: Any) -> dict[str, Any]:
        self._ensure_schema()
        with self.driver.session(database=self.database) as session:
            def published_version(tx):
                row = tx.run("MATCH (b:CKGBuild {run_id:$run_id,id:$run_id}) "
                             "RETURN b.projected_version AS version", run_id=self.run_id).single()
                return int(row["version"]) if row else -1
            version = session.execute_read(published_version)
        return self.sync(store.snapshot(share_records=True, events_after_version=version))

    def _stage_sources(self, data: dict[str, Any]) -> None:
        # 约十万条原文不能积压在图谱发布事务中；预写结果尚不代表知识已入图。
        with self.driver.session(database=self.database) as session:
            for label, key in (("CKGBook", "books"), ("CKGEvidence", "evidence"), ("CKGRevision", "revisions")):
                missing = session.execute_read(self._new_immutable_rows, label, data[key])
                for batch in self._batches(missing):
                    session.execute_write(self._stage_source_batch, label, batch, data["version"])

    def _stage_source_batch(self, tx: Any, label: str, rows: list[dict[str, Any]], version: int) -> None:
        if label not in {"CKGBook", "CKGEvidence", "CKGRevision"}:
            raise ValueError("Invalid source label")
        result = tx.run(
            f"UNWIND $rows AS row MERGE (n:{label} {{run_id:$run_id,id:row.id}}) "
            "ON CREATE SET n += row.props,n.staged_version=$version,n.storage_kind='immutable_source' "
            "RETURN count(CASE WHEN n.payload_digest <> row.props.payload_digest THEN 1 END) AS conflicts",
            run_id=self.run_id, rows=rows, version=version,
        ).single()
        if result and result["conflicts"]:
            raise ValueError(f"Immutable {label} content differs for an existing ID")
        if label == "CKGEvidence":
            tx.run(
                "UNWIND $rows AS row MATCH (e:CKGEvidence {run_id:$run_id,id:row.id}) "
                "MATCH (b:CKGBook {run_id:$run_id,id:row.book_id}) MERGE (e)-[:FROM_BOOK]->(b)",
                run_id=self.run_id, rows=[row for row in rows if row["book_id"]],
            ).consume()

    def _prepare(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        version = snapshot.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("snapshot.version must be a nonnegative integer")
        if snapshot.get("run_id", self.run_id) != self.run_id:
            raise ValueError("snapshot run_id does not match projection namespace")
        nodes = _records(snapshot.get("nodes", {}), "nodes")
        claims = _records(snapshot.get("claims", {}), "claims")
        evidence = _records(snapshot.get("evidence", {}), "evidence")
        books = _records(snapshot.get("books", {}), "books")
        events = _records(snapshot.get("events", []), "events")
        node_rows = []
        for node_id, node in nodes.items():
            _identifier(node.get("name"), "node.name")
            props = _properties(node, (
                "name", "aliases", "entity_type", "node_kind", "definition", "definition_status", "scope",
                "origin", "verification_status", "human_decision", "status", "created_version", "updated_version",
            ))
            props.setdefault("human_decision", "pending")
            props.setdefault("verification_status", "unverified")
            node_rows.append({"id": node_id, "props": props})
        evidence_rows = []
        for evidence_id, item in evidence.items():
            source = item.get("source", {})
            source = source if isinstance(source, Mapping) else {}
            book_id = item.get("book_id") or source.get("book_id")
            if book_id:
                book_id = _identifier(book_id, "evidence.book_id")
                books.setdefault(book_id, {"id": book_id})
            props = _properties(item, (
                "book_id", "text", "quote", "content", "chapter_id", "chapter_path", "title_path", "chunk_id",
                "start", "end", "char_start", "char_end", "source_path", "page", "page_start", "page_end",
            ))
            if book_id:
                props["book_id"] = book_id
            evidence_rows.append({"id": evidence_id, "book_id": book_id, "props": props})
        claim_rows = [self._claim_row(claim, nodes, evidence) for claim in claims.values()]
        revision_rows: dict[str, dict[str, Any]] = {}
        for claim in claims.values():
            if claim.get("updated_version", claim.get("created_version", 0)) <= snapshot.get("revision_after_version", -1):
                continue
            revision = self._claim_revision(claim)
            revision_rows[revision["id"]] = revision
        for event_id, event in events.items():
            props = _properties(event, (
                "version", "operation", "target_id", "reason", "proposal_id", "group_id", "evidence_ids",
                "compared_evidence_ids",
            ))
            props["revision_kind"] = "graph_event"
            revision_rows["event:" + event_id] = {"id": "event:" + event_id, "claim_id": None, "props": props}
            for field in ("before", "after"):
                historical = event.get(field)
                if isinstance(historical, Mapping) and isinstance(historical.get("content"), Mapping):
                    historical = dict(historical)
                    historical.setdefault("id", event.get("target_id"))
                    if historical.get("id") in claims:
                        revision = self._claim_revision(historical)
                        revision_rows[revision["id"]] = revision
        return {
            "version": version,
            "digest": snapshot["projection_digest"] if "projection_digest" in snapshot else _digest(
                {"version": version, "nodes": nodes, "claims": claims, "evidence": evidence,
                 "books": books, "events": events}),
            "nodes": node_rows, "claims": claim_rows, "evidence": evidence_rows,
            "books": [{"id": key, "props": _properties(value, ("title", "name", "course", "source_path", "fingerprint"))}
                      for key, value in books.items()],
            "revisions": list(revision_rows.values()),
        }

    def _claim_row(self, claim: Mapping[str, Any], nodes: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
        claim_id = _identifier(claim.get("id"), "claim.id")
        status = claim.get("status", "active")
        if status not in {"active", "retired", "contested"}:
            raise ValueError("Invalid claim.status")
        content = claim.get("content")
        if not isinstance(content, Mapping):
            raise ValueError("claim.content must be a mapping")
        kind = content.get("kind")
        if kind == "relation":
            if content.get("relation") not in RELATION_TYPES:
                raise ValueError("Unknown semantic relation type")
            if content.get("head") not in nodes or content.get("tail") not in nodes:
                raise ValueError("Relation endpoint is missing from snapshot")
        elif kind == "node_fact":
            if content.get("node_id") not in nodes:
                raise ValueError("Node fact endpoint is missing from snapshot")
            _identifier(content.get("field"), "claim.content.field")
        else:
            raise ValueError("Unknown claim.content.kind")
        evidence_ids = claim.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or any(not isinstance(key, str) or key not in evidence for key in evidence_ids):
            raise ValueError("Claim references missing or invalid evidence")
        props = _properties(claim, (
            "revision", "origin", "human_decision", "verification_status", "created_version", "updated_version",
            "evidence_ids", "reason", "supersedes", "superseded_by",
        ))
        props.update({
            "kind": kind, "status": status, "content_json": _json(content),
            "conditions": _text(content.get("conditions", "")), "conditions_json": _json(content.get("conditions", "")),
            "head": content.get("head"), "tail": content.get("tail"), "relation": content.get("relation"),
            "node_id": content.get("node_id"), "field": content.get("field"),
            "value": _text(content.get("value", "")), "value_json": _json(content.get("value")),
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "human_decision": claim.get("human_decision", "pending"),
            "verification_status": claim.get("verification_status", "evidence_backed" if evidence_ids else "unverified"),
        })
        return {"id": claim_id, "props": props, "evidence_ids": props["evidence_ids"]}

    @staticmethod
    def _claim_revision(claim: Mapping[str, Any]) -> dict[str, Any]:
        claim_id = _identifier(claim.get("id"), "historical_claim.id")
        digest = _digest(claim)
        props = _properties(claim, (
            "revision", "status", "origin", "human_decision", "verification_status", "created_version",
            "updated_version", "evidence_ids",
        ))
        props.update({"revision_kind": "claim_state", "claim_id": claim_id, "content_json": _json(claim.get("content"))})
        return {"id": f"claim:{claim_id}:{claim.get('revision', 0)}:{digest}", "claim_id": claim_id, "props": props}

    def _upsert(self, tx: Any, label: str, rows: list[dict[str, Any]], version: int, immutable: bool = False) -> None:
        if label not in _LABELS:
            raise ValueError("Invalid projection label")
        setter = "ON CREATE SET n += row.props" if immutable else "SET n += row.props"
        query = (
            f"UNWIND $rows AS row MERGE (n:{label} {{run_id:$run_id, id:row.id}}) "
            f"{setter} SET n.in_current_snapshot=true, n.projected_version=$version "
            "RETURN count(CASE WHEN n.payload_digest <> row.props.payload_digest THEN 1 END) AS conflicts"
        )
        for batch in self._batches(rows):
            result = tx.run(query, run_id=self.run_id, rows=batch, version=version).single()
            if immutable and result and result["conflicts"]:
                raise ValueError(f"Immutable {label} content differs for an existing ID")

    def _new_immutable_rows(self, tx: Any, label: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if label not in {"CKGBook", "CKGEvidence", "CKGRevision"}:
            raise ValueError("Invalid immutable projection label")
        existing = {}
        for batch in self._batches(rows):
            existing.update({row["id"]: row["digest"] for row in tx.run(
                f"UNWIND $ids AS id MATCH (n:{label} {{run_id:$run_id,id:id}}) "
                "RETURN n.id AS id,n.payload_digest AS digest",
                run_id=self.run_id, ids=[row["id"] for row in batch],
            )})
        new_rows = []
        for row in rows:
            if row["id"] not in existing:
                new_rows.append(row)
            elif existing[row["id"]] != row["props"]["payload_digest"]:
                raise ValueError(f"Immutable {label} content differs for an existing ID")
        return new_rows

    def _sync_tx(self, tx: Any, data: dict[str, Any]) -> dict[str, Any]:
        version = data["version"]
        # 写同一属性会取得节点写锁，版本检查与投影因此串行执行。
        build = tx.run(
            "MERGE (b:CKGBuild {run_id:$run_id,id:$run_id}) "
            "ON CREATE SET b.projected_version=-1,b.lock_counter=0 "
            "SET b.lock_counter=coalesce(b.lock_counter,0)+1 "
            "RETURN b.projected_version AS version,b.snapshot_digest AS digest,b.status AS status",
            run_id=self.run_id,
        ).single()
        current = int(build["version"])
        if current > version:
            raise ValueError("Refusing to project an older snapshot version")
        if current == version:
            if build["digest"] != data["digest"]:
                raise ValueError("Different snapshot content uses the same version")
            if build["status"] == "ready":
                return self._stats(data, changed=0, skipped=True)
        tx.run(
            "MATCH (b:CKGBuild {run_id:$run_id,id:$run_id}) "
            "SET b.status='projecting',b.graph_kind='working'", run_id=self.run_id,
        ).consume()
        existing = {row["id"]: row["digest"] for row in tx.run(
            "MATCH (a:CKGAssertion {run_id:$run_id}) WHERE a.in_current_snapshot=true "
            "RETURN a.id AS id,a.payload_digest AS digest", run_id=self.run_id,
        )}
        changed_rows = [row for row in data["claims"] if existing.get(row["id"]) != row["props"]["payload_digest"]]
        present = {row["id"] for row in data["claims"]}
        changed_ids = [row["id"] for row in changed_rows] + sorted(set(existing) - present)
        existing_nodes = {row["id"]: row["digest"] for row in tx.run(
            "MATCH (n:CKGConcept {run_id:$run_id}) WHERE n.in_current_snapshot=true "
            "RETURN n.id AS id,n.payload_digest AS digest", run_id=self.run_id,
        )}
        changed_nodes = [row for row in data["nodes"]
                         if existing_nodes.get(row["id"]) != row["props"]["payload_digest"]]
        # 大段教材原文和历史只传输新增项，避免每轮重发所有证据。
        new_revisions = []
        for batch in self._batches(data["revisions"]):
            unpublished = {row["id"] for row in tx.run(
                "UNWIND $ids AS id MATCH (n:CKGRevision {run_id:$run_id,id:id}) "
                "WHERE coalesce(n.in_current_snapshot,false)=false RETURN n.id AS id",
                run_id=self.run_id, ids=[row["id"] for row in batch],
            )}
            new_revisions.extend(row for row in batch if row["id"] in unpublished)
        # 未变化的正文属性不重写，避免全图内容积压在发布事务的堆内存中。
        for label, previous, rows in (("CKGConcept", existing_nodes, data["nodes"]),
                                      ("CKGAssertion", existing, data["claims"])):
            removed = sorted(set(previous) - {row["id"] for row in rows})
            for batch in self._batches(removed):
                tx.run(f"UNWIND $ids AS id MATCH (n:{label} {{run_id:$run_id,id:id}}) "
                       "SET n.in_current_snapshot=false", run_id=self.run_id, ids=batch).consume()
        tx.run("MATCH (n:CKGBook {run_id:$run_id}) SET n.in_current_snapshot=false",
               run_id=self.run_id).consume()
        self._upsert(tx, "CKGBook", data["books"], version)
        self._upsert(tx, "CKGConcept", changed_nodes, version)
        self._upsert(tx, "CKGAssertion", changed_rows, version)
        # 历史正文已分批预写；发布事务只切换可见标志并关联来源。
        for batch in self._batches(new_revisions):
            tx.run("UNWIND $ids AS id MATCH (n:CKGRevision {run_id:$run_id,id:id}) "
                   "SET n.in_current_snapshot=true,n.projected_version=$version",
                   run_id=self.run_id, ids=[row["id"] for row in batch], version=version).consume()
        for batch in self._batches(changed_ids):
            tx.run(
                "UNWIND $ids AS claim_id MATCH (a:CKGAssertion {run_id:$run_id,id:claim_id})-[:HEAD]->"
                "(h:CKGConcept {run_id:$run_id}) MATCH (h)-[r]->(:CKGConcept {run_id:$run_id}) "
                "WHERE r.ckg_projection=$projection AND r.claim_id=claim_id DELETE r",
                ids=batch, run_id=self.run_id, projection=_PROJECTION,
            ).consume()
            tx.run(
                "UNWIND $ids AS claim_id MATCH (a:CKGAssertion {run_id:$run_id,id:claim_id})-[r]->() "
                "WHERE type(r) IN ['HEAD','TAIL','SUBJECT','EVIDENCED_BY'] DELETE r",
                ids=batch, run_id=self.run_id,
            ).consume()
        self._link_sources(tx, {"evidence": [], "claims": changed_rows, "revisions": new_revisions})
        self._link_assertions(tx, changed_rows)
        # 记录版本表示最后一次写入；整图版本由下方构建节点原子发布。
        # 未变化记录按快照成员标志读取，无需为每次发布重写版本。
        tx.run(
            "MATCH (b:CKGBuild {run_id:$run_id,id:$run_id}) "
            "SET b.status='ready', b.projected_version=$version,b.snapshot_digest=$digest,"
            "b.updated_at=datetime(),b.node_count=$nodes,b.claim_count=$claims,b.evidence_count=$evidence",
            run_id=self.run_id, version=version, digest=data["digest"],
            nodes=len(data["nodes"]), claims=len(data["claims"]), evidence=len(data["evidence"]),
        ).consume()
        return self._stats(data, len(changed_rows), False)

    def _link_sources(self, tx: Any, data: dict[str, Any]) -> None:
        for batch in self._batches([row for row in data["evidence"] if row["book_id"]]):
            tx.run(
                "UNWIND $rows AS row MATCH (e:CKGEvidence {run_id:$run_id,id:row.id}) "
                "MATCH (b:CKGBook {run_id:$run_id,id:row.book_id}) MERGE (e)-[:FROM_BOOK]->(b)",
                run_id=self.run_id, rows=batch,
            ).consume()
        for batch in self._batches(data["claims"]):
            tx.run(
                "UNWIND $rows AS row MATCH (a:CKGAssertion {run_id:$run_id,id:row.id}) "
                "UNWIND row.evidence_ids AS evidence_id MATCH (e:CKGEvidence {run_id:$run_id,id:evidence_id}) "
                "MERGE (a)-[:EVIDENCED_BY]->(e)", run_id=self.run_id, rows=batch,
            ).consume()
        revisions = [dict(id=row["id"], claim_id=row["claim_id"],
                          props=dict(evidence_ids=row["props"].get("evidence_ids", [])))
                     for row in data["revisions"]]
        for batch in self._batches(revisions):
            tx.run(
                "UNWIND $rows AS row MATCH (b:CKGBuild {run_id:$run_id,id:$run_id}) "
                "MATCH (v:CKGRevision {run_id:$run_id,id:row.id}) MERGE (b)-[:HAS_REVISION]->(v)",
                run_id=self.run_id, rows=batch,
            ).consume()
            tx.run(
                "UNWIND $rows AS row MATCH (v:CKGRevision {run_id:$run_id,id:row.id}) "
                "MATCH (a:CKGAssertion {run_id:$run_id,id:row.claim_id}) MERGE (a)-[:HAS_REVISION]->(v)",
                run_id=self.run_id, rows=batch,
            ).consume()
            tx.run(
                "UNWIND $rows AS row MATCH (v:CKGRevision {run_id:$run_id,id:row.id}) "
                "UNWIND coalesce(row.props.evidence_ids,[]) AS evidence_id "
                "MATCH (e:CKGEvidence {run_id:$run_id,id:evidence_id}) MERGE (v)-[:EVIDENCED_BY]->(e)",
                run_id=self.run_id, rows=batch,
            ).consume()

    def _link_assertions(self, tx: Any, rows: list[dict[str, Any]]) -> None:
        relations = [row for row in rows if row["props"]["kind"] == "relation"]
        facts = [row for row in rows if row["props"]["kind"] == "node_fact"]
        for batch in self._batches(relations):
            tx.run(
                "UNWIND $rows AS row MATCH (a:CKGAssertion {run_id:$run_id,id:row.id}) "
                "MATCH (h:CKGConcept {run_id:$run_id,id:row.props.head}) "
                "MATCH (t:CKGConcept {run_id:$run_id,id:row.props.tail}) MERGE (a)-[:HEAD]->(h) MERGE (a)-[:TAIL]->(t)",
                run_id=self.run_id, rows=batch,
            ).consume()
        for batch in self._batches(facts):
            tx.run(
                "UNWIND $rows AS row MATCH (a:CKGAssertion {run_id:$run_id,id:row.id}) "
                "MATCH (n:CKGConcept {run_id:$run_id,id:row.props.node_id}) MERGE (a)-[:SUBJECT]->(n)",
                run_id=self.run_id, rows=batch,
            ).consume()
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in relations:
            if row["props"]["status"] == "active":
                by_type[row["props"]["relation"]].append(row)
        for relation, grouped in by_type.items():
            if relation not in RELATION_TYPES:
                raise ValueError("Unknown semantic relation type")
            for batch in self._batches(grouped):
                tx.run(
                    "UNWIND $rows AS row MATCH (h:CKGConcept {run_id:$run_id,id:row.props.head}) "
                    "MATCH (t:CKGConcept {run_id:$run_id,id:row.props.tail}) "
                    f"MERGE (h)-[r:{relation} {{run_id:$run_id,ckg_projection:$projection,claim_id:row.id}}]->(t) "
                    "SET r.conditions=row.props.conditions,r.conditions_json=row.props.conditions_json,"
                    "r.status=row.props.status,r.origin=row.props.origin,r.revision=row.props.revision,"
                    "r.verification_status=row.props.verification_status,r.human_decision=row.props.human_decision,"
                    "r.evidence_ids=row.props.evidence_ids",
                    run_id=self.run_id, projection=_PROJECTION, rows=batch,
                ).consume()

    @staticmethod
    def _stats(data: dict[str, Any], changed: int, skipped: bool) -> dict[str, Any]:
        return {"status": "ready", "graph_kind": "working", "projected_version": data["version"],
                "nodes": len(data["nodes"]), "claims": len(data["claims"]), "evidence": len(data["evidence"]),
                "books": len(data["books"]), "changed_claims": changed, "skipped": skipped}

    def summary(self) -> dict[str, Any]:
        def read(tx: Any) -> dict[str, Any]:
            build = tx.run(
                "MATCH (b:CKGBuild {run_id:$run_id,id:$run_id}) "
                "RETURN b.status AS status,b.graph_kind AS graph_kind,b.projected_version AS projected_version",
                run_id=self.run_id,
            ).single()
            if build is None:
                return {"status": "not_projected", "run_id": self.run_id}
            result = {**build.data(), "run_id": self.run_id}
            for label, key in (("CKGConcept", "nodes"), ("CKGAssertion", "claims"), ("CKGEvidence", "evidence"),
                               ("CKGBook", "books"), ("CKGRevision", "revisions")):
                current_only = " AND n.in_current_snapshot=true" if label in {"CKGConcept", "CKGAssertion", "CKGBook"} else ""
                if label == "CKGEvidence":
                    current_only = " AND coalesce(n.staged_version,-1) <= $version"
                elif label == "CKGRevision":
                    current_only = " AND n.in_current_snapshot=true"
                count = tx.run(f"MATCH (n:{label} {{run_id:$run_id}}) WHERE true{current_only} RETURN count(n) AS n",
                               run_id=self.run_id, version=result["projected_version"]).single()
                result[key] = count["n"]
            result["active_relations"] = tx.run(
                "MATCH (:CKGConcept {run_id:$run_id})-[r]->(:CKGConcept {run_id:$run_id}) "
                "WHERE r.ckg_projection=$projection RETURN count(r) AS n", run_id=self.run_id, projection=_PROJECTION,
            ).single()["n"]
            return result
        with self.driver.session(database=self.database) as session:
            return session.execute_read(read)
