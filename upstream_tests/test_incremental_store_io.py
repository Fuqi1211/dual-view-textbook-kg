import json
import tempfile
import unittest
from pathlib import Path

from domain_ckg_workspace.ckg_incremental.store import GraphStore


class StoreIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'graph.sqlite'
        self.store = GraphStore(self.path)
        self.addCleanup(self.store.close)
        self.store._write('nodes', 'a', {'id': 'a', 'name': '旧定义', 'aliases': []})
        self.store._write('nodes', 'b', {'id': 'b', 'name': '不变', 'aliases': []})

    def test_subsequent_snapshots_do_not_rescan_working_tables(self):
        before = self.store.snapshot(include_sources=False, share_records=True)
        statements = []
        self.store._db.set_trace_callback(statements.append)
        self.store._write('nodes', 'a', {'id': 'a', 'name': '新定义', 'aliases': ['原名']})
        after = self.store.snapshot(include_sources=False, share_records=True)
        scans = [s for s in statements if s in ('SELECT id,data FROM nodes', 'SELECT id,data FROM claims')]
        self.assertEqual(scans, [])
        self.assertEqual(before['nodes']['a']['name'], '旧定义')
        self.assertEqual(after['nodes']['a']['name'], '新定义')
        self.assertIs(before['nodes']['b'], after['nodes']['b'])

    def test_mutable_rows_and_unchanged_writes_preserve_snapshot(self):
        before = self.store.snapshot(include_sources=False)
        working = self.store._rows('nodes')
        working['a']['aliases'].append('新别名')
        self.assertEqual(before['nodes']['a']['aliases'], [])
        self.store._write('nodes', 'a', working['a'])
        working['a']['aliases'].append('未提交')
        self.assertEqual(self.store.snapshot(include_sources=False)['nodes']['a']['aliases'], ['新别名'])
        writes = self.store._db.total_changes
        self.store._write('nodes', 'b', working['b'])
        self.assertEqual(self.store._db.total_changes, writes)

    def test_rollback_and_external_commit_are_visible(self):
        self.store.snapshot(include_sources=False)
        with self.assertRaisesRegex(ValueError, 'rollback'):
            with self.store._transaction():
                self.store._write('nodes', 'a', {'id': 'a', 'name': '回滚', 'aliases': []})
                raise ValueError('rollback')
        self.assertEqual(self.store.snapshot(include_sources=False)['nodes']['a']['name'], '旧定义')
        other = GraphStore(self.path)
        try:
            other._write('nodes', 'a', {'id': 'a', 'name': '外部提交', 'aliases': []})
        finally:
            other.close()
        self.assertEqual(self.store.snapshot(include_sources=False)['nodes']['a']['name'], '外部提交')

    def test_complete_export_still_contains_history(self):
        with self.store._transaction():
            self.store._event(1, 'test', 'a', None, {'id': 'a'})
        self.store.snapshot(include_sources=False)
        destination = self.store.export_snapshot(Path(self.tmp.name) / 'graph.json')
        exported = json.loads(destination.read_text('utf-8'))
        self.assertEqual(len(exported['events']), 1)
        self.assertEqual(exported['nodes']['a']['name'], '旧定义')
