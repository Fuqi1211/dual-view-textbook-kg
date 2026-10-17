from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml

from domain_ckg_workspace.ckg_incremental.engine import IncrementalEngine
from domain_ckg_workspace.ckg_incremental.runtime import ConceptIndex, ModelRuntime
from domain_ckg_workspace.ckg_incremental.sources import SourceCorpus
from domain_ckg_workspace.ckg_incremental.store import GraphStore


ROOT = Path(__file__).resolve().parents[1]


class IncrementalPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = yaml.safe_load((ROOT / 'domain_ckg_workspace/config/concept_kg_incremental.yaml').read_text('utf-8'))
        self.runtime = ModelRuntime(self.tmp.name, ROOT / 'prompts/incremental_v5', self.config)
        self.addCleanup(self.runtime.close)
        self.sent_texts = []
        names = {}

        def post(endpoint, body, options, key, stage):
            self.assertEqual(endpoint, '/embeddings')
            self.sent_texts.extend(body['input'])
            rows = []
            for i, text in enumerate(body['input']):
                name = json.loads(text)['name']
                position = names.setdefault(name, len(names))
                vector = np.zeros(1024, dtype=np.float32)
                vector[position] = 1
                rows.append({'index': i, 'embedding': vector.tolist()})
            return {'data': rows}

        self.post = patch.object(self.runtime, '_post', side_effect=post)
        self.post.start()
        self.addCleanup(self.post.stop)

    def index(self):
        index = ConceptIndex(self.runtime)
        index.backend = 'numpy_exact'
        return index

    def node(self, name, identifier):
        return dict(id=identifier, query_id=identifier, name=name, aliases=[], entity_type='Concept',
                    definition=name, scope='', evidence_ids=['e1'])

    def test_batch_and_global_retrieval_share_concept_embeddings(self):
        index = self.index()
        index.refresh({'version': 1, 'nodes': {'old': self.node('old', 'old')}})
        self.sent_texts.clear()
        engine = IncrementalEngine.__new__(IncrementalEngine)
        engine.runtime = self.runtime
        evidence = [{'id': 'e1', 'book_id': 'b', 'text': 'source'}]
        engine.source = SourceCorpus({'evidence': evidence})
        concepts = [self.node('alpha', 'q1'), self.node('beta', 'q2')]
        kept = engine._within_chunk(copy.deepcopy(concepts), evidence, {'version': 1}, 'req')
        results = index.retrieve(kept)
        self.assertEqual(kept, concepts)
        self.assertEqual([[n['id'] for n in row] for row in results], [['old'], ['old']])
        self.assertEqual(len(self.sent_texts), 2)

    def test_same_version_refresh_reuses_index_without_encoding(self):
        index = self.index()
        snapshot = {'version': 1, 'nodes': {'old': self.node('old', 'old')}}
        index.refresh(snapshot)
        matrix = index.matrix
        with patch.object(self.runtime, 'embed', wraps=self.runtime.embed) as embed:
            index.refresh(copy.deepcopy(snapshot))
            self.assertEqual(embed.call_count, 0)
        self.assertIs(index.matrix, matrix)

    def test_encoding_new_nodes_does_not_block_current_index_reads(self):
        index = self.index()
        old = self.node('old', 'old')
        index.refresh({'version': 1, 'nodes': {'old': old}})
        index.retrieve([old])
        started, release, read_done = threading.Event(), threading.Event(), threading.Event()
        embed = self.runtime.embed

        def blocked(texts):
            if any(json.loads(text)['name'] == 'new' for text in texts):
                started.set()
                release.wait(3)
            return embed(texts)

        with patch.object(self.runtime, 'embed', side_effect=blocked), ThreadPoolExecutor(2) as pool:
            refresh = pool.submit(index.refresh, {'version': 2, 'nodes': {'old': old, 'new': self.node('new', 'new')}})
            self.assertTrue(started.wait(1))

            def read():
                rows = index.retrieve([old])
                read_done.set()
                return rows

            reader = pool.submit(read)
            try:
                readable = read_done.wait(0.5)
            finally:
                release.set()
            refresh.result(timeout=3)
            self.assertEqual(reader.result(timeout=3)[0][0]['id'], 'old')
            self.assertTrue(readable, 'Current index reads waited for unrelated embedding I/O')

    def test_changed_definition_and_alias_are_visible_after_refresh(self):
        index = self.index()
        node = self.node('old', 'old')
        index.refresh({'version': 1, 'nodes': {'old': node}})
        revised = dict(node, definition='revised', aliases=['alias'])
        index.refresh({'version': 2, 'nodes': {'old': revised}})
        self.assertEqual(index.retrieve([self.node('alias', 'q')])[0][0]['definition'], 'revised')
        index.refresh({'version': 3, 'nodes': {}})
        self.assertEqual(index.retrieve([node]), [[]])

    def test_evidence_only_update_keeps_vectors_and_exposes_new_evidence(self):
        index = self.index()
        node = self.node('old', 'old')
        index.refresh({'version': 1, 'nodes': {'old': node}})
        matrix = index.matrix
        revised = dict(node, evidence_ids=['e1', 'e2'])
        with patch.object(self.runtime, 'embed', wraps=self.runtime.embed) as embed:
            index.refresh({'version': 2, 'nodes': {'old': revised}})
            self.assertEqual(embed.call_count, 0)
        self.assertIs(index.matrix, matrix)
        self.assertEqual(index.retrieve([node])[0][0]['evidence_ids'], ['e1', 'e2'])

    def test_analysis_snapshot_is_reused_until_graph_version_changes(self):
        engine = IncrementalEngine.__new__(IncrementalEngine)
        engine._analysis_snapshot = None
        engine.store = GraphStore(Path(self.tmp.name) / 'snapshots.sqlite')
        self.addCleanup(engine.store.close)
        with patch.object(engine.store, 'snapshot', wraps=engine.store.snapshot) as snapshot:
            initial = engine._snapshot()
            self.assertEqual(engine._snapshot(), initial)
            self.assertEqual(snapshot.call_count, 1)
            engine.store.add_sources([{'id': 'book'}], [])
            current = engine._snapshot()
            self.assertGreater(current['version'], initial['version'])
            self.assertEqual(current['books']['book']['id'], 'book')
            self.assertEqual(snapshot.call_count, 2)

    def test_raw_cache_lookup_uses_key_index(self):
        plan = self.runtime.db.execute(
            "EXPLAIN QUERY PLAN SELECT status,value FROM attempts WHERE key=? "
            "AND status IN ('response','invalid_response','output_limit') ORDER BY id DESC LIMIT 1", ('missing',)
        ).fetchall()
        self.assertTrue(any('SEARCH' in row[3] and 'INDEX' in row[3] for row in plan), plan)

    def test_graph_versions_share_unchanged_records_and_keep_old_values(self):
        from unittest.mock import Mock
        graph = dict(version=1, nodes={'stable': self.node('stable', 'stable'),
                                      'changed': self.node('old', 'changed')},
                     claims={'fact': {'id': 'fact', 'evidence_ids': ['e1']}}, books={'book': {'id': 'book'}})
        engine = IncrementalEngine.__new__(IncrementalEngine)
        engine._analysis_snapshot = None
        engine.store = Mock(version=1, snapshot=lambda **kwargs: copy.deepcopy(graph))
        previous = engine._snapshot()
        graph['version'] = engine.store.version = 2
        graph['nodes']['changed']['definition'] = 'new'
        current = engine._snapshot()
        self.assertIs(current['nodes']['stable'], previous['nodes']['stable'])
        self.assertIs(current['claims']['fact'], previous['claims']['fact'])
        self.assertIs(current['books']['book'], previous['books']['book'])
        self.assertIsNot(current['nodes']['changed'], previous['nodes']['changed'])
        self.assertEqual(previous['nodes']['changed']['definition'], 'old')
        self.assertEqual(current['nodes']['changed']['definition'], 'new')

    def test_projection_digest_streams_and_preserves_existing_keys(self):
        import hashlib
        from domain_ckg_workspace.ckg_incremental import neo4j_store
        graph = {'version': 3, 'events': [{'after': {'definition': '教材原文' * 1000}}]}
        expected = hashlib.sha256(neo4j_store._json(graph).encode('utf-8')).hexdigest()
        with patch.object(neo4j_store, '_json', side_effect=AssertionError('Do not materialize the whole graph')):
            self.assertEqual(neo4j_store._digest(graph), expected)

    def test_snapshot_export_streams_complete_history(self):
        store = GraphStore(Path(self.tmp.name) / 'export.sqlite')
        self.addCleanup(store.close)
        store._event(4, 'test', 'a', None, {'definition': '教材原文' * 1000})
        store._db.execute("UPDATE metadata SET value='4' WHERE key='version'")
        graph = store.snapshot()
        destination = Path(self.tmp.name) / 'graph.json'
        with patch.object(store, 'snapshot', side_effect=AssertionError('Do not retain the whole snapshot')), patch(
                'domain_ckg_workspace.ckg_incremental.store.json.dumps',
                side_effect=AssertionError('Do not materialize the whole export')):
            store.export_snapshot(destination)
        self.assertEqual(json.loads(destination.read_text('utf-8')), graph)

    def test_refill_after_each_commit_uses_latest_graph(self):
        engine = IncrementalEngine.__new__(IncrementalEngine)
        engine._maintenance_future = None
        engine._maintenance_batch = []
        engine._maintenance_dirty = set()
        engine.root = Path(self.tmp.name)
        engine.config = {'workers': 2}
        engine.fingerprint = 'test'
        engine.compatible_fingerprints = {'test'}
        engine.store = GraphStore(engine.root / 'graph.sqlite')
        self.addCleanup(engine.store.close)
        chunks = [dict(id=f'c{i}', book_id='b', evidence_ids=['e1']) for i in range(4)]
        engine.source = SourceCorpus({'books': [{'id': 'b', 'title': 'book'}], 'chunks': chunks, 'evidence': []})
        engine.seed = lambda: None
        engine.project = lambda **kwargs: None
        engine.projection = None
        engine._progress = lambda *args, **kwargs: None
        engine._pending_units = lambda rows: [(row, [row]) for row in rows]
        engine.index = type('Index', (), {'refresh': lambda self, snapshot: None})()
        version, commits, observed = [1], [], []
        engine._snapshot = lambda: {'version': version[0], 'nodes': {}, 'claims': {}}
        refill_started = threading.Event()

        def analyze(chunk, snapshot):
            if chunk['id'] == 'c2':
                observed.append(snapshot['version'])
                refill_started.set()
            return {'chunk': chunk}

        def commit(plan):
            commits.append(plan['chunk']['id'])
            if len(commits) == 2:
                self.assertTrue(refill_started.wait(0.5), 'Worker was not refilled after the first commit')
            version[0] += 1
            engine.store.record_chunk(plan['chunk']['id'], 'test', 'complete', {'version': version[0]})

        engine.analyze, engine._commit = analyze, commit

        def completed_together(futures, **kwargs):
            return wait(futures, return_when=ALL_COMPLETED)

        with patch('domain_ckg_workspace.ckg_incremental.engine.wait', side_effect=completed_together):
            engine.run(max_chunks=4)
        self.assertEqual(observed, [2])
        self.assertEqual(len(commits), 4)


if __name__ == '__main__':
    unittest.main()
