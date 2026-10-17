from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml

from domain_ckg_workspace.ckg_incremental.engine import IncrementalEngine
from domain_ckg_workspace.ckg_incremental.runtime import ModelRuntime


ROOT = Path(__file__).resolve().parents[1]


class ExtractionRuntime:
    fingerprint = 'single-pass-test'

    def __init__(self):
        self.calls = []
        self.payloads = []
        self.edit = lambda result: None

    def embed(self, texts):
        vectors = np.zeros((len(texts), 1024), dtype=np.float32)
        for row, text in zip(vectors, texts):
            for i in range(max(1, len(text) - 1)):
                index = int.from_bytes(hashlib.sha256(text[i:i + 2].encode()).digest()[:4], 'little') % 1024
                row[index] += 1
            row /= np.linalg.norm(row)
        return vectors

    def call(self, stage, payload, validator):
        self.calls.append(stage)
        self.payloads.append(copy.deepcopy(payload))
        if stage == 'semantic_identity':
            result = dict(decisions={p['pair_id']: dict(
                          decision='separate' if 'SQL' in str(p) else 'merge',
                          reason='测试依据定义判断', evidence_ids=[])
                          for p in payload['pairs']})
            validator(result)
            return result
        if stage != 'extract_graph':
            raise AssertionError('Unexpected model stage: ' + stage)
        evidence_id = payload['source_evidence'][0]['id']
        concepts = []
        for identifier, name, definition in (
            ('q1', '缓存', '保存近期数据的存储区'),
            ('q2', '存储系统', '保存和读取数据的系统'),
        ):
            concepts.append(dict(query_id=identifier, name=name, name_status='source_term',
                                 entity_type='Structure', aliases=[], scope='', definition=definition,
                                 definition_status='explicit', evidence_ids=[evidence_id],
                                 definition_evidence_ids=[evidence_id]))
        result = dict(request_id=payload['request_id'], graph_version=payload['graph_version'],
                      concepts=concepts, relations=[dict(head='q1', tail='q2', relation='part_of',
                                                        conditions='', evidence_ids=[evidence_id])],
                      revisions=[])
        self.edit(result)
        validator(result)
        return copy.deepcopy(result)


class SinglePassGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ExtractionRuntime()
        evidence = [dict(id='ev1', book_id='b', section_id='s', text='缓存是保存近期数据的存储区，是存储系统的一部分。'),
                    dict(id='ev2', book_id='b', section_id='s', text='存储系统保存和读取数据，其中包含缓存。')]
        self.chunks = [dict(id='c' + str(i), book_id='b', section_id='s', title_path=['存储'],
                            evidence_ids=[row['id']]) for i, row in enumerate(evidence)]
        corpus = dict(fingerprint='source', books=[dict(id='b', title='教材')], evidence=evidence,
                      chunks=self.chunks)
        schema = dict(entity_types={'Structure': '结构'}, relation_types={'part_of': '部分到整体'})
        self.engine = IncrementalEngine(corpus, self.tmp.name, schema,
                                        dict(domain='计算机', workers=16, llm={'model': 'test'},
                                             embedding={'model': 'test'}), self.runtime)
        self.addCleanup(self.engine.store.close)
        self.engine.index.refresh(self.engine._snapshot())

    def test_extracts_entities_and_relations_in_one_call(self):
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        graph = self.engine._snapshot()
        self.assertEqual(self.runtime.calls, ['extract_graph'])
        self.assertEqual(len(graph['nodes']), 2)
        relations = [c for c in graph['claims'].values() if c['content']['kind'] == 'relation']
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['evidence_ids'], ['ev1'])

    def test_concurrent_fragments_merge_on_commit_without_model_replay(self):
        initial = self.engine._snapshot()
        plans = [self.engine.analyze(chunk, initial) for chunk in self.chunks]
        for plan in plans:
            self.engine._commit(plan)
        graph = self.engine._snapshot()
        self.assertEqual(self.runtime.calls, ['extract_graph', 'extract_graph'])
        self.assertEqual(len(graph['nodes']), 2)
        self.assertTrue(all(set(n['definition_evidence_ids']) == {'ev1', 'ev2'} for n in graph['nodes'].values()))
        relations = [c for c in graph['claims'].values() if c['content']['kind'] == 'relation']
        self.assertEqual(len(relations), 1)
        self.assertEqual(set(relations[0]['evidence_ids']), {'ev1', 'ev2'})

    def test_prompt_omits_repeated_metadata_without_changing_source(self):
        row = self.engine.source.evidence['ev1']
        row.update(title_path=['教材', '存储系统'], page_start=12,
                   paragraph_boundary_status='ocr_block', source_id='original-paragraph')
        self.engine.analyze(self.chunks[0], self.engine._snapshot())
        supplied = self.runtime.payloads[-1]['source_evidence'][0]
        self.assertEqual(supplied, dict(id='e1', text=row['text'], role='primary'))
        self.assertEqual(self.runtime.payloads[-1]['source_batch']['title_path'], ['存储'])
        self.assertEqual(self.engine.source.evidence['ev1']['source_id'], 'original-paragraph')

    def test_prior_omits_relations_outside_graph_schema_and_keeps_other_knowledge(self):
        prior = dict(concepts=[dict(id='g1', name='缓存', entity_type='Structure', aliases=[],
                                    definition='保存近期数据的存储区', scope='', evidence_ids=[]),
                               dict(id='g2', name='存储系统', entity_type='Structure', aliases=[],
                                    definition='保存和读取数据的系统', scope='', evidence_ids=[])],
                     relations=[dict(id='r1', head='g1', relation='part_of', tail='g1', conditions='', evidence_ids=[]),
                                dict(id='r2', head='g1', relation='part_of', tail='g2', conditions='', evidence_ids=[]),
                                dict(id='r3', head='g1', relation='manages', tail='g2', conditions='', evidence_ids=[]),
                                dict(id='r4', head='g1', relation='instance_of', tail='g2', conditions='', evidence_ids=[])])
        def call(stage, payload, validator):
            self.runtime.calls.append(stage)
            self.assertNotIn('source_evidence', payload)
            response = copy.deepcopy(prior)
            validator(response)
            return response
        self.runtime.call = call
        self.engine.seed()
        graph = self.engine._snapshot()
        relations = [c for c in graph['claims'].values() if c['content']['kind'] == 'relation']
        self.assertEqual(len(graph['nodes']), 2)
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['evidence_ids'], [])
        self.assertEqual(self.runtime.calls, ['prior'])
        omitted = json.loads((Path(self.tmp.name) / 'g0_omitted_relations.json').read_text('utf-8'))
        self.assertEqual([item['relation'] for item in omitted],
                         [prior['relations'][i] for i in (0, 2, 3)])
        self.assertEqual(len(prior['relations']), 4)

    def test_identical_objects_and_self_relation_are_removed_without_another_call(self):
        def edit(result):
            result['concepts'].append(copy.deepcopy(result['concepts'][0]))
            result['relations'].append(dict(result['relations'][0], tail='q1'))
        self.runtime.edit = edit
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        self.assertEqual(self.runtime.calls, ['extract_graph'])
        self.assertEqual(plan['normalization'], dict(duplicate_concepts=1, self_relations=1, unsupported_definitions=0))
        self.assertEqual(len(self.engine._snapshot()['nodes']), 2)
        self.assertEqual(len(plan['extracted']['relations']), 1)

    def test_unsupported_definition_keeps_grounded_mention(self):
        self.runtime.edit = lambda result: result['concepts'][1].update(definition_evidence_ids=[])
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        node = next(n for n in self.engine._snapshot()['nodes'].values() if n['name'] == '存储系统')
        self.assertIsNone(node['definition'])
        self.assertEqual(node['mention_evidence_ids'], ['ev1'])
        self.assertEqual(plan['normalization']['unsupported_definitions'], 1)
        self.assertEqual(self.runtime.calls, ['extract_graph'])

    def test_mention_status_discards_definition_fields_without_another_call(self):
        self.runtime.edit = lambda result: result['concepts'][1].update(definition_status='not_provided')
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        node = next(n for n in self.engine._snapshot()['nodes'].values() if n['name'] == '存储系统')
        self.assertIsNone(node['definition'])
        self.assertEqual(node['mention_evidence_ids'], ['ev1'])
        self.assertEqual(plan['normalization']['unsupported_definitions'], 1)
        self.assertEqual(self.runtime.calls, ['extract_graph'])

    def test_dangling_relation_is_recorded_and_valid_fragment_commits(self):
        self.runtime.edit = lambda result: result['relations'].append(dict(result['relations'][0], tail='missing'))
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        self.assertEqual(len(self.engine._snapshot()['nodes']), 2)
        self.assertEqual(len(plan['extracted']['relations']), 1)
        self.assertEqual(plan['normalization']['unknown_endpoint_relations'][0]['tail'], 'missing')
        self.assertEqual(self.runtime.calls, ['extract_graph'])

    def test_successful_resume_does_not_count_previous_failure_as_skipped(self):
        self.runtime.stats = lambda: {}
        self.engine.seed = lambda: None
        self.engine.compatible_fingerprints.add('before_format_fix')
        self.engine.store.record_chunk('c0', 'before_format_fix', 'failed', {'error': 'format'})
        self.engine.run()
        state = json.loads((Path(self.tmp.name) / 'run_state.json').read_text('utf-8'))
        self.assertEqual(state['phase'], 'complete')
        self.assertEqual(state['completed'], 2)
        self.assertEqual(state['skipped'], 0)
        self.assertEqual(self.engine.store.chunk_state('c0')['fingerprint'], 'before_format_fix')

    def test_failed_source_batch_resumes_with_original_checkpoint_fingerprints(self):
        self.runtime.stats = lambda: {}
        self.engine.seed = lambda: None
        self.engine.compatible_fingerprints.add('before_format_fix')
        for chunk in self.chunks:
            chunk.update(part_count=1, chapter_id='ch', core_chars=100, section_segment_id='seg')
            self.engine.store.record_chunk(chunk['id'], 'before_format_fix', 'failed', {'error': 'format'})
        self.engine.run()
        state = json.loads((Path(self.tmp.name) / 'run_state.json').read_text('utf-8'))
        self.assertEqual((state['completed'], state['skipped']), (2, 0))
        self.assertEqual(self.runtime.calls, ['extract_graph'])
        self.assertTrue(all(self.engine.store.chunk_state(c['id'])['fingerprint'] == 'before_format_fix'
                            for c in self.chunks))

    def test_resume_extracts_pending_sources_before_global_deduplication(self):
        self.runtime.stats = lambda: {}
        self.engine.seed = lambda: None
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        self.runtime.calls.clear()
        original = self.engine.semantic.deduplicate
        seen = []

        def deduplicate(snapshot):
            seen.append([self.engine.store.chunk_state(c['id'])['status'] for c in self.chunks])
            return original(snapshot)

        self.engine.semantic.deduplicate = deduplicate
        self.engine.run()
        self.assertEqual(seen, [])
        self.assertEqual(self.runtime.calls.count('extract_graph'), 1)

    def test_normal_fragment_completes_related_prior_without_narrowing_extraction(self):
        self.engine.store.seed_prior(dict(concepts=[
            dict(id='g1', name='缓存', entity_type='Structure', aliases=[], scope='',
                 definition='保存近期数据的存储区', evidence_ids=[]),
            dict(id='g2', name='存储系统', entity_type='Structure', aliases=[], scope='',
                 definition='保存和读取数据的系统', evidence_ids=[])],
            relations=[dict(id='r1', head='g1', tail='g2', relation='part_of', conditions='', evidence_ids=[])]), self.engine.schema)
        snapshot = self.engine._snapshot()
        self.engine.index.refresh(snapshot)
        plan = self.engine.analyze(self.chunks[0], snapshot)
        payload = next(p for stage, p in zip(self.runtime.calls, self.runtime.payloads) if stage == 'extract_graph')
        self.assertTrue(payload['target_claim_ids'])
        self.assertIn('完整抽取当前教材', payload['task'])
        self.assertNotIn('本次只抽取', payload['task'])
        self.engine._commit(plan)
        definitions = [c for c in self.engine._snapshot()['claims'].values() if
                       c['status'] == 'active' and c['content'].get('field') == 'definition']
        self.assertTrue(all('ev1' in c['evidence_ids'] for c in definitions))

    def test_online_merge_rebases_against_new_relation_before_commit(self):
        with patch.object(self.engine.semantic, 'match', return_value=({}, [])):
            for chunk in self.chunks:
                self.engine._commit(self.engine.analyze(chunk, self.engine._snapshot()))
        self.assertEqual(len(self.engine._snapshot()['nodes']), 4)
        self.engine._start_maintenance(self.engine._snapshot())
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.engine._maintain(pool)
            self.engine._maintenance_future.result(timeout=10)
            snapshot = self.engine._snapshot()
            caches = [n for n in snapshot['nodes'].values() if n['name'].startswith('缓存')]
            allowed = [dict(n, lifecycle='existing', identity_status='resolved') for n in caches]
            proposal = dict(request_id='new-hierarchy', base_graph_version=snapshot['version'], changes=[dict(
                proposal_id='p', group_id='g', operation='add_claim', target_id=None,
                payload=dict(claim=dict(kind='relation', head=caches[0]['id'], tail=caches[1]['id'], relation='part_of', conditions='')),
                evidence_ids=['ev1'], compared_evidence_ids=[], reason='教材区分两个概念')], follow_up=[])
            self.engine.store.commit(proposal, allowed, ['ev1'], [], self.engine.schema)
            self.engine._maintain(pool)
        nodes = list(self.engine._snapshot()['nodes'].values())
        self.assertEqual(sum(n['name'].startswith('缓存') for n in nodes), 2)
        self.assertEqual(sum(n['name'].startswith('存储系统') for n in nodes), 1)
        self.assertEqual(self.engine._maintenance_state['completed_batches'], 1)
        self.assertEqual(self.engine._maintenance_state['merged_nodes'], 1)
        self.assertTrue((Path(self.tmp.name) / 'decisions/online_merge_1.json').exists())

    def test_nonmetric_evaluation_is_recorded_without_discarding_fragment(self):
        self.engine.schema['relation_types']['evaluated_by'] = '对象到指标'
        self.runtime.edit = lambda result: result['relations'].append(dict(head='q1', relation='evaluated_by',
                                                                         tail='q2', conditions='', evidence_ids=['e1']))
        plan = self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.engine._commit(plan)
        decision = json.loads((Path(self.tmp.name) / 'decisions/c0.json').read_text('utf-8'))
        self.assertEqual(len(decision['omitted_relations']), 1)
        self.assertEqual(len(self.engine._snapshot()['nodes']), 2)
        self.assertEqual(self.engine.store.chunk_state('c0')['status'], 'complete')
        self.assertEqual(self.runtime.calls, ['extract_graph'])

    def test_duplicate_mention_keeps_later_definition_and_relation_endpoints(self):
        def edit(result):
            defined = copy.deepcopy(result['concepts'][0])
            defined['query_id'] = 'q3'
            result['concepts'].append(defined)
            result['concepts'][0].update(definition=None, definition_status='not_provided', definition_evidence_ids=[])
            result['relations'][0]['head'] = 'q3'
        self.runtime.edit = edit
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        graph = self.engine._snapshot()
        cache = next(node for node in graph['nodes'].values() if node['name'] == '缓存')
        self.assertEqual(len(graph['nodes']), 2)
        self.assertEqual(cache['definition'], '保存近期数据的存储区')
        self.assertEqual(cache['definition_evidence_ids'], ['ev1'])
        relation = next(claim for claim in graph['claims'].values() if claim['content']['kind'] == 'relation')
        self.assertEqual(relation['content']['head'], cache['id'])

    def test_additional_definition_is_preserved_without_overwriting_supported_definition(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        def edit(result):
            result['concepts'][0]['definition'] = '用于加速近期数据访问的存储区'
        self.runtime.edit = edit
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        cache = next(node for node in self.engine._snapshot()['nodes'].values() if node['name'] == '缓存')
        self.assertEqual(cache['definition'], '保存近期数据的存储区')
        self.assertIn('用于加速近期数据访问的存储区', cache['definition_details'])
        self.assertEqual(self.runtime.calls, ['extract_graph', 'extract_graph', 'semantic_identity'])

    def test_semantic_reuse_accumulates_aliases_and_updates_relation_endpoints(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        def edit(result):
            result['concepts'][0].update(name='高速缓冲存储器', aliases=['缓存'], scope='计算机硬件')
        self.runtime.edit = edit
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        graph = self.engine._snapshot()
        cache = next(n for n in graph['nodes'].values() if n['name'] == '缓存')
        self.assertEqual(len(graph['nodes']), 2)
        self.assertIn('高速缓冲存储器', cache['aliases'])
        self.assertIn('计算机硬件', cache['scope_variants'])
        self.assertEqual(set(cache['definition_evidence_ids']), {'ev1', 'ev2'})
        relations = [c for c in graph['claims'].values() if c['status'] == 'active' and c['content']['kind'] == 'relation']
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['content']['head'], cache['id'])
        self.assertEqual(set(relations[0]['evidence_ids']), {'ev1', 'ev2'})
        self.assertEqual(self.runtime.calls, ['extract_graph', 'extract_graph'])

    def test_multiple_scope_variants_on_semantic_node_do_not_conflict(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        def edit(result):
            result['concepts'][0].update(scope='计算机硬件')
            another = copy.deepcopy(result['concepts'][0])
            another.update(query_id='q3', scope='计算机体系结构')
            result['concepts'].append(another)
        self.runtime.edit = edit
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        cache = next(n for n in self.engine._snapshot()['nodes'].values() if n['name'] == '缓存')
        self.assertEqual(cache['scope_variants'], ['计算机体系结构', '计算机硬件'])
        self.assertEqual(len(self.engine._snapshot()['nodes']), 2)

    def test_same_name_with_distinct_definition_and_scope_remains_separate(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        self.runtime.edit = lambda result: result['concepts'][0].update(scope='数据库',
                                 definition='保存数据库查询结果以便 SQL 查询复用的缓存机制。')
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        cache_nodes = [n for n in self.engine._snapshot()['nodes'].values() if '缓存' in n['aliases']]
        self.assertEqual(len(cache_nodes), 2)
        self.assertEqual(len({n['name'] for n in cache_nodes}), 2)
        self.assertEqual({n['scope'] for n in cache_nodes}, {'', '数据库'})

    def test_flash_rejection_cannot_be_overridden_by_exact_name_and_scope(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        self.runtime.edit = lambda result: result['concepts'][0].update(
            definition='保存数据库查询结果以便 SQL 查询复用的缓存机制。')
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        graph = self.engine._snapshot()
        caches = [n for n in graph['nodes'].values() if '缓存' in n['aliases']]
        self.assertEqual(len(caches), 2)
        self.assertEqual(len({n['name'] for n in graph['nodes'].values()}), len(graph['nodes']))
        second = next(n for n in caches if 'SQL' in n['definition'])
        relation = next(c for c in graph['claims'].values() if c['content'].get('head') == second['id'])
        self.assertEqual(relation['evidence_ids'], ['ev2'])

    def test_scope_whitespace_does_not_create_a_field_conflict(self):
        self.runtime.edit = lambda result: result['concepts'][0].update(scope='数据库')
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        self.runtime.edit = lambda result: result['concepts'][0].update(scope=' 数据库 ')
        self.engine._commit(self.engine.analyze(self.chunks[1], self.engine._snapshot()))
        cache_nodes = [n for n in self.engine._snapshot()['nodes'].values() if n['name'] == '缓存']
        self.assertEqual(len(cache_nodes), 1)
        self.assertEqual(cache_nodes[0]['scope'], '数据库')
        self.assertEqual(set(cache_nodes[0]['mention_evidence_ids']), {'ev1', 'ev2'})

    def test_invented_evidence_is_rejected_before_writing(self):
        self.runtime.edit = lambda result: result['relations'][0].update(evidence_ids=['invented'])
        with self.assertRaisesRegex(ValueError, 'unknown evidence IDs'):
            self.engine.analyze(self.chunks[0], self.engine._snapshot())
        self.assertEqual(self.engine._snapshot()['nodes'], {})

    def test_explicit_relation_revision_commits_without_a_review_call(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        snapshot = self.engine._snapshot()
        self.engine.index.refresh(snapshot)
        old = next(c for c in snapshot['claims'].values() if c['content']['kind'] == 'relation')
        def edit(result):
            result['relations'] = []
            result['revisions'] = [dict(claim_id=old['id'], operation='revise_claim',
                                        before=copy.deepcopy(old['content']),
                                        after=dict(old['content'], conditions='读操作'),
                                        evidence_ids=['e1'], reason='教材明确限定关系成立条件。')]
        self.runtime.edit = edit
        self.engine._commit(self.engine.analyze(self.chunks[1], snapshot))
        revised = self.engine._snapshot()['claims'][old['id']]
        self.assertEqual(revised['content']['conditions'], '读操作')
        self.assertEqual(revised['evidence_ids'], ['ev2'])
        self.assertEqual(self.runtime.calls, ['extract_graph', 'extract_graph'])
        events = self.engine.store.snapshot()['events']
        revision = next(e for e in events if e['operation'] == 'revise_claim')
        self.assertEqual(revision['before']['evidence_ids'], ['ev1'])

    def test_small_sections_share_one_request_up_to_the_chunk_limit(self):
        chunks = [dict(chunk, chapter_id='chapter', section_segment_id='segment' + str(i),
                       part_count=1, core_chars=size) for i, (chunk, size) in
                  enumerate(zip(self.chunks, (3000, 1000)))]
        units = self.engine._pending_units(chunks)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0][0]['core_chars'], 4000)
        self.assertEqual(len(units[0][0]['source_chunks']), 2)

    def test_targeted_prior_is_supplied_even_when_context_limit_is_zero(self):
        self.engine._commit(self.engine.analyze(self.chunks[0], self.engine._snapshot()))
        snapshot = self.engine._snapshot()
        target = next(c for c in snapshot['claims'].values() if c['content']['kind'] == 'relation')
        self.engine.config['context_claims'] = 0
        chunk = dict(self.chunks[1], id='backward-test', target_claim_ids=[target['id']])
        self.engine.analyze(chunk, snapshot)
        payload = self.runtime.payloads[-1]
        self.assertEqual(payload['target_claim_ids'], [target['id']])
        self.assertIn(target['id'], [c['id'] for c in payload['local_graph']['claims']])
        self.assertEqual(self.runtime.calls, ['extract_graph', 'extract_graph'])

    def test_prior_completion_attaches_evidence_once_and_preserves_unmentioned_prior(self):
        prior = dict(concepts=[dict(id='g1', name='缓存', entity_type='Structure', aliases=[],
                                    definition='保存近期数据的存储区', scope='', evidence_ids=[]),
                               dict(id='g2', name='存储系统', entity_type='Structure', aliases=[],
                                    definition='保存和读取数据的系统', scope='', evidence_ids=[]),
                               dict(id='g3', name='量子互联', entity_type='Structure', aliases=[],
                                    definition='量子互联专有结构', scope='', evidence_ids=[])],
                     relations=[dict(id='r1', head='g1', relation='part_of', tail='g2', conditions='', evidence_ids=[])])
        self.engine.store.seed_prior(prior, self.engine.schema)
        self.engine._progress = lambda *args, **kwargs: None
        self.assertEqual(self.engine.complete_prior({'b'}), 0)
        graph = self.engine._snapshot()
        relation = next(c for c in graph['claims'].values() if c['content']['kind'] == 'relation')
        self.assertTrue(relation['evidence_ids'])
        unknown = next(n for n in graph['nodes'].values() if n['name'] == '量子互联')
        self.assertEqual(unknown['mention_evidence_ids'], [])
        calls = len(self.runtime.calls)
        self.assertGreater(calls, 0)
        self.assertEqual(set(self.runtime.calls), {'extract_graph'})
        self.assertEqual(self.engine.complete_prior({'b'}), 0)
        self.assertEqual(len(self.runtime.calls), calls)

    def test_prior_completion_no_support_keeps_claim_without_invented_evidence(self):
        prior = dict(concepts=[dict(id='g1', name='缓存', entity_type='Structure', aliases=[],
                                    definition='保存近期数据的存储区', scope='', evidence_ids=[])], relations=[])
        self.engine.store.seed_prior(prior, self.engine.schema)
        self.engine._progress = lambda *args, **kwargs: None
        self.runtime.edit = lambda result: result.update(concepts=[], relations=[], revisions=[])
        self.assertEqual(self.engine.complete_prior({'b'}), 0)
        self.assertTrue(all(c['evidence_ids'] == [] for c in self.engine._snapshot()['claims'].values()))
        calls = len(self.runtime.calls)
        self.engine.complete_prior({'b'})
        self.assertEqual(len(self.runtime.calls), calls)

    def test_actual_prompt_receives_schema_and_cached_extraction_avoids_another_request(self):
        config = yaml.safe_load((ROOT / 'domain_ckg_workspace/config/concept_kg_incremental.yaml').read_text('utf-8'))
        runtime = ModelRuntime(Path(self.tmp.name) / 'runtime', ROOT / 'prompts/incremental_v5', config)
        self.addCleanup(runtime.close)
        requests = []
        def post(endpoint, body, options, key, stage):
            requests.append(stage)
            self.assertEqual(endpoint, '/chat/completions')
            self.assertEqual(body['response_format']['type'], 'json_schema')
            output_schema = body['response_format']['json_schema']['schema']['properties']
            fields = output_schema['concepts']['items']['properties']
            self.assertEqual(fields['entity_type']['enum'], ['Structure'])
            self.assertEqual(fields['evidence_ids']['items']['enum'], ['e1'])
            self.assertEqual(output_schema['relations']['items']['properties']['relation']['enum'], ['part_of'])
            self.assertEqual(output_schema['concepts']['maxItems'], 30)
            prompt = body['messages'][1]['content']
            schema = json.loads(prompt.split('SCHEMA_JSON:\n', 1)[1].split('\n\n', 1)[0])
            self.assertEqual(schema, self.engine.schema)
            task = json.loads(prompt.split('TASK_INPUT_JSON:\n', 1)[1])
            self.assertNotIn('schema', task)
            result = self.runtime.call(stage, task, lambda result: None)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
        runtime._post = post
        self.engine.runtime = runtime
        initial = self.engine._snapshot()
        plan = self.engine.analyze(self.chunks[0], initial)
        self.assertEqual(self.engine.analyze(self.chunks[0], initial)['extracted'], plan['extracted'])
        self.engine._commit(plan)
        self.assertEqual(requests, ['extract_graph'])


if __name__ == '__main__':
    unittest.main()
