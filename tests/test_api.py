from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path


class APITests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / '.test_tmp'
        temporary_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_public_interface_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('dual_kg'), '需要独立的双视角接口包')

    def test_model_process_export_is_readable_and_read_only(self):
        import dual_kg
        self.assertTrue(hasattr(dual_kg, 'export_process'), '需要可直接查看的过程导出')
        run = self.root / 'run'
        (run / 'runtime').mkdir(parents=True)
        path = run / 'runtime/model_cache.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE attempts(id INTEGER PRIMARY KEY, key TEXT, stage TEXT, created REAL, status TEXT, value TEXT)')
            db.execute('INSERT INTO attempts VALUES (1, ?, ?, 1, ?, ?)',
                       ('r1', 'extract_graph', 'response', json.dumps({'concepts': ['缓存']})))
            db.commit()
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        report = dual_kg.export_process(run, self.root / 'process')
        rows = [json.loads(x) for x in Path(report['model_attempts']).read_text('utf-8').splitlines()]
        self.assertEqual(rows[0]['value']['concepts'], ['缓存'])
        self.assertEqual(rows[0]['stage'], 'extract_graph')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)

    def test_structure_materialization_uses_existing_builder(self):
        import dual_kg
        self.assertTrue(hasattr(dual_kg, 'build_structure'), '需要封装已有结构图构建阶段')
        from examples.make_fixture import make_fixture
        project = self.root / 'project'
        make_fixture(project)
        result = dual_kg.build_structure(project / 'data/demo_book/text_graph.json',
            self.root / 'materialized', book_id='demo_book', book_title='合成教材：存储基础')
        graph = json.loads(Path(result['hierarchy_graph']).read_text('utf-8'))
        paragraph = next(n for n in graph['nodes'] if n['id'] == 'paragraph_1')
        self.assertEqual(paragraph['parent_id'], 'section_1')
        self.assertEqual(paragraph['path_ids'], ['demo_book', 'chapter_1', 'section_1', 'paragraph_1'])
        self.assertFalse(graph['human_verified'])

    def test_structure_export_preserves_published_ids_and_bytes(self):
        from dual_kg import export_structure
        from examples.make_fixture import make_fixture
        project = self.root / 'project'
        make_fixture(project)
        result = export_structure(project, 'demo_book', self.root / 'export')
        original = project / 'data/demo_book/hierarchy_graph.json'
        exported = Path(result['hierarchy_graph'])
        self.assertEqual(original.read_bytes(), exported.read_bytes())
        self.assertEqual(result['paragraph_count'], 1)
        self.assertFalse(result['human_verified'])
        with self.assertRaises(FileExistsError):
            export_structure(project, 'demo_book', self.root / 'export')

    def test_tampered_source_is_rejected_without_output(self):
        from dual_kg import export_structure
        from examples.make_fixture import make_fixture
        project = self.root / 'project'
        make_fixture(project)
        source = project / 'data/demo_book/text_graph.json'
        source.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            export_structure(project, 'demo_book', self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_prepare_build_and_resume_preserve_evidence(self):
        from dual_kg import prepare_concepts, build_concepts, inspect_run
        from examples.make_fixture import make_fixture
        from examples.offline_runtime import OfflineRuntime
        project = self.root / 'project'
        make_fixture(project)
        prepared = prepare_concepts(project, self.root / 'prepared', book_ids=['demo_book'])
        self.assertEqual(prepared['evidence'][0]['source_id'], 'paragraph_1')
        runtime = OfflineRuntime()
        result = build_concepts(self.root / 'prepared/corpus.json', self.root / 'run', runtime=runtime)
        graph = json.loads(Path(result['graph']).read_text('utf-8'))
        self.assertEqual(len(graph['nodes']), 2)
        relation = next(c for c in graph['claims'].values() if c['content']['kind'] == 'relation')
        self.assertEqual(relation['evidence_ids'], [prepared['evidence'][0]['id']])
        self.assertTrue(list((self.root / 'run/analysis').glob('*.json')))
        self.assertTrue(list((self.root / 'run/decisions').glob('*.json')))
        before = graph['version']
        build_concepts(self.root / 'prepared/corpus.json', self.root / 'run', runtime=runtime)
        self.assertEqual(inspect_run(self.root / 'run')['state']['phase'], 'complete')
        after = json.loads((self.root / 'run/graph.json').read_text('utf-8'))['version']
        self.assertEqual(before, after)
        self.assertFalse(result['state']['database_written'])

    def test_network_runtime_requires_explicit_allow_api(self):
        from dual_kg import build_concepts
        with self.assertRaisesRegex(ValueError, 'allow_api'):
            build_concepts(self.root / 'not-read.json', self.root / 'run')
        self.assertFalse((self.root / 'run').exists())

    def test_partial_run_and_lock_are_reported(self):
        from dual_kg import build_concepts, prepare_concepts
        from dual_kg.api import _run_lock
        from examples.make_fixture import make_fixture
        from examples.offline_runtime import OfflineRuntime
        project = make_fixture(self.root / 'project')
        prepare_concepts(project, self.root / 'prepared')
        corpus = self.root / 'prepared/corpus.json'
        run = self.root / 'run'
        with _run_lock(run):
            with self.assertRaisesRegex(RuntimeError, 'run lock'):
                build_concepts(corpus, run, runtime=OfflineRuntime())
        result = build_concepts(corpus, run, max_chunks=1, runtime=OfflineRuntime())
        self.assertEqual(result['state']['phase'], 'partial')
        result = build_concepts(corpus, run, runtime=OfflineRuntime())
        self.assertEqual(result['state']['phase'], 'complete')

    def test_failed_build_retains_error_and_unlocks(self):
        from dual_kg import build_concepts, prepare_concepts, inspect_run
        from examples.make_fixture import make_fixture
        from examples.offline_runtime import OfflineRuntime
        project = self.root / 'project'
        make_fixture(project)
        prepare_concepts(project, self.root / 'prepared')
        class BrokenRuntime(OfflineRuntime):
            def call(self, stage, payload, validator):
                raise RuntimeError('offline failure')
        with self.assertRaisesRegex(RuntimeError, 'offline failure'):
            build_concepts(self.root / 'prepared/corpus.json', self.root / 'run', runtime=BrokenRuntime())
        info = inspect_run(self.root / 'run')
        self.assertEqual(info['error']['error_type'], 'RuntimeError')
        self.assertEqual(info['api_state']['phase'], 'failed')
        result = build_concepts(self.root / 'prepared/corpus.json', self.root / 'run', runtime=OfflineRuntime())
        self.assertEqual(result['api_state']['phase'], 'complete')
        self.assertIsNone(result['error'])


if __name__ == '__main__':
    unittest.main()
