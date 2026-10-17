import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from domain_ckg_workspace.ckg_incremental.neo4j_store import Neo4jProjection
from domain_ckg_workspace.ckg_incremental.store import GraphStore


class ProjectionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GraphStore(Path(self.tmp.name) / 'graph.sqlite')
        self.addCleanup(self.store.close)
        self.store._write('nodes', 'a', dict(id='a', name='缓存'))
        self.store.add_sources([dict(id='book', title='教材')],
                               [dict(id='ev', book_id='book', text='原文')])
        for version in range(1, 13):
            self.store._event(version, 'test', 'a', None, dict(id='a', name='缓存'))
        self.store._db.execute("UPDATE metadata SET value='12' WHERE key='version'")

    def test_delta_history_keeps_full_snapshot_digest_and_retries_stable(self):
        full = self.store.snapshot()
        expected = Neo4jProjection(None, 'test')._prepare(full)['digest']
        delta = self.store.snapshot(share_records=True, events_after_version=10)
        self.assertEqual([e['version'] for e in delta['events']], [11, 12])
        self.assertEqual(delta['projection_digest'], expected)
        retry = self.store.snapshot(share_records=True, events_after_version=12)
        self.assertEqual(retry['events'], [])
        self.assertEqual(retry['projection_digest'], expected)
        self.assertEqual(self.store.snapshot(), full)

    def test_export_streams_full_history_without_materializing_snapshot(self):
        expected = self.store.snapshot()
        with patch.object(self.store, 'snapshot', side_effect=AssertionError('full snapshot retained')):
            destination = self.store.export_snapshot(Path(self.tmp.name) / 'graph.json')
        self.assertEqual(json.loads(destination.read_text('utf-8')), expected)

    def test_projection_prepares_only_new_claim_revisions_with_full_digest(self):
        claim = dict(id='claim', status='active', content=dict(kind='node_fact', node_id='a',
                     field='definition', value='保存数据'), evidence_ids=['ev'], revision=1,
                     updated_version=10)
        self.store._write('claims', 'claim', claim)
        full = self.store.snapshot()
        delta = self.store.snapshot(share_records=True, events_after_version=10)
        prepared = Neo4jProjection(None, 'test')._prepare(delta)
        self.assertEqual(prepared['digest'], Neo4jProjection(None, 'test')._prepare(full)['digest'])
        self.assertEqual(len(prepared['claims']), 1)
        self.assertEqual(len(prepared['revisions']), 2)
        claim['updated_version'] = 12
        self.store._write('claims', 'claim', claim)
        prepared = Neo4jProjection(None, 'test')._prepare(
            self.store.snapshot(share_records=True, events_after_version=10))
        self.assertEqual(len(prepared['revisions']), 3)

    def test_immutable_lookup_is_bounded_and_still_rejects_changed_history(self):
        class Transaction:
            def __init__(self):
                self.calls = []

            def run(self, query, **parameters):
                self.calls.append(parameters['ids'])
                return [dict(id='old', digest='saved')] if 'old' in parameters['ids'] else []

        projection = Neo4jProjection(None, 'test', batch_size=2)
        tx = Transaction()
        rows = [dict(id=key, props=dict(payload_digest=value))
                for key, value in [('old', 'saved'), ('new1', 'one'), ('new2', 'two')]]
        self.assertEqual(projection._new_immutable_rows(tx, 'CKGRevision', rows), rows[1:])
        self.assertTrue(all(len(ids) <= 2 for ids in tx.calls))
        rows[0]['props']['payload_digest'] = 'modified'
        with self.assertRaisesRegex(ValueError, 'Immutable'):
            projection._new_immutable_rows(tx, 'CKGRevision', rows)

    def test_publish_writes_only_changed_payloads_and_preserves_membership(self):
        class Result(list):
            def single(self):
                return self[0] if self else None

            def consume(self):
                return None

        class Transaction:
            def __init__(self):
                self.calls = []

            def run(self, query, **parameters):
                self.calls.append((query, parameters))
                if 'RETURN b.projected_version AS version' in query:
                    return Result([dict(version=1, digest='previous', status='ready')])
                if 'RETURN a.id AS id' in query or 'RETURN n.id AS id' in query:
                    return Result([dict(id='same', digest='same'),
                                   dict(id='changed', digest='previous'),
                                   dict(id='removed', digest='removed')])
                return Result([])

        rows = [dict(id=key, props=dict(payload_digest=value)) for key, value in
                [('same', 'same'), ('changed', 'new'), ('added', 'new'), ('restored', 'restored')]]
        data = dict(version=2, digest='current', nodes=rows, claims=rows,
                    books=[], evidence=[], revisions=[])
        projection = Neo4jProjection(None, 'test', batch_size=2)
        tx = Transaction()
        with patch.object(projection, '_upsert') as upsert, \
             patch.object(projection, '_link_sources') as sources, \
             patch.object(projection, '_link_assertions'):
            result = projection._sync_tx(tx, data)
        for label in ('CKGConcept', 'CKGAssertion'):
            written = next(call.args[2] for call in upsert.call_args_list if call.args[1] == label)
            self.assertEqual([row['id'] for row in written], ['changed', 'added', 'restored'])
            retired = [parameters['ids'] for query, parameters in tx.calls
                       if label in query and 'SET n.in_current_snapshot=false' in query]
            self.assertEqual(retired, [['removed']])
            self.assertFalse(any(label in query and 'SET n.projected_version=$version' in query
                                 for query, parameters in tx.calls),
                             'Unchanged records must not consume publication transaction memory')
        self.assertEqual([r['id'] for r in sources.call_args.args[1]['claims']],
                         ['changed', 'added', 'restored'])
        self.assertEqual(result['claims'], 4)
        self.assertEqual(result['changed_claims'], 3)
        self.assertIn("SET b.status='ready'", tx.calls[-1][0])
        self.assertEqual(tx.calls[-1][1]['version'], 2)
        self.assertEqual(tx.calls[-1][1]['digest'], 'current')

    def test_history_payloads_are_staged_in_bounded_transactions_before_publication(self):
        rows = [dict(id=str(i), claim_id=None, props=dict(payload_digest=str(i), source_json='完整历史'))
                for i in range(5)]
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.execute_read.side_effect = lambda operation, label, values: values
        projection = Neo4jProjection(driver, 'test', batch_size=2)
        projection._stage_sources(dict(version=2, books=[], evidence=[], revisions=rows))
        history = [call for call in session.execute_write.call_args_list if call.args[1] == 'CKGRevision']
        self.assertEqual([len(call.args[2]) for call in history], [2, 2, 1])
        self.assertEqual([row for call in history for row in call.args[2]], rows)
        self.assertTrue(all(call.args[0] == projection._stage_source_batch for call in history))

    def test_publication_links_staged_history_without_rewriting_its_payload(self):
        class Result(list):
            def single(self):
                return self[0] if self else None

            def consume(self):
                pass

        class Transaction:
            def __init__(self):
                self.calls = []

            def run(self, query, **parameters):
                self.calls.append((query, parameters))
                if 'RETURN b.projected_version AS version' in query:
                    return Result([dict(version=1, digest='old', status='ready')])
                if 'CKGRevision' in query and 'RETURN n.id AS id' in query:
                    return Result([dict(id='new', digest='new')])
                return Result([])

        rows = [dict(id='old', claim_id=None, props=dict(payload_digest='old', source_json='旧历史')),
                dict(id='new', claim_id=None, props=dict(payload_digest='new', source_json='新历史'))]
        data = dict(version=2, digest='current', nodes=[], claims=[], books=[], evidence=[], revisions=rows)
        projection = Neo4jProjection(None, 'test')
        tx = Transaction()
        with patch.object(projection, '_upsert') as upsert, \
             patch.object(projection, '_link_sources') as links, \
             patch.object(projection, '_link_assertions'):
            projection._sync_tx(tx, data)
        self.assertFalse(any(call.args[1] == 'CKGRevision' for call in upsert.call_args_list))
        self.assertEqual(links.call_args.args[1]['revisions'], [rows[1]])
        marked = [params['ids'] for query, params in tx.calls
                  if 'CKGRevision' in query and 'SET n.in_current_snapshot=true' in query]
        self.assertEqual(marked, [['new']])
        self.assertIn("SET b.status='ready'", tx.calls[-1][0])


if __name__ == '__main__':
    unittest.main()
