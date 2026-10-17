from __future__ import annotations

import tempfile
import shutil
import tracemalloc
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from domain_ckg_workspace.ckg_incremental.semantic_merge import SemanticMerger
from domain_ckg_workspace.ckg_incremental.store import GraphStore
from domain_ckg_workspace.ckg_incremental.neo4j_store import Neo4jProjection
from domain_ckg_workspace.ckg_incremental.graph_merge import graph_plan
from domain_ckg_workspace.ckg_incremental.runtime import ModelRuntime, OutputLimit, digest, semantic_response_format


class Vectors:
    def __init__(self, decision='merge'):
        self.decision, self.calls = decision, []

    def call(self, stage, payload, validator):
        self.calls.append((stage, payload))
        result = dict(decisions={p['pair_id']: dict(decision=self.decision, reason='测试判定', evidence_ids=[])
                                 for p in payload['pairs']})
        validator(result)
        return result

    def embed(self, texts):
        rows = []
        for text in texts:
            vector = np.zeros(1024, dtype=np.float32)
            vector[0 if '缓存' in text or '缓冲' in text else 1] = 1
            rows.append(vector)
        return np.asarray(rows)


def node(identifier, name='缓存', definition='缓存近期数据', scope='', entity_type='Concept'):
    return dict(id=identifier, query_id=identifier, name=name, definition=definition,
                definition_status='explicit', aliases=[], scope=scope, entity_type=entity_type,
                evidence_ids=['ev1'], definition_evidence_ids=['ev1'])


