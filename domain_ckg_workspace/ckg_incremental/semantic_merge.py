from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .runtime import OutputLimit, digest, normalized


class SemanticMerger:
    def __init__(self, runtime, evidence=None, books=None):
        self.runtime = runtime
        self.vectors = {}
        self.evidence = evidence or {}
        self.books = books or {}
        self.decisions = {}
        self.lock = threading.RLock()

    def _encode(self, texts):
        missing = list(dict.fromkeys(text for text in texts if text not in self.vectors))
        batches = [missing[i:i + 256] for i in range(0, len(missing), 256)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            for batch, vectors in zip(batches, pool.map(self.runtime.embed, batches)):
                self.vectors.update(zip(batch, vectors))

    @staticmethod
    def _blocked(snapshot, relations):
        rows = [claim['content'] for claim in snapshot['claims'].values() if claim['status'] == 'active']
        return {frozenset((row['head'], row['tail'])) for row in rows + relations
                if row.get('relation') in {'is_a', 'part_of', 'consists_of', 'compared_with',
                                            'implemented_by', 'derived_from'}}

    @staticmethod
    def _names(node):
        return {normalized(name) for name in [node['name']] + node.get('aliases', [])}

    @staticmethod
    def _distinct(a, b):
        for left, right in (('向左', '向右'), ('输入', '输出'), ('行地址', '列地址'),
                            ('轻载', '重载'), ('串/并', '并/串'), ('数/模', '模/数')):
            if (left in a and right in b) or (right in a and left in b):
                return True
        if set(re.findall(r'\d+', a)) != set(re.findall(r'\d+', b)):
            return True
        return False

    def match(self, concepts, snapshot, relations=None, judge=True):
        existing = list(snapshot['nodes'].values())
        return self._match(existing, concepts, self._blocked(snapshot, relations or []), judge)

    def deduplicate(self, snapshot):
        nodes = sorted(snapshot['nodes'].values(), key=lambda node: (
            node['entity_type'] == 'Concept', -len(node.get('definition_evidence_ids', [])),
            -len(node.get('mention_evidence_ids', [])), node['id']))
        return self._match([], nodes, self._blocked(snapshot, []))

    def reconcile(self, snapshot, node_ids, judge=True):
        selected = set(node_ids) & set(snapshot['nodes'])
        existing = [n for nid, n in snapshot['nodes'].items() if nid not in selected]
        queries = sorted((snapshot['nodes'][nid] for nid in selected), key=lambda n: (
            n['entity_type'] == 'Concept', -len(n.get('definition_evidence_ids', [])), n['id']))
        return self._match(existing, queries, self._blocked(snapshot, []), judge)

    @staticmethod
    def name_qualifiers(audit):
        return {row[key]: row[field].strip() for row in audit
                for key, field in (('source_id', 'source_qualifier'), ('canonical_id', 'canonical_qualifier'))
                if row.get(field, '').strip() and row['decision'] != 'merge'}

    def _pair(self, a, b):
        def description(node):
            definitions = node.get('definitions', [])[-3:]
            ids = list(dict.fromkeys(node.get('definition_evidence_ids', [])[:1] +
                       [eid for row in definitions for eid in row.get('evidence_ids', [])[:1]] +
                       node.get('evidence_ids', [])[:1]))[:4]
            def excerpt(row):
                result = {key: row[key] for key in ('id', 'text', 'book_id', 'title_path') if key in row}
                result['book_title'] = self.books.get(row.get('book_id'), {}).get('title', '')
                text = row.get('text', '')
                anchor = (node.get('definition') or '')[:12]
                start = max(0, text.find(anchor) - 200) if anchor else 0
                result['text'] = text[start:start + 900]
                return result
            return dict(name=node['name'], entity_type=node['entity_type'], aliases=node.get('aliases', [])[:8],
                        definition=node.get('definition'), scope=node.get('scope', ''),
                        definition_variants=[dict(value=row['value'], evidence_ids=[eid for eid in row.get('evidence_ids', []) if eid in ids], conditions=row.get('conditions', ''))
                                             for row in definitions],
                        scope_variants=node.get('scope_variants', [])[:4], evidence=[excerpt(row) for eid in ids if (row := self.evidence.get(eid))])
        pair = dict(left=description(a), right=description(b))
        return dict(pair_id=digest(pair), **pair)

    def _judge(self, pairs):
        pending = {}
        for pair in pairs:
            key = pair['pair_id']
            with self.lock:
                decision = self.decisions.get(key)
            if decision is None and hasattr(self.runtime, 'semantic_decision'):
                decision = self.runtime.semantic_decision(key)
            if decision is not None:
                with self.lock:
                    self.decisions[key] = decision
            else:
                pending[key] = pair
        batches = [list(pending.values())[i:i + 16] for i in range(0, len(pending), 16)]

        def judge(batch):
            expected = {pair['pair_id']: pair for pair in batch}
            def validate(result):
                rows = result.get('decisions')
                if not isinstance(rows, dict) or set(rows) != set(expected):
                    raise ValueError('Return one decision for each candidate pair')
                for pair_id, row in rows.items():
                    if row.get('decision') not in {'merge', 'separate', 'unresolved'}:
                        raise ValueError('Invalid semantic decision')
                    if not isinstance(row.get('reason'), str) or not row['reason'].strip():
                        raise ValueError('Decision needs a reason')
                    for field in ('left_qualifier', 'right_qualifier'):
                        if not isinstance(row.get(field, ''), str) or len(row.get(field, '')) > 60:
                            raise ValueError('Meaning qualifier must be a short string')
                    pair = expected[pair_id]
                    supplied = {e['id'] for side in ('left', 'right') for e in pair[side]['evidence']}
                    evidence_ids = row.get('evidence_ids')
                    if not isinstance(evidence_ids, list) or not all(isinstance(e, str) for e in evidence_ids):
                        raise ValueError('Decision evidence must be an array of IDs')
                    if not set(evidence_ids) <= supplied:
                        raise ValueError('Decision cites evidence not supplied for this pair')
            try:
                result = self.runtime.call('semantic_identity', dict(pairs=batch), validate)
            except OutputLimit:
                if len(batch) == 1:
                    raise
                # 仅拆分超限的候选批次，每个子批次仍由模型判定并保存。
                middle = len(batch) // 2
                judge(batch[:middle])
                judge(batch[middle:])
                return
            with self.lock:
                decisions = {pair_id: dict(fields, pair_id=pair_id)
                             for pair_id, fields in result['decisions'].items()}
                if hasattr(self.runtime, 'save_semantic_decisions'):
                    self.runtime.save_semantic_decisions(decisions)
                elif hasattr(self.runtime, 'save_semantic_decision'):
                    for pair_id, row in decisions.items():
                        self.runtime.save_semantic_decision(pair_id, row)
                self.decisions.update(decisions)
        with ThreadPoolExecutor(max_workers=int(getattr(self.runtime, 'config', {}).get('workers', 4))) as pool:
            list(pool.map(judge, batches))

    def _recall(self, names, definitions, definition_texts, first_query):
        by_name = {}
        for j, aliases in enumerate(names):
            for alias in aliases:
                by_name.setdefault(alias, []).append(j)
        empty_vector = np.zeros(1024, dtype=np.float32)
        for first in range(first_query, len(names), 32):
            stop = min(first + 32, len(names))
            scores = [np.empty(i, dtype=np.float32) for i in range(first, stop)]
            query_vectors = [self.vectors[definition_texts[i]] if definitions[i] else empty_vector
                             for i in range(first, stop)]
            # 每块参考向量只组装一次，仍沿用原来的逐查询点积与比较范围。
            for start in range(0, stop - 1, 1024):
                end = min(start + 1024, stop - 1)
                block = np.asarray([self.vectors[definition_texts[j]] if definitions[j] else empty_vector
                                    for j in range(start, end)], dtype=np.float32)
                for row, i in enumerate(range(first, stop)):
                    size = min(end, i) - start
                    if size > 0:
                        scores[row][start:start + size] = block[:size] @ query_vectors[row]
                del block
            for row, i in enumerate(range(first, stop)):
                connected = np.zeros(i, dtype=bool)
                for alias in names[i]:
                    for j in by_name[alias]:
                        if j >= i:
                            break
                        connected[j] = True
                yield i, scores[row], connected

    def _match(self, existing, queries, blocked, judge=True):
        if not queries:
            return {}, []
        nodes = existing + queries
        ids = [node.get('query_id', node.get('id')) for node in nodes]
        names = [self._names(node) for node in nodes]
        name_texts = ['名称：' + normalized(node['name']) for node in nodes]
        definitions = [normalized(node.get('definition') or '') if node.get('definition_status') != 'partial'
                       else '' for node in nodes]
        definition_texts = ['定义：' + value for value in definitions]
        self._encode(name_texts + [text for text, value in zip(definition_texts, definitions) if value])
        candidates_by_query, pairs = {}, {}
        for i, scores, connected in self._recall(names, definitions, definition_texts, len(existing)):
            positions = np.flatnonzero((scores >= 0.8 - 1e-7) | connected)
            positions = sorted(positions, key=lambda j: (-float(scores[j]), ids[j]))
            candidates_by_query[i] = []
            for j in positions:
                a, b = nodes[i], nodes[j]
                types = {a['entity_type'], b['entity_type']}
                if frozenset((ids[i], ids[j])) in blocked or self._distinct(a.get('unqualified_name', a['name']), b.get('unqualified_name', b['name'])):
                    continue
                if len(types) > 1 and ('Concept' not in types or 'Metric' in types):
                    continue
                ds = float(scores[j])
                ns = float(self.vectors[name_texts[i]] @ self.vectors[name_texts[j]])
                full = (a.get('definition_status') != 'partial' and b.get('definition_status') != 'partial'
                        and bool(definitions[i]) and bool(definitions[j]))
                compatible_scope = (not a.get('scope') or not b.get('scope') or
                                    normalized(a['scope']) == normalized(b['scope']))
                symbols_a = set(re.findall(r'[A-Za-z]+\d*[A-Za-z]*', a['name']))
                symbols_b = set(re.findall(r'[A-Za-z]+\d*[A-Za-z]*', b['name']))
                distinct_symbols = bool(symbols_a and symbols_b and symbols_a != symbols_b and not connected[j])
                certain = (full and compatible_scope and not distinct_symbols and ds >= 0.96
                           and (connected[j] or ns >= 0.96))
                candidates_by_query[i].append((j, ds, ns, certain, None))
            candidates = candidates_by_query[i]
            automatic = [item for item in candidates if item[3]]
            candidates_by_query[i] = [(j, ds, ns, certain, None if certain else self._pair(nodes[i], nodes[j]))
                                      for j, ds, ns, certain, _ in (automatic or candidates[:8])]
            for _, _, _, _, pair in candidates_by_query[i]:
                if pair:
                    pairs[pair['pair_id']] = pair
        if judge:
            self._judge(pairs.values())
        mapping, audit = {}, []
        eligible = set(range(len(existing)))
        members = {identifier: {identifier} for identifier in ids}
        existing_names = {}
        for identifier, aliases in zip(ids[:len(existing)], names[:len(existing)]):
            for alias in aliases:
                existing_names.setdefault(alias, set()).add(identifier)
        for i in range(len(existing), len(nodes)):
            matched = False
            for j, ds, ns, certain, pair in candidates_by_query[i]:
                if j not in eligible:
                    continue
                origins = {ids[i]} | {identifier for alias in names[i] for identifier in existing_names.get(alias, ())}
                if any(frozenset((source, target)) in blocked for source in origins for target in members[ids[j]]):
                    continue
                decision = self.decisions.get(pair['pair_id']) if pair else None
                verdict = 'merge' if certain else decision['decision'] if decision else 'unresolved'
                a, b = nodes[i], nodes[j]
                audit.append(dict(source_id=ids[i], canonical_id=ids[j], source_name=a['name'],
                                  canonical_name=b['name'], source_scope=a.get('scope', ''),
                                  canonical_scope=b.get('scope', ''), source_definition=a.get('definition'),
                                  canonical_definition=b.get('definition'), name_similarity=round(ns, 6),
                                  definition_similarity=round(ds, 6), decision=verdict,
                                  method='bge_m3' if certain else 'flash' if decision else 'pending_flash',
                                  reason=decision['reason'] if decision else '',
                                  evidence_ids=decision.get('evidence_ids', []) if decision else [],
                                  source_qualifier=decision.get('left_qualifier', '') if decision else '',
                                  canonical_qualifier=decision.get('right_qualifier', '') if decision else ''))
                if verdict == 'merge':
                    mapping[ids[i]] = ids[j]
                    members[ids[j]].add(ids[i])
                    matched = True
                    break
            if not matched:
                eligible.add(i)
        return mapping, audit
