from __future__ import annotations

import hashlib
import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ENTITY_TYPES = frozenset({
    "Concept", "Method", "Principle", "Formula", "Algorithm", "Structure",
    "Process", "Metric", "Tool", "Phenomenon",
})
RELATION_TYPES = frozenset({
    "is_a", "part_of", "prerequisite_of", "causes", "affects", "used_for",
    "consists_of", "compared_with", "instance_of", "evaluated_by",
    "implemented_by", "derived_from",
})
FACT_FIELDS = frozenset({"definition", "definition_detail", "alias", "scope", "entity_type"})


class GraphValidationError(ValueError):
    pass


class StaleGraphError(GraphValidationError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _ids(value: Any, label: str = "evidence_ids") -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise GraphValidationError(f"{label} must be a list of nonempty string IDs")
    return sorted(set(value))


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise GraphValidationError(f"{label} must be a {'possibly empty' if empty else 'nonempty'} string")
    return value


def _schema(schema: dict[str, Any]) -> tuple[set[str], set[str]]:
    def names(key: str) -> set[str]:
        raw = schema.get(key)
        if isinstance(raw, dict):
            return set(raw)
        if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
            return set(raw)
        raise GraphValidationError(f"schema.{key} must be a mapping or string list")

    entities, relations = names("entity_types"), names("relation_types")
    if not entities or not entities <= ENTITY_TYPES or not relations or not relations <= RELATION_TYPES:
        raise GraphValidationError("schema contains missing or unsupported entity/relation types")
    return entities, relations


def _relation_key(content: dict[str, Any]) -> str:
    normalized = dict(content)
    if normalized["relation"] == "consists_of":
        normalized["head"], normalized["tail"] = normalized["tail"], normalized["head"]
        normalized["relation"] = "part_of"
    elif normalized["relation"] == "compared_with":
        normalized["head"], normalized["tail"] = sorted((normalized["head"], normalized["tail"]))
    return _json(normalized)


def _content_key(content: dict[str, Any]) -> str:
    return _relation_key(content) if content["kind"] == "relation" else _json(content)


class GraphStore:
    """增量图的事务状态与不可变修改历史；Neo4j 投影从快照恢复。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('version', '0');
            CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_version ON events(version);
            CREATE INDEX IF NOT EXISTS events_projection_order ON events (('evt_' || seq));
            CREATE TABLE IF NOT EXISTS commits (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                details TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        self._row_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._cache_data_version = self._db.execute("PRAGMA data_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._check_row_cache()
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._row_cache.clear()
                raise

    @property
    def version(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0])

    def _check_row_cache(self) -> None:
        version = self._db.execute("PRAGMA data_version").fetchone()[0]
        if version != self._cache_data_version:
            self._row_cache.clear()
            self._cache_data_version = version

    def _rows(self, table: str, *, copy_records: bool = True) -> dict[str, dict[str, Any]]:
        if table not in {"nodes", "claims", "evidence", "books"}:
            raise ValueError("Unsupported table")
        with self._lock:
            if table not in {"nodes", "claims"}:
                return {r["id"]: json.loads(r["data"]) for r in self._db.execute(f"SELECT id,data FROM {table}")}
            self._check_row_cache()
            if table not in self._row_cache:
                self._row_cache[table] = {
                    r["id"]: json.loads(r["data"]) for r in self._db.execute(f"SELECT id,data FROM {table}")
                }
            rows = self._row_cache[table]
            # 事务修改使用副本，分析快照共享不再变化的记录。
            return copy.deepcopy(rows) if copy_records else dict(rows)

    def snapshot(self, include_sources: bool = True, *, share_records: bool = False,
                 events_after_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                result: dict[str, Any] = {
                    "version": self.version, "nodes": self._rows("nodes", copy_records=not share_records),
                    "claims": self._rows("claims", copy_records=not share_records),
                    "books": self._rows("books"), "evidence": {}, "events": [],
                }
                if include_sources:
                    result["evidence"] = self._rows("evidence")
                    result["events"] = [
                        {**json.loads(r["data"]), "id": f"evt_{r['seq']}", "version": r["version"]}
                        for r in self._db.execute("SELECT seq,version,data FROM events WHERE version>? ORDER BY seq",
                                                 (events_after_version if events_after_version is not None else -1,))
                    ]
                if events_after_version is not None:
                    # 校验完整历史，但只在内存中保留尚未发布的变更。
                    digest = hashlib.sha256()
                    for part in self._snapshot_json(events_as_mapping=True):
                        digest.update(part.encode("utf-8"))
                    result["projection_digest"] = digest.hexdigest()
                    result["revision_after_version"] = events_after_version
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def add_sources(self, books: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
        with self._transaction():
            new_books: list[str] = []
            new_evidence: list[str] = []
            for table, items in (("books", books), ("evidence", evidence)):
                for incoming in items:
                    item = dict(incoming)
                    identifier = item.get("id") or (item.get("book_id") if table == "books" else None)
                    _string(identifier, f"{table}.id")
                    item["id"] = identifier
                    if table == "evidence":
                        _string(item.get("text"), "evidence.text")
                        _string(item.get("book_id"), "evidence.book_id")
                        if not self._db.execute("SELECT 1 FROM books WHERE id=?", (item["book_id"],)).fetchone():
                            raise GraphValidationError(f"Unknown evidence book: {item['book_id']}")
                        start, end = item.get("start"), item.get("end")
                        if start is not None or end is not None:
                            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                                raise GraphValidationError("Invalid evidence character offsets")
                    fingerprint = _hash(item)
                    old = self._db.execute(f"SELECT fingerprint FROM {table} WHERE id=?", (identifier,)).fetchone()
                    if old and old[0] != fingerprint:
                        raise GraphValidationError(f"Immutable {table} source changed: {identifier}")
                    if not old:
                        (new_books if table == "books" else new_evidence).append(identifier)
                    self._db.execute(
                        f"INSERT OR IGNORE INTO {table}(id,fingerprint,data) VALUES (?,?,?)",
                        (identifier, fingerprint, _json(item)),
                    )
            if new_books or new_evidence:
                version = self.version + 1
                self._event(version, "add_sources", "sources", None,
                            {"book_ids": new_books, "evidence_ids": new_evidence})
                self._db.execute("UPDATE metadata SET value=? WHERE key='version'", (str(version),))

    def _pin_schema(self, schema: dict[str, Any]) -> tuple[set[str], set[str]]:
        names = _schema(schema)
        fingerprint = _hash(schema)
        old = self._db.execute("SELECT value FROM metadata WHERE key='schema_fingerprint'").fetchone()
        if old and old[0] != fingerprint:
            raise GraphValidationError("Schema differs from the schema pinned to this graph")
        self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_fingerprint',?)", (fingerprint,))
        return names

    def _write(self, table: str, identifier: str, value: dict[str, Any]) -> None:
        if table not in {"nodes", "claims"}:
            raise ValueError("Unsupported table")
        with self._lock:
            if not self._db.in_transaction:
                self._check_row_cache()
            rows = self._row_cache.get(table)
            if rows is not None and rows.get(identifier) == value:
                return
            serialized = _json(value)
            self._db.execute(
                f"INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (identifier, serialized),
            )
            if rows is not None:
                rows[identifier] = json.loads(serialized)

    def _event(self, version: int, operation: str, target_id: str,
               before: dict[str, Any] | None, after: dict[str, Any] | None,
               change: dict[str, Any] | None = None) -> None:
        change = change or {}
        event = {
            "operation": operation, "target_id": target_id, "before": before, "after": after,
            "reason": change.get("reason", ""), "proposal_id": change.get("proposal_id"),
            "group_id": change.get("group_id"), "evidence_ids": change.get("evidence_ids", []),
            "compared_evidence_ids": change.get("compared_evidence_ids", []),
            "proposal": change,
        }
        self._db.execute("INSERT INTO events(version,data) VALUES (?,?)", (version, _json(event)))

    @staticmethod
    def _node(raw: dict[str, Any], identifier: str, entity_types: set[str], origin: str) -> dict[str, Any]:
        name = _string(raw.get("name"), "node.name")
        kind = raw.get("node_kind", "concept")
        if kind not in {"concept", "instance"}:
            raise GraphValidationError("node_kind must be concept or instance")
        if raw.get("entity_type") not in entity_types:
            raise GraphValidationError("Unknown node entity_type")
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
            raise GraphValidationError("Node aliases must be nonempty strings")
        definition = raw.get("definition")
        if definition is not None:
            _string(definition, "node.definition", empty=True)
        status = raw.get("definition_status", "summarized" if definition else "not_provided")
        if status not in {"explicit", "summarized", "partial", "not_provided"}:
            raise GraphValidationError("Unknown definition_status")
        if status == "not_provided" and definition:
            raise GraphValidationError("not_provided definition must be empty")
        evidence = _ids(raw.get("evidence_ids", []))
        definition_evidence = _ids(raw.get("definition_evidence_ids", []), "definition_evidence_ids")
        if not set(definition_evidence) <= set(evidence):
            raise GraphValidationError("Definition evidence must be part of occurrence evidence")
        if origin != "parametric_prior" and definition and not definition_evidence:
            raise GraphValidationError("A textbook definition needs its own supporting evidence")
        scope = raw.get("scope", "") or ""
        _string(scope, "node.scope", empty=True)
        return {
            "id": identifier, "name": name, "aliases": sorted(set(aliases)),
            "entity_type": raw["entity_type"], "node_kind": kind, "definition": definition or None,
            "definition_status": status, "scope": scope, "evidence_ids": evidence,
            "mention_evidence_ids": evidence, "definition_evidence_ids": definition_evidence,
            "origin": origin, "verification_status": "unverified" if origin == "parametric_prior" else "mentioned",
            "human_decision": "undecided",
        }

    @staticmethod
    def _validate_content(content: Any, nodes: dict[str, dict[str, Any]],
                          entity_types: set[str], relation_types: set[str]) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise GraphValidationError("Claim content must be an object")
        content = dict(content)
        kind = content.get("kind")
        _string(content.get("conditions", ""), "claim.conditions", empty=True)
        content.setdefault("conditions", "")
        if kind == "relation":
            if set(content) - {"kind", "head", "relation", "tail", "conditions"}:
                raise GraphValidationError("Unknown relation content field")
            head, tail, rel = content.get("head"), content.get("tail"), content.get("relation")
            if head not in nodes or tail not in nodes or rel not in relation_types:
                raise GraphValidationError("Invalid relation endpoint or relation type")
            if head == tail:
                raise GraphValidationError("Self relations are not allowed")
            if rel == "instance_of" and nodes[head].get("node_kind") != "instance":
                raise GraphValidationError("instance_of requires an explicit instance head")
            if rel == "evaluated_by" and nodes[tail].get("entity_type") != "Metric":
                raise GraphValidationError("evaluated_by requires a Metric tail")
        elif kind == "node_fact":
            if set(content) - {"kind", "node_id", "field", "value", "conditions", "definition_status"}:
                raise GraphValidationError("Unknown node fact content field")
            if content.get("node_id") not in nodes or content.get("field") not in FACT_FIELDS:
                raise GraphValidationError("Invalid node fact target or field")
            _string(content.get("value"), "node fact value")
            if content["field"] == "entity_type" and content["value"] not in entity_types:
                raise GraphValidationError("Unknown entity_type fact value")
            if content["field"] in {"definition", "definition_detail"}:
                status = content.get("definition_status", "partial" if content["field"] == "definition_detail" else "summarized")
                if status not in {"explicit", "summarized", "partial"}:
                    raise GraphValidationError("Definition facts need a valid definition_status")
                if content["field"] == "definition" and status == "partial":
                    raise GraphValidationError("Partial definitions must use definition_detail")
                content["definition_status"] = status
            elif "definition_status" in content:
                raise GraphValidationError("definition_status belongs only to definition facts")
        else:
            raise GraphValidationError("Unknown claim kind")
        return content

    @staticmethod
    def _new_claim(identifier: str, content: dict[str, Any], origin: str,
                   evidence_ids: list[str], version: int) -> dict[str, Any]:
        return {
            "id": identifier, "revision": 1, "content": content, "origin": origin,
            "status": "active", "evidence_ids": evidence_ids, "created_version": version,
            "updated_version": version, "human_decision": "undecided",
        }

    def _node_claims(self, node: dict[str, Any], version: int,
                     claims: dict[str, dict[str, Any]], change: dict[str, Any] | None = None) -> None:
        fields: list[tuple[str, str, list[str], str | None]] = []
        evidence = node["evidence_ids"]
        fields.append(("entity_type", node["entity_type"], evidence, None))
        if node.get("scope"):
            fields.append(("scope", node["scope"], evidence, None))
        fields.extend(("alias", value, evidence, None) for value in node.get("aliases", []))
        if node.get("definition"):
            field = "definition_detail" if node["definition_status"] == "partial" else "definition"
            fields.append((field, node["definition"], node["definition_evidence_ids"], node["definition_status"]))
        for field, value, sources, status in fields:
            content: dict[str, Any] = {"kind": "node_fact", "node_id": node["id"], "field": field, "value": value, "conditions": ""}
            if status:
                content["definition_status"] = status
            identifier = "c_" + _hash([node["id"], content])[:32]
            claim = self._new_claim(identifier, content, node["origin"], sources, version)
            claims[identifier] = claim
            self._write("claims", identifier, claim)
            self._event(version, "add_claim", identifier, None, claim, change)

    def _refresh_nodes(self, node_ids: set[str], nodes: dict[str, dict[str, Any]],
                       claims: dict[str, dict[str, Any]], version: int) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in node_ids}
        for claim in claims.values():
            content = claim["content"]
            if claim["status"] == "active" and content["kind"] == "node_fact" and content["node_id"] in grouped:
                grouped[content["node_id"]].append(claim)
        for identifier, facts in grouped.items():
            node = nodes[identifier]
            before = json.loads(_json(node))
            facts.sort(key=lambda item: (item["updated_version"], item["revision"], item["id"]))
            unconditional = [fact for fact in facts if not fact["content"].get("conditions")]
            by_field = {field: [fact for fact in unconditional if fact["content"]["field"] == field] for field in FACT_FIELDS}
            # 只以有效完整定义更新摘要，局部特征和有条件事实保留在断言层。
            definitions = by_field["definition"]
            best_definition = definitions[-1] if definitions else None
            node["definition"] = best_definition["content"]["value"] if best_definition else None
            node["definition_status"] = best_definition["content"].get("definition_status", "summarized") if best_definition else "not_provided"
            node["definition_evidence_ids"] = best_definition["evidence_ids"] if best_definition else []
            node["definition_details"] = [fact["content"]["value"] for fact in by_field["definition_detail"]]
            node["definitions"] = [dict(value=fact["content"]["value"],
                                         definition_status=fact["content"].get("definition_status"),
                                         evidence_ids=fact["evidence_ids"], conditions=fact["content"].get("conditions", ""))
                                   for fact in facts if fact["content"]["field"] in {"definition", "definition_detail"}]
            node["aliases"] = sorted({fact["content"]["value"] for fact in by_field["alias"]})
            node["scope"] = by_field["scope"][-1]["content"]["value"] if by_field["scope"] else ""
            node["scope_variants"] = sorted({fact["content"]["value"] for fact in facts
                                              if fact["content"]["field"] == "scope"})
            node["entity_type"] = by_field["entity_type"][-1]["content"]["value"] if by_field["entity_type"] else None
            evidence = set(node.get("mention_evidence_ids", []))
            for fact in facts:
                evidence.update(fact["evidence_ids"])
            node["evidence_ids"] = sorted(evidence)
            has_prior = any(fact["origin"] in {"parametric_prior", "mixed"} for fact in facts)
            has_text = bool(evidence)
            node["origin"] = "mixed" if has_prior and has_text else "textbook" if has_text else "parametric_prior"
            if facts and all(fact["evidence_ids"] for fact in facts):
                node["verification_status"] = "textbook_grounded"
            elif any(fact["evidence_ids"] for fact in facts):
                node["verification_status"] = "partially_supported"
            else:
                node["verification_status"] = "mentioned" if has_text else "unverified"
            self._write("nodes", identifier, node)
            if before != node:
                self._event(version, "refresh_node", identifier, before, node)

    def _unique_names(self, nodes, claims, version, name_qualifiers=None):
        name_qualifiers = name_qualifiers or {}
        groups = {}
        for identifier, node in sorted(nodes.items()):
            base = node.get('unqualified_name', node['name'])
            groups.setdefault(' '.join(base.split()), []).append(identifier)
        occupied = {' '.join(node['name'].split()) for node in nodes.values()}
        changed = set()
        for base, identifiers in groups.items():
            if len(identifiers) < 2:
                continue
            qualifiers = {identifier: name_qualifiers.get(identifier) or nodes[identifier].get('name_qualifier') or
                          nodes[identifier].get('scope') or
                          nodes[identifier].get('definition') or '含义待厘清' for identifier in identifiers}
            counts = {value: list(qualifiers.values()).count(value) for value in set(qualifiers.values())}
            for number, identifier in enumerate(identifiers, 1):
                node = nodes[identifier]
                qualifier = ' '.join(qualifiers[identifier].split())
                if counts[qualifiers[identifier]] > 1:
                    definition = node.get('definition') or '含义待厘清'
                    qualifier = qualifier + '：' + definition if definition != qualifier else qualifier
                qualifier = qualifier[:48] + ('…' if len(qualifier) > 48 else '')
                target = base + '（' + qualifier + '）'
                if target in occupied and target != node['name']:
                    target = base + '（' + qualifier + '；待厘清 ' + str(number) + '）'
                    while target in occupied and target != node['name']:
                        number += 1
                        target = base + '（' + qualifier + '；待厘清 ' + str(number) + '）'
                if node['name'] == target:
                    continue
                before = json.loads(_json(node))
                node['name_qualifier'] = qualifiers[identifier]
                node['unqualified_name'] = base
                node['name'] = target
                for alias in {base, before['name']}:
                    content = dict(kind='node_fact', node_id=identifier, field='alias', value=alias, conditions='')
                    cid = 'c_' + _hash([identifier, content])[:32]
                    if cid not in claims:
                        claim = self._new_claim(cid, content, node['origin'], node.get('evidence_ids', []), version)
                        claims[cid] = claim
                        self._write('claims', cid, claim)
                        self._event(version, 'add_claim', cid, None, claim)
                node['aliases'] = sorted(set(node['aliases']) | {base, before['name']})
                occupied.add(target)
                self._write('nodes', identifier, node)
                self._event(version, 'qualify_name', identifier, before, node,
                            {'reason': '独立含义使用限定名称，未确定同义的节点保留待厘清标识。'})
                changed.add(identifier)
        return changed

    def seed_prior(self, prior: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        with self._transaction():
            entity_types, relation_types = self._pin_schema(schema)
            fingerprint = _hash(prior)
            existing = self._db.execute("SELECT value FROM metadata WHERE key='prior_fingerprint'").fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise GraphValidationError("G0 differs from the immutable initial prior")
                return {"version": self.version, "applied": 0, "node_map": {}, "changed_node_ids": []}
            if self._db.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
                raise GraphValidationError("G0 can only seed an empty graph")
            concepts, relations = prior.get("concepts", []), prior.get("relations", [])
            if not isinstance(concepts, list) or not isinstance(relations, list) or not concepts:
                raise GraphValidationError("G0 needs a nonempty concepts list and a relations list")
            version = self.version + 1
            nodes: dict[str, dict[str, Any]] = {}
            claims: dict[str, dict[str, Any]] = {}
            node_map: dict[str, str] = {}
            for raw in concepts:
                old_id = _string(raw.get("id"), "prior concept.id")
                if old_id in node_map or raw.get("evidence_ids") or raw.get("definition_evidence_ids"):
                    raise GraphValidationError("G0 IDs must be unique and evidence must be empty")
                if raw.get("node_kind", "concept") != "concept":
                    raise GraphValidationError("G0 contains a non-concept node")
                identifier = "n_" + _hash(["prior", fingerprint, old_id])[:32]
                node_map[old_id] = identifier
                node = self._node(raw, identifier, entity_types, "parametric_prior")
                nodes[identifier] = node
                self._write("nodes", identifier, node)
                self._event(version, "add_node", identifier, None, node)
                self._node_claims(node, version, claims)
            seen_ids: set[str] = set()
            seen_content: set[str] = set()
            for raw in relations:
                old_id = _string(raw.get("id"), "prior relation.id")
                if old_id in seen_ids or raw.get("evidence_ids"):
                    raise GraphValidationError("G0 relation IDs must be unique and evidence must be empty")
                seen_ids.add(old_id)
                content = {
                    "kind": "relation", "head": node_map.get(raw.get("head")),
                    "tail": node_map.get(raw.get("tail")), "relation": raw.get("relation"),
                    "conditions": raw.get("conditions", ""),
                }
                content = self._validate_content(content, nodes, entity_types, relation_types)
                key = _content_key(content)
                if key in seen_content:
                    continue
                seen_content.add(key)
                identifier = "c_" + _hash(["prior", fingerprint, old_id])[:32]
                claim = self._new_claim(identifier, content, "parametric_prior", [], version)
                claims[identifier] = claim
                self._write("claims", identifier, claim)
                self._event(version, "add_claim", identifier, None, claim)
            self._refresh_nodes(set(nodes), nodes, claims, version)
            self._unique_names(nodes, claims, version)
            self._db.execute("INSERT INTO metadata VALUES ('prior_fingerprint',?)", (fingerprint,))
            self._db.execute("UPDATE metadata SET value=? WHERE key='version'", (str(version),))
            return {"version": version, "applied": len(nodes) + len(claims), "node_map": node_map, "changed_node_ids": sorted(nodes)}

    def commit(self, proposal: dict[str, Any], allowed_nodes: list[dict[str, Any]],
               source_evidence_ids: list[str], reference_evidence_ids: list[str],
               schema: dict[str, Any], name_qualifiers=None) -> dict[str, Any]:
        fingerprint = _hash([proposal, allowed_nodes, sorted(source_evidence_ids), sorted(reference_evidence_ids), schema, name_qualifiers])
        request_id = proposal.get("request_id") or fingerprint
        _string(request_id, "request_id")
        with self._transaction():
            entity_types, relation_types = self._pin_schema(schema)
            saved = self._db.execute("SELECT fingerprint,result FROM commits WHERE id=?", (request_id,)).fetchone()
            if saved:
                if saved[0] != fingerprint:
                    raise GraphValidationError("A committed request_id was reused with different inputs")
                return json.loads(saved[1])
            base_version = proposal.get("base_graph_version")
            if type(base_version) is not int or base_version != self.version:
                raise StaleGraphError(f"Proposal base version {base_version!r} differs from graph version {self.version}")
            source_ids, reference_ids = set(_ids(source_evidence_ids)), set(_ids(reference_evidence_ids))
            for identifier in source_ids | reference_ids:
                if not self._db.execute("SELECT 1 FROM evidence WHERE id=?", (identifier,)).fetchone():
                    raise GraphValidationError(f"Unknown evidence ID: {identifier}")
            nodes, claims = self._rows("nodes"), self._rows("claims")
            allowed: dict[str, dict[str, Any]] = {}
            provisional: set[str] = set()
            for raw in allowed_nodes:
                identifier = _string(raw.get("id"), "allowed node.id")
                if identifier in allowed or raw.get("identity_status") != "resolved":
                    raise GraphValidationError("Allowed node IDs must be unique and identity_status resolved")
                allowed[identifier] = raw
                if raw.get("lifecycle") == "provisional":
                    if identifier in nodes:
                        raise GraphValidationError("Provisional node ID already exists")
                    provisional.add(identifier)
                elif identifier not in nodes:
                    raise GraphValidationError(f"Unknown canonical allowed node: {identifier}")
            changes = proposal.get("changes", [])
            follow_up = proposal.get("follow_up", [])
            if not isinstance(changes, list) or not isinstance(follow_up, list):
                raise GraphValidationError("changes and follow_up must be lists")
            seen_proposals: set[str] = set()
            additions: dict[str, dict[str, Any]] = {}
            modified: set[str] = set()
            for change in changes:
                pid = _string(change.get("proposal_id"), "proposal_id")
                _string(change.get("group_id"), "group_id")
                _string(change.get("reason"), "reason")
                if pid in seen_proposals:
                    raise GraphValidationError("Duplicate proposal_id")
                seen_proposals.add(pid)
                operation, target = change.get("operation"), change.get("target_id")
                if operation not in {"add_node", "add_claim", "attach_evidence", "revise_claim", "retire_claim"}:
                    raise GraphValidationError("Unknown graph operation")
                payload = change.get("payload")
                if not isinstance(payload, dict):
                    raise GraphValidationError("Change payload must be an object")
                expected = {"claim"} if operation == "add_claim" else {"before", "after"} if operation == "revise_claim" else set()
                if set(payload) != expected:
                    raise GraphValidationError(f"Unexpected payload keys for {operation}")
                evidence = _ids(change.get("evidence_ids", []))
                compared = _ids(change.get("compared_evidence_ids", []), "compared_evidence_ids")
                if not evidence or not set(evidence) <= source_ids or not set(compared) <= reference_ids:
                    raise GraphValidationError("Each update needs current source evidence and valid reference evidence")
                if operation == "add_node":
                    if target not in provisional or target in additions:
                        raise GraphValidationError("add_node requires one distinct provisional node")
                    additions[target] = change
                elif operation == "add_claim":
                    if target is not None:
                        raise GraphValidationError("add_claim target_id must be null")
                else:
                    if target not in claims or claims[target]["status"] != "active":
                        raise GraphValidationError("Changes can only target an existing active claim")
                    if target in modified:
                        raise GraphValidationError("The same claim may only be changed once per proposal")
                    modified.add(target)
                    prior_claim = claims[target]
                    content = prior_claim["content"]
                    endpoint_ids = {content["node_id"]} if content["kind"] == "node_fact" else {content["head"], content["tail"]}
                    if not endpoint_ids <= set(allowed):
                        raise GraphValidationError("Target claim endpoints are outside allowed_nodes")
                    if operation in {"revise_claim", "retire_claim"} and prior_claim["evidence_ids"]:
                        if not set(prior_claim["evidence_ids"]) <= set(compared):
                            raise GraphValidationError("Changing a supported claim requires its actual previous evidence")
                    if operation == "revise_claim" and payload["before"] != content:
                        raise StaleGraphError("revise_claim.before no longer matches current content")
            mentioned_followups: set[str] = set()
            for item in follow_up:
                mentioned_followups.update(_ids(item.get("related_ids", []), "follow_up.related_ids"))
            if provisional - set(additions) - mentioned_followups:
                raise GraphValidationError("Every provisional node requires add_node or a specific follow_up")
            mentions: dict[str, list[str]] = {}
            for identifier, raw in allowed.items():
                if identifier in nodes:
                    new_mentions = set(_ids(raw.get("evidence_ids", []))) & source_ids
                    if new_mentions - set(nodes[identifier].get("mention_evidence_ids", [])):
                        mentions[identifier] = sorted(new_mentions | set(nodes[identifier].get("mention_evidence_ids", [])))
            version = base_version + 1 if changes or mentions else base_version
            node_map: dict[str, str] = {}
            changed_nodes: set[str] = set()
            for identifier, mention_ids in mentions.items():
                old_node = json.loads(_json(nodes[identifier]))
                nodes[identifier]["mention_evidence_ids"] = mention_ids
                changed_nodes.add(identifier)
                self._write("nodes", identifier, nodes[identifier])
                self._event(version, "attach_mention", identifier, old_node, nodes[identifier],
                            {"evidence_ids": sorted(set(mention_ids) & source_ids)})
            for temporary_id, change in additions.items():
                identifier = "n_" + _hash([request_id, base_version, temporary_id])[:32]
                if identifier in nodes:
                    raise GraphValidationError("Allocated canonical node ID already exists")
                raw = allowed[temporary_id]
                if not set(_ids(raw.get("evidence_ids", []))) <= source_ids or not raw.get("evidence_ids"):
                    raise GraphValidationError("A new node needs occurrence evidence from the current source")
                node = self._node(raw, identifier, entity_types, "textbook")
                nodes[identifier] = node
                node_map[temporary_id] = identifier
                changed_nodes.add(identifier)
                self._write("nodes", identifier, node)
                self._event(version, "add_node", identifier, None, node, change)
                self._node_claims(node, version, claims, change)

            def resolved_content(raw: Any, change: dict[str, Any]) -> dict[str, Any]:
                if not isinstance(raw, dict):
                    raise GraphValidationError("Claim must be an object")
                content = dict(raw)
                keys = ("node_id",) if content.get("kind") == "node_fact" else ("head", "tail")
                for key in keys:
                    endpoint = content.get(key)
                    if endpoint not in allowed:
                        raise GraphValidationError("Claim endpoint is outside allowed_nodes")
                    if endpoint in provisional:
                        if endpoint not in additions or additions[endpoint]["group_id"] != change["group_id"]:
                            raise GraphValidationError("A new endpoint and its dependent claim must belong to the same group")
                        content[key] = node_map[endpoint]
                return self._validate_content(content, nodes, entity_types, relation_types)

            def touch(content: dict[str, Any]) -> None:
                if content["kind"] == "node_fact":
                    changed_nodes.add(content["node_id"])
                else:
                    changed_nodes.update((content["head"], content["tail"]))

            by_content = {_content_key(claim["content"]): cid for cid, claim in claims.items() if claim["status"] == "active"}
            single_fields: dict[tuple[str, str, str], set[str]] = {}

            def field_key(content: dict[str, Any]) -> tuple[str, str, str] | None:
                if content["kind"] == "node_fact" and content["field"] in {"definition", "scope", "entity_type"}:
                    return (content["node_id"], content["field"], content.get("conditions", ""))
                return None

            for cid, existing_claim in claims.items():
                single_key = field_key(existing_claim["content"])
                if single_key and existing_claim["status"] == "active":
                    single_fields.setdefault(single_key, set()).add(cid)

            def protect_single_field(content: dict[str, Any], excluding_id: str | None = None) -> None:
                key = field_key(content)
                if not key:
                    return
                for existing_id in single_fields.get(key, set()):
                    if existing_id != excluding_id and claims[existing_id]["content"] != content:
                        raise GraphValidationError("Changing an existing definition/scope/type needs revise_claim; partial additions use definition_detail")

            for change in changes:
                operation = change["operation"]
                if operation == "add_node":
                    continue
                target = change.get("target_id")
                evidence = _ids(change["evidence_ids"])
                if operation == "add_claim":
                    content = resolved_content(change["payload"]["claim"], change)
                    key = _content_key(content)
                    if key in by_content:
                        target = by_content[key]
                        old = claims[target]
                        claim = json.loads(_json(old))
                        merged_evidence = sorted(set(old["evidence_ids"]) | set(evidence))
                        if merged_evidence == old["evidence_ids"]:
                            continue
                        claim["evidence_ids"] = merged_evidence
                        claim["revision"] += 1
                        claim["updated_version"] = version
                        claim["origin"] = "mixed" if old["origin"] in {"parametric_prior", "mixed"} else "textbook"
                        self._event(version, "attach_evidence", target, old, claim, change)
                    else:
                        protect_single_field(content)
                        target = "c_" + _hash([request_id, change["proposal_id"], content])[:32]
                        claim = self._new_claim(target, content, "textbook", evidence, version)
                        by_content[key] = target
                        single_key = field_key(content)
                        if single_key:
                            single_fields.setdefault(single_key, set()).add(target)
                        self._event(version, operation, target, None, claim, change)
                else:
                    old = claims[target]
                    claim = json.loads(_json(old))
                    claim["revision"] += 1
                    claim["updated_version"] = version
                    if operation == "attach_evidence":
                        claim["evidence_ids"] = sorted(set(old["evidence_ids"]) | set(evidence))
                        claim["origin"] = "mixed" if old["origin"] in {"parametric_prior", "mixed"} else "textbook"
                    elif operation == "revise_claim":
                        content = resolved_content(change["payload"]["after"], change)
                        before_content = old["content"]
                        if content["kind"] != before_content["kind"]:
                            raise GraphValidationError("A revision cannot change claim kind")
                        if content["kind"] == "node_fact" and (
                            content["node_id"] != before_content["node_id"] or content["field"] != before_content["field"]
                        ):
                            raise GraphValidationError("A node fact revision cannot change identity or field")
                        key = _content_key(content)
                        if key in by_content and by_content[key] != target:
                            raise GraphValidationError("Revised claim duplicates an active claim; attach evidence and retire instead")
                        protect_single_field(content, target)
                        by_content.pop(_content_key(before_content), None)
                        by_content[key] = target
                        old_single_key, new_single_key = field_key(before_content), field_key(content)
                        if old_single_key:
                            single_fields[old_single_key].discard(target)
                        if new_single_key:
                            single_fields.setdefault(new_single_key, set()).add(target)
                        claim["content"] = content
                        # 修订后的断言只继承明确支持新含义的证据，旧证据保留在历史中。
                        claim["evidence_ids"] = evidence
                        claim["origin"] = "textbook"
                        touch(before_content)
                    else:
                        claim["status"] = "retired"
                        claim["retirement_evidence_ids"] = evidence
                        claim["retirement_reason"] = change["reason"]
                        by_content.pop(_content_key(old["content"]), None)
                        single_key = field_key(old["content"])
                        if single_key:
                            single_fields[single_key].discard(target)
                    self._event(version, operation, target, old, claim, change)
                claims[target] = claim
                self._write("claims", target, claim)
                touch(claim["content"])
            self._refresh_nodes(changed_nodes, nodes, claims, version)
            changed_nodes.update(self._unique_names(nodes, claims, version,
                {node_map.get(key, key): value for key, value in (name_qualifiers or {}).items()}))
            # 字段修订后再次检查类型相关关系，禁止留下已失效的 Metric/instance 约束。
            for claim in claims.values():
                if claim["status"] == "active" and claim["content"]["kind"] == "relation":
                    content = claim["content"]
                    if content["head"] in changed_nodes or content["tail"] in changed_nodes:
                        self._validate_content(content, nodes, entity_types, relation_types)
            self._db.execute("UPDATE metadata SET value=? WHERE key='version'", (str(version),))
            result = {"version": version, "applied": len(changes), "node_map": node_map, "changed_node_ids": sorted(changed_nodes)}
            self._db.execute("INSERT INTO commits VALUES (?,?,?)", (request_id, fingerprint, _json(result)))
            return result

    def merge_nodes(self, mapping: dict[str, str], schema: dict[str, Any], base_version: int, name_qualifiers=None) -> dict[str, Any]:
        with self._transaction():
            entity_types, relation_types = self._pin_schema(schema)
            if base_version != self.version:
                raise StaleGraphError("Semantic merge version differs from current graph version")
            nodes, claims = self._rows("nodes"), self._rows("claims")
            if not mapping:
                renamed = self._unique_names(nodes, claims, self.version + 1, name_qualifiers)
                if renamed:
                    self._db.execute("UPDATE metadata SET value=? WHERE key='version'", (str(self.version + 1),))
                return dict(version=self.version, merged_nodes=0, redirected_claims=0, merged_claims=0,
                            renamed_nodes=len(renamed))
            for source, target in mapping.items():
                if source not in nodes or target not in nodes or source == target or target in mapping:
                    raise GraphValidationError("Semantic merge needs existing nodes and direct canonical targets")
                types = {nodes[source]["entity_type"], nodes[target]["entity_type"]}
                if len(types) > 1 and ("Concept" not in types or "Metric" in types):
                    raise GraphValidationError("Incompatible semantic merge entity types")
            version = self.version + 1
            originals = {identifier: json.loads(_json(node)) for identifier, node in nodes.items()}
            targets = set(mapping.values())
            for source, target in mapping.items():
                old, canonical = nodes[source], nodes[target]
                canonical["mention_evidence_ids"] = sorted(set(canonical.get("mention_evidence_ids", [])) |
                                                            set(old.get("mention_evidence_ids", [])))
                canonical["merged_from"] = sorted(set(canonical.get("merged_from", [])) |
                                                   set(old.get("merged_from", [])) | {source})
                if old["name"] != canonical["name"]:
                    content = dict(kind="node_fact", node_id=target, field="alias", value=old["name"], conditions="")
                    cid = "c_" + _hash(["semantic_alias", target, old["name"]])[:32]
                    if cid not in claims:
                        claims[cid] = self._new_claim(cid, content, old["origin"], old.get("evidence_ids", []), version)
                        self._event(version, "add_claim", cid, None, claims[cid], {"reason": "原节点名称保留为规范节点别名。"})
            redirected, merged = 0, 0
            by_content: dict[str, str] = {}
            for cid, claim in sorted(claims.items(), key=lambda item: (
                    item[1]["content"].get("node_id", item[1]["content"].get("head")) in mapping, item[0])):
                before = json.loads(_json(claim))
                content = dict(claim["content"])
                for key in ("node_id",) if content["kind"] == "node_fact" else ("head", "tail"):
                    content[key] = mapping.get(content[key], content[key])
                if content["kind"] == "node_fact" and before["content"]["node_id"] in mapping:
                    canonical = nodes[content["node_id"]]
                    if content["field"] == "definition" and content["value"] != canonical.get("definition"):
                        content["field"] = "definition_detail"
                    elif content["field"] == "scope" and content["value"] != canonical.get("scope", ""):
                        content["conditions"] = content.get("conditions") or "教材语境：" + content["value"]
                    elif content["field"] == "entity_type" and content["value"] != canonical["entity_type"]:
                        claim["status"] = "retired"
                        claim["retirement_reason"] = "同义节点统一使用规范节点的实体类型，原标签保留在历史中。"
                elif content["kind"] == "relation" and content["head"] == content["tail"]:
                    claim["status"] = "retired"
                    claim["retirement_reason"] = "同义节点合并后形成自环。"
                claim["content"] = content
                if claim["status"] == "active":
                    key = _content_key(content)
                    existing_id = by_content.get(key)
                    if existing_id:
                        kept = claims[existing_id]
                        old_kept = json.loads(_json(kept))
                        kept["evidence_ids"] = sorted(set(kept["evidence_ids"]) | set(claim["evidence_ids"]))
                        if kept["evidence_ids"] and (kept["origin"] != "textbook" or claim["origin"] != "textbook"):
                            kept["origin"] = "mixed"
                        if kept != old_kept:
                            kept["revision"] += 1
                            kept["updated_version"] = version
                            self._write("claims", existing_id, kept)
                            self._event(version, "attach_evidence", existing_id, old_kept, kept,
                                        {"reason": "累积同义节点的重复断言证据。"})
                        claim["status"] = "retired"
                        claim["merged_into"] = existing_id
                        merged += 1
                    else:
                        by_content[key] = cid
                if claim != before:
                    claim["revision"] += 1
                    claim["updated_version"] = version
                    redirected += int(content != before["content"])
                    self._event(version, "redirect_claim", cid, before, claim,
                                {"reason": "同义概念统一到规范节点，保留原定义、条件及证据。"})
                self._write("claims", cid, claim)
            for source, target in mapping.items():
                self._event(version, "merge_node", source, originals[source], None,
                            {"reason": "名称与定义的 BGE-M3 语义匹配。", "canonical_id": target})
                self._db.execute("DELETE FROM nodes WHERE id=?", (source,))
                self._row_cache["nodes"].pop(source, None)
                del nodes[source]
            for claim in claims.values():
                if claim["status"] == "active":
                    self._validate_content(claim["content"], nodes, entity_types, relation_types)
            self._refresh_nodes(targets, nodes, claims, version)
            renamed = self._unique_names(nodes, claims, version, name_qualifiers)
            self._db.execute("UPDATE metadata SET value=? WHERE key='version'", (str(version),))
            return dict(version=version, merged_nodes=len(mapping), redirected_claims=redirected, merged_claims=merged,
                        renamed_nodes=len(renamed))

    def record_chunk(self, chunk_id: str, fingerprint: str, status: str, details: dict[str, Any]) -> None:
        self.record_chunks([(chunk_id, fingerprint, status, details)])

    def record_chunks(self, rows: list[tuple[str, str, str, dict[str, Any]]]) -> None:
        with self._transaction():
            for chunk_id, fingerprint, status, details in rows:
                _string(chunk_id, "chunk_id")
                _string(fingerprint, "chunk fingerprint")
                _string(status, "chunk status")
                old = self._db.execute("SELECT fingerprint FROM chunks WHERE id=?", (chunk_id,)).fetchone()
                if old and old[0] != fingerprint:
                    raise GraphValidationError(f"Chunk input changed: {chunk_id}")
                self._db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "status=excluded.status,details=excluded.details,updated_at=excluded.updated_at",
                    (chunk_id, fingerprint, status, _json(details), datetime.now(timezone.utc).isoformat()),
                )

    def chunk_state(self, chunk_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if not row:
                return None
            return {"id": row["id"], "fingerprint": row["fingerprint"], "status": row["status"],
                    "details": json.loads(row["details"]), "updated_at": row["updated_at"]}

    def _snapshot_json(self, *, events_as_mapping: bool = False) -> Iterator[str]:
        encode = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode
        yield "{"
        for index, table in enumerate(("books", "claims", "events", "evidence", "nodes")):
            yield ("," if index else "") + encode(table) + ":"
            if table == "events":
                yield "{" if events_as_mapping else "["
                order = "'evt_' || seq" if events_as_mapping else "seq"
                rows = self._db.execute(f"SELECT seq,version,data FROM events ORDER BY {order}")
                for number, row in enumerate(rows):
                    event = {**json.loads(row["data"]), "id": f"evt_{row['seq']}", "version": row["version"]}
                    yield ("," if number else "") + (encode(event["id"]) + ":" if events_as_mapping else "")
                    yield encode(event)
                yield "}" if events_as_mapping else "]"
            else:
                yield "{"
                for number, row in enumerate(self._db.execute(f"SELECT id,data FROM {table} ORDER BY id")):
                    yield ("," if number else "") + encode(row["id"]) + ":"
                    yield encode(json.loads(row["data"]))
                yield "}"
        yield ',"version":' + str(self.version) + "}"

    def export_snapshot(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self._lock:
            self._db.execute("BEGIN")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    for part in self._snapshot_json():
                        stream.write(part)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        temporary.replace(destination)
        return destination


Store = GraphStore