class SemanticMergeTests(unittest.TestCase):
    def setUp(self):
        self.merger = SemanticMerger(Vectors())

    def test_judgements_use_runtime_capacity_and_cache_every_pair(self):
        class ConcurrentRuntime(Vectors):
            def __init__(self):
                super().__init__('separate')
                self.config = {'workers': 8}
                self.barrier = threading.Barrier(8)
                self.saved = {}

            def call(self, stage, payload, validator):
                self.barrier.wait(timeout=2)
                return super().call(stage, payload, validator)

            def save_semantic_decision(self, key, row):
                self.saved[key] = row

        runtime = ConcurrentRuntime()
        merger = SemanticMerger(runtime)
        pairs = [dict(pair_id=f'p{i}', left=dict(evidence=[]), right=dict(evidence=[]))
                 for i in range(128)]
        merger._judge(pairs)
        self.assertEqual(len(runtime.calls), 8)
        self.assertEqual(set(runtime.saved), {row['pair_id'] for row in pairs})
        self.assertEqual(runtime.saved, merger.decisions)
        merger._judge(pairs)
        self.assertEqual(len(runtime.calls), 8)

    def test_output_limit_splits_only_uncached_pairs_and_saves_every_decision(self):
        class LimitedRuntime(Vectors):
            def __init__(self):
                super().__init__('separate')
                self.saved = {'p0': dict(decision='merge', reason='已完成', evidence_ids=[])}
                self.requests = []

            def semantic_decision(self, key):
                return self.saved.get(key)

            def save_semantic_decision(self, key, decision):
                self.saved[key] = decision

            def call(self, stage, payload, validator):
                self.requests.append([p['pair_id'] for p in payload['pairs']])
                if len(payload['pairs']) > 4:
                    raise OutputLimit('测试输出超限')
                return super().call(stage, payload, validator)

        runtime = LimitedRuntime()
        merger = SemanticMerger(runtime)
        pairs = [dict(pair_id=f'p{i}', left=dict(evidence=[]), right=dict(evidence=[])) for i in range(18)]
        merger._judge(pairs)
        self.assertEqual(set(merger.decisions), {p['pair_id'] for p in pairs})
        self.assertEqual(set(runtime.saved), set(merger.decisions))
        self.assertEqual(merger.decisions['p0']['decision'], 'merge')
        self.assertTrue(all('p0' not in request for request in runtime.requests))
        successful = [p['pair_id'] for _, payload in runtime.calls for p in payload['pairs']]
        self.assertCountEqual(successful, [f'p{i}' for i in range(1, 18)])
        merger._judge(pairs)
        self.assertEqual(len(successful), sum(len(payload['pairs']) for _, payload in runtime.calls))

    def test_single_pair_output_limit_keeps_failure_visible(self):
        merger = SemanticMerger(Vectors())
        pair = dict(pair_id='p1', left=dict(evidence=[]), right=dict(evidence=[]))
        with patch.object(merger.runtime, 'call', side_effect=OutputLimit('单项超限')):
            with self.assertRaises(OutputLimit):
                merger._judge([pair])
        self.assertEqual(merger.decisions, {})

    def test_same_meaning_with_different_scope_reuses_node(self):
        existing = node('n1', scope='计算机组成')
        query = node('q1', scope='计算机硬件')
        mapping, audit = self.merger.match([query], dict(nodes={'n1': existing}, claims={}))
        self.assertEqual(mapping, {'q1': 'n1'})
        self.assertEqual(audit[0]['definition_similarity'], 1)

    def test_different_names_with_same_meaning_reuse_node(self):
        mapping, _ = self.merger.match([node('q1', name='高速缓冲存储器')],
                                       dict(nodes={'n1': node('n1')}, claims={}))
        self.assertEqual(mapping, {'q1': 'n1'})

    def test_full_graph_recall_keeps_candidate_workspace_below_twelve_megabytes(self):
        existing = [node(str(i), name='概念' + chr(0x4e00 + i), definition='完整定义')
                    for i in range(2048)]
        query = node('query', name='新概念', definition='完整定义')
        vector = np.zeros(1024, dtype=np.float32)
        vector[0] = 1
        self.merger.vectors.update({'名称：' + item['name']: vector for item in existing + [query]})
        self.merger.vectors['定义：完整定义'] = vector
        tracemalloc.start()
        try:
            mapping, _ = self.merger.match([query], dict(nodes={n['id']: n for n in existing}, claims={}))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(mapping, {'query': '0'})
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_only_selected_candidates_build_flash_evidence_payloads(self):
        existing = [node(str(i), name='概念' + chr(0x4e00 + i), scope='旧范围') for i in range(32)]
        self.merger.runtime.decision = 'separate'
        with patch.object(self.merger, '_pair', wraps=self.merger._pair) as pair:
            mapping, _ = self.merger.match([node('query', name='新概念', scope='新范围')],
                                          dict(nodes={n['id']: n for n in existing}, claims={}))
        self.assertEqual(mapping, {})
        self.assertEqual(pair.call_count, 8)

    def test_recall_finds_synonym_in_last_partial_vector_block(self):
        existing = [node(str(i), name='概念' + chr(0x4e00 + i), definition='无关定义')
                    for i in range(2050)]
        existing[-1]['definition'] = '相关定义'
        query = node('query', name='新概念', definition='相关定义')
        relevant, unrelated = np.zeros(1024, dtype=np.float32), np.zeros(1024, dtype=np.float32)
        relevant[0], unrelated[1] = 1, 1
        self.merger.vectors.update({'名称：' + item['name']: relevant for item in existing + [query]})
        self.merger.vectors.update({'定义：相关定义': relevant, '定义：无关定义': unrelated})
        mapping, _ = self.merger.match([query], dict(nodes={n['id']: n for n in existing}, claims={}))
        self.assertEqual(mapping, {'query': '2049'})

    def test_same_name_with_different_definition_stays_separate(self):
        self.merger.runtime.decision = 'separate'
        mapping, _ = self.merger.match([node('q1', definition='数据库事务的日志状态')],
                                       dict(nodes={'n1': node('n1')}, claims={}))
        self.assertEqual(mapping, {})

    def test_different_names_at_point_eight_are_judged_by_flash(self):
        for score in (0.79, 0.8, 0.901):
            with self.subTest(score=score):
                merger = SemanticMerger(Vectors())
                a = np.zeros(1024, dtype=np.float32)
                a[0] = 1
                b = np.zeros(1024, dtype=np.float32)
                b[:2] = [score, np.sqrt(1 - score ** 2)]
                merger.vectors.update({'名称：缓存': a, '名称：高速缓冲存储器': a, '定义：完整定义甲': a, '定义：完整定义乙': b})
                mapping, _ = merger.match([node('q1', name='高速缓冲存储器', definition='完整定义乙')],
                                          dict(nodes={'n1': node('n1', definition='完整定义甲')}, claims={}))
                self.assertEqual(mapping, {'q1': 'n1'} if score >= 0.8 else {})

    def test_flash_can_merge_different_names_below_automatic_threshold(self):
        a = np.zeros(1024, dtype=np.float32)
        a[0] = 1
        b = np.zeros(1024, dtype=np.float32)
        b[:2] = [0.85, np.sqrt(1 - 0.85 ** 2)]
        self.merger.vectors.update({'名称：缓存': a, '名称：高速缓冲存储器': a,
                                    '定义：完整定义甲': a, '定义：完整定义乙': b})
        mapping, _ = self.merger.match([node('q1', name='高速缓冲存储器', definition='完整定义乙')],
                                       dict(nodes={'n1': node('n1', definition='完整定义甲')}, claims={}))
        self.assertEqual(mapping, {'q1': 'n1'})
        self.assertEqual(self.merger.runtime.calls[0][0], 'semantic_identity')

    def test_general_concept_type_can_reuse_specific_type(self):
        mapping, _ = self.merger.match([node('q1')],
                                       dict(nodes={'n1': node('n1', entity_type='Structure')}, claims={}))
        self.assertEqual(mapping, {'q1': 'n1'})

    def test_metric_is_not_merged_into_other_type(self):
        mapping, _ = self.merger.match([node('q1')],
                                       dict(nodes={'n1': node('n1', entity_type='Metric')}, claims={}))
        self.assertEqual(mapping, {})

    def test_existing_hierarchy_prevents_synonym_merge(self):
        a, b = node('n1'), node('n2', name='高速缓冲存储器')
        claim = dict(status='active', content=dict(kind='relation', head='n1', tail='n2', relation='is_a'))
        mapping, _ = self.merger.deduplicate(dict(nodes={'n1': a, 'n2': b}, claims={'c1': claim}))
        self.assertEqual(mapping, {})

    def test_incremental_reconciliation_searches_full_graph_without_self_merge(self):
        snapshot = dict(nodes={key: node(key) for key in ('a', 'b', 'c')}, claims={})
        mapping, _ = self.merger.reconcile(snapshot, ['b', 'c'])
        self.assertEqual(mapping, {'b': 'a', 'c': 'a'})
        self.assertTrue(all(source != target and target not in mapping for source, target in mapping.items()))

    def test_incremental_reconciliation_preserves_live_hierarchy(self):
        snapshot = dict(nodes={key: node(key) for key in ('a', 'b')},
                        claims={'edge': dict(status='active', content=dict(kind='relation', head='a', tail='b', relation='part_of'))})
        mapping, _ = self.merger.reconcile(snapshot, ['b'])
        self.assertEqual(mapping, {})

    def test_new_fragment_hierarchy_prevents_synonym_merge(self):
        mapping, _ = self.merger.match([node('q1'), node('q2', name='高速缓冲存储器')],
                                       dict(nodes={}, claims={}),
                                       [dict(head='q1', tail='q2', relation='part_of')])
        self.assertEqual(mapping, {})

    def test_fragment_hierarchy_also_blocks_reusing_one_existing_node_twice(self):
        snapshot = dict(nodes={'n1': node('n1')}, claims={})
        mapping, _ = self.merger.match([node('q1'), node('q2', name='高速缓冲存储器')], snapshot,
                                       [dict(head='q1', tail='q2', relation='part_of')])
        self.assertEqual(mapping, {'q1': 'n1'})

    def test_different_processor_numbers_stay_separate(self):
        mapping, _ = self.merger.match([node('q1', name='8088处理器')],
                                       dict(nodes={'n1': node('n1', name='8086处理器')}, claims={}))
        self.assertEqual(mapping, {})

    def test_opposite_normalization_directions_stay_separate_despite_high_similarity(self):
        mapping, _ = self.merger.match([node('q1', name='向右规格化')],
                                       dict(nodes={'n1': node('n1', name='向左规格化')}, claims={}))
        self.assertEqual(mapping, {})

    def test_opposite_conversion_directions_stay_separate_despite_high_similarity(self):
        mapping, _ = self.merger.match([node('q1', name='数/模转换')],
                                       dict(nodes={'n1': node('n1', name='模/数转换')}, claims={}))
        self.assertEqual(mapping, {})

    def test_row_and_column_registers_stay_separate_despite_high_similarity(self):
        mapping, _ = self.merger.match([node('q1', name='列地址缓冲寄存器')],
                                       dict(nodes={'n1': node('n1', name='行地址缓冲寄存器')}, claims={}))
        self.assertEqual(mapping, {})

    def test_distinct_register_symbols_stay_separate_despite_high_similarity(self):
        self.merger.runtime.decision = 'separate'
        mapping, _ = self.merger.match([node('q1', name='中断屏蔽寄存器IMR')],
                                       dict(nodes={'n1': node('n1', name='中断服务寄存器ISR')}, claims={}))
        self.assertEqual(mapping, {})

    def test_automatic_match_avoids_flash_for_weaker_candidates(self):
        snapshot = dict(nodes={'n1': node('n1'), 'n2': node('n2', scope='数据库')}, claims={})
        mapping, _ = self.merger.match([node('q1', scope='硬件')], snapshot)
        self.assertEqual(mapping, {'q1': 'n1'})
        self.assertEqual(self.merger.runtime.calls, [])

    def test_english_synonyms_are_given_to_flash(self):
        mapping, audit = self.merger.match([node('q1', name='processor')],
                                         dict(nodes={'n1': node('n1', name='CPU')}, claims={}))
        self.assertEqual(mapping, {'q1': 'n1'})
        self.assertEqual(audit[0]['method'], 'flash')

    def test_large_same_meaning_group_uses_direct_canonical_target(self):
        nodes = {str(i): node(str(i)) for i in range(35)}
        mapping, _ = self.merger.deduplicate(dict(nodes=nodes, claims={}))
        self.assertEqual(len(mapping), 34)
        self.assertTrue(all(target not in mapping for target in mapping.values()))
        self.assertEqual(self.merger.runtime.calls, [])

    def test_same_name_different_scopes_requires_flash_even_with_identical_definitions(self):
        self.merger.runtime.decision = 'separate'
        mapping, audit = self.merger.match([node('q1', name='栈', scope='进程内存')],
                                         dict(nodes={'n1': node('n1', name='栈', scope='数据结构')}, claims={}))
        self.assertEqual(mapping, {})
        self.assertEqual(audit[0]['method'], 'flash')

    def test_commit_rebase_uses_cache_without_another_flash_request(self):
        snapshot = dict(nodes={'n1': node('n1', scope='硬件')}, claims={})
        query = node('q1', scope='教材')
        self.merger.match([query], snapshot)
        mapping, _ = self.merger.match([query], snapshot, judge=False)
        self.assertEqual(mapping, {'q1': 'n1'})
        self.assertEqual(len(self.merger.runtime.calls), 1)

    def test_uncertain_decision_does_not_merge(self):
        self.merger.runtime.decision = 'unresolved'
        mapping, audit = self.merger.match([node('q1', scope='硬件')],
                                         dict(nodes={'n1': node('n1', scope='教材')}, claims={}))
        self.assertEqual(mapping, {})
        self.assertEqual(audit[0]['decision'], 'unresolved')

    def test_flash_cannot_cite_evidence_from_outside_candidate_pair(self):
        def bad_call(stage, payload, validator):
            validator(dict(decisions={p['pair_id']: dict(decision='merge', reason='同义',
                                           evidence_ids=['invented']) for p in payload['pairs']}))
        self.merger.runtime.call = bad_call
        with self.assertRaisesRegex(ValueError, 'not supplied'):
            self.merger.match([node('q1', scope='硬件')],
                              dict(nodes={'n1': node('n1', scope='教材')}, claims={}))

    def test_schema_requires_each_pair_key_and_binds_its_own_evidence(self):
        pairs = [dict(pair_id='a', left=dict(evidence=[dict(id='ev1')]), right=dict(evidence=[])),
                 dict(pair_id='b', left=dict(evidence=[dict(id='ev2')]), right=dict(evidence=[]))]
        schema = semantic_response_format(dict(pairs=pairs))['json_schema']['schema']['properties']['decisions']
        self.assertEqual(schema['type'], 'object')
        self.assertEqual(set(schema['required']), {'a', 'b'})
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['a']['properties']['evidence_ids']['items']['enum'], ['ev1'])
        self.assertEqual(schema['properties']['b']['properties']['evidence_ids']['items']['enum'], ['ev2'])

    def test_old_array_with_duplicate_pair_id_is_rejected(self):
        def bad_call(stage, payload, validator):
            pair_id = payload['pairs'][0]['pair_id']
            validator(dict(decisions=[dict(pair_id=pair_id, decision='merge', reason='同义', evidence_ids=[])] * 2))
        self.merger.runtime.call = bad_call
        with self.assertRaisesRegex(ValueError, 'one decision'):
            self.merger.match([node('q1', scope='硬件')],
                              dict(nodes={'n1': node('n1', scope='教材')}, claims={}))

    def test_pair_includes_actual_textbook_evidence(self):
        self.merger.evidence = {'ev1': dict(id='ev1', text='缓存保存近期数据', book_id='b', title_path=['存储'])}
        self.merger.books = {'b': dict(id='b', title='计算机组成')}
        self.merger.match([node('q1', scope='硬件')],
                          dict(nodes={'n1': node('n1', scope='教材')}, claims={}))
        pair = self.merger.runtime.calls[0][1]['pairs'][0]
        self.assertEqual(pair['left']['evidence'][0]['text'], '缓存保存近期数据')
        self.assertEqual(pair['left']['evidence'][0]['book_title'], '计算机组成')
        self.assertEqual(pair['left']['evidence'][0]['title_path'], ['存储'])


class StoreSemanticMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GraphStore(Path(self.tmp.name) / 'graph.sqlite')
        self.addCleanup(self.store.close)
        self.schema = dict(entity_types={'Concept': '概念', 'Structure': '结构'},
                           relation_types={'part_of': '组成', 'used_for': '用途'})
        self.store.add_sources([dict(id='b', title='教材')],
                               [dict(id='ev1', book_id='b', text='缓存定义一'),
                                dict(id='ev2', book_id='b', text='缓存定义二')])
        prior = dict(concepts=[node('a'), node('b', name='高速缓冲存储器', definition='保存近期数据的缓存', scope='硬件'),
                               node('c', name='存储系统', definition='存储系统')],
                     relations=[dict(id='r1', head='a', tail='c', relation='part_of', conditions='读操作'),
                                dict(id='r2', head='b', tail='c', relation='part_of', conditions='读操作'),
                                dict(id='r3', head='b', tail='c', relation='part_of', conditions='写操作')])
        for n in prior['concepts']:
            n.update(evidence_ids=[], definition_evidence_ids=[])
        self.store.seed_prior(prior, self.schema)
        snapshot = self.store.snapshot(False)
        self.ids = {n['name']: i for i, n in snapshot['nodes'].items()}
        for cid, claim in snapshot['claims'].items():
            claim['evidence_ids'] = ['ev2' if claim['content'].get('node_id', claim['content'].get('head')) == self.ids['高速缓冲存储器'] else 'ev1']
            self.store._write('claims', cid, claim)
        self.store._refresh_nodes(set(snapshot['nodes']), snapshot['nodes'], self.store._rows('claims'), self.store.version)

    def test_merge_preserves_definitions_evidence_aliases_and_conditioned_relations(self):
        canonical, duplicate = self.ids['缓存'], self.ids['高速缓冲存储器']
        before = self.store.snapshot(False)
        result = self.store.merge_nodes({duplicate: canonical}, self.schema, before['version'])
        graph = self.store.snapshot()
        self.assertEqual(result['merged_nodes'], 1)
        self.assertEqual(len(graph['nodes']), 2)
        n = graph['nodes'][canonical]
        self.assertIn('高速缓冲存储器', n['aliases'])
        self.assertIn('保存近期数据的缓存', n['definition_details'])
        self.assertEqual(set(n['evidence_ids']), {'ev1', 'ev2'})
        relations = [c for c in graph['claims'].values() if c['status'] == 'active' and c['content']['kind'] == 'relation']
        self.assertEqual(len(relations), 2)
        read = next(c for c in relations if c['content']['conditions'] == '读操作')
        self.assertEqual(set(read['evidence_ids']), {'ev1', 'ev2'})
        for c in graph['claims'].values():
            self.assertNotIn(duplicate, [c['content'].get(k) for k in ('node_id', 'head', 'tail')])
        self.assertTrue(any(e['operation'] == 'merge_node' and e['before']['id'] == duplicate for e in graph['events']))
        projection = Neo4jProjection(None, 'semantic_test')
        prepared = projection._prepare(graph)
        self.assertEqual(len(prepared['nodes']), 2)
        self.assertEqual(len(prepared['claims']), len(graph['claims']))

    def test_merged_alias_and_definition_evidence_survive_later_refresh(self):
        canonical, duplicate = self.ids['缓存'], self.ids['高速缓冲存储器']
        self.store.merge_nodes({duplicate: canonical}, self.schema, self.store.version)
        nodes, claims = self.store._rows('nodes'), self.store._rows('claims')
        self.store._refresh_nodes({canonical}, nodes, claims, self.store.version)
        merged = self.store.snapshot(False)['nodes'][canonical]
        self.assertIn('高速缓冲存储器', merged['aliases'])
        alternative = next(d for d in merged['definitions'] if d['value'] == '保存近期数据的缓存')
        self.assertEqual(alternative['evidence_ids'], ['ev2'])

    def test_name_qualification_is_unique_idempotent_and_keeps_alias_history(self):
        duplicate = self.ids['高速缓冲存储器']
        nodes = self.store._rows('nodes')
        nodes[duplicate]['name'] = '缓存'
        self.store._write('nodes', duplicate, nodes[duplicate])
        result = self.store.merge_nodes({}, self.schema, self.store.version)
        graph = self.store.snapshot(False)
        self.assertEqual(result['renamed_nodes'], 2)
        self.assertEqual(len({n['name'] for n in graph['nodes'].values()}), len(graph['nodes']))
        self.assertTrue(all('缓存' in graph['nodes'][identifier]['aliases']
                            for identifier in (duplicate, self.ids['缓存'])))
        version = self.store.version
        self.store.merge_nodes({}, self.schema, version)
        self.assertEqual(self.store.version, version)
        self.store._refresh_nodes(set(graph['nodes']), graph['nodes'], self.store._rows('claims'), version)
        self.assertIn('缓存', self.store.snapshot(False)['nodes'][duplicate]['aliases'])

    def test_flash_qualifiers_are_persisted_and_survive_future_commits(self):
        duplicate, canonical = self.ids['高速缓冲存储器'], self.ids['缓存']
        nodes = self.store._rows('nodes')
        nodes[duplicate]['name'] = '缓存'
        self.store._write('nodes', duplicate, nodes[duplicate])
        self.store.merge_nodes({}, self.schema, self.store.version,
                               {duplicate: '数据库查询', canonical: '硬件存储'})
        graph = self.store.snapshot(False)
        self.assertEqual(graph['nodes'][duplicate]['name'], '缓存（数据库查询）')
        self.assertEqual(graph['nodes'][canonical]['name'], '缓存（硬件存储）')
        before = graph['version']
        self.store.merge_nodes({}, self.schema, before)
        self.assertEqual(self.store.version, before)
        self.assertEqual(self.store.snapshot(False)['nodes'][duplicate]['name'], '缓存（数据库查询）')

    def test_stale_merge_does_not_change_graph(self):
        before = self.store.snapshot(False)
        with self.assertRaisesRegex(ValueError, 'version'):
            self.store.merge_nodes({self.ids['高速缓冲存储器']: self.ids['缓存']}, self.schema, before['version'] - 1)
        self.assertEqual(self.store.snapshot(False), before)

    def test_cached_relation_with_merged_endpoint_redirects_to_canonical_node(self):
        canonical, duplicate = self.ids['缓存'], self.ids['高速缓冲存储器']
        old_nodes = list(self.store.snapshot(False)['nodes'].values())
        self.store.merge_nodes({duplicate: canonical}, self.schema, self.store.version)
        extracted = dict(concepts=[], revisions=[], relations=[dict(head=duplicate, tail=self.ids['存储系统'],
                         relation='part_of', conditions='重放片段', evidence_ids=['ev1'])])
        plan = graph_plan(extracted, dict(id='cached'), [], self.store.snapshot(False), old_nodes)
        self.store.commit(plan['proposal'], plan['allowed'], ['ev1'], [], self.schema)
        claims = self.store.snapshot(False)['claims'].values()
        replayed = next(c for c in claims if c['content'].get('conditions') == '重放片段')
        self.assertEqual(replayed['content']['head'], canonical)


class SemanticDecisionCacheTests(unittest.TestCase):
    def test_prompt_changes_invalidate_judgements_but_preserve_extraction_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / 'prompts'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'prompts/incremental_v5', prompts)
            config = dict(llm={'model': 'Flash'}, embedding={'model': 'BGE-M3'})
            runtime = ModelRuntime(root, prompts, config)
            fingerprint = runtime.fingerprint
            legacy_templates = {k: v for k, v in runtime.templates.items()
                                if k not in runtime.FILES['semantic_identity']}
            self.assertEqual(fingerprint, digest(dict(templates=legacy_templates, **config)))
            decision = dict(pair_id='pair', decision='merge', reason='同义', evidence_ids=[])
            runtime.save_semantic_decision('pair', decision)
            runtime.close()
            runtime = ModelRuntime(root, prompts, config)
            self.assertEqual(runtime.semantic_decision('pair'), decision)
            runtime.close()
            with (prompts / '07_同义概念判定_user.txt').open('a', encoding='utf-8') as stream:
                stream.write('修订判定说明')
            runtime = ModelRuntime(root, prompts, config)
            self.assertEqual(runtime.fingerprint, fingerprint)
            self.assertIsNone(runtime.semantic_decision('pair'))
            runtime.close()
