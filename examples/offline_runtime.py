from __future__ import annotations

import hashlib
import json

import numpy as np


class OfflineRuntime:
    """固定合成输出只用于演示接口与引擎衔接，不能用于真实抽取或论文评价。"""
    fingerprint = 'dual-kg-synthetic-demo-v1'

    def stats(self):
        return dict(mode='synthetic_demo', paid_requests=0)

    def embed(self, texts):
        vectors = np.zeros((len(texts), 1024), dtype=np.float32)
        for row, text in zip(vectors, texts):
            for i in range(max(1, len(text) - 1)):
                index = int.from_bytes(hashlib.sha256(text[i:i + 2].encode()).digest()[:4], 'little') % 1024
                row[index] += 1
            row /= max(float(np.linalg.norm(row)), 1)
        return vectors

    def call(self, stage, payload, validator):
        if stage == 'prior':
            result = dict(concepts=[dict(id='g_cache', name='缓存', aliases=[], entity_type='Structure',
                definition='', scope='', evidence_ids=[])], relations=[])
        elif stage == 'extract_graph':
            eid = payload['source_evidence'][0]['id']
            concepts = [dict(query_id=identifier, name=name, aliases=[], entity_type='Structure',
                definition=definition, definition_status='explicit', scope='', evidence_ids=[eid],
                definition_evidence_ids=[eid]) for identifier, name, definition in (
                    ('q_cache', '缓存', '保存近期数据的存储区'),
                    ('q_storage', '存储系统', '负责保存和读取数据的系统'))]
            result = dict(request_id=payload['request_id'], graph_version=payload['graph_version'],
                concepts=concepts, relations=[dict(head='q_cache', tail='q_storage', relation='part_of',
                conditions='', evidence_ids=[eid])], revisions=[])
        elif stage == 'semantic_identity':
            result = dict(decisions={p['pair_id']: dict(
                decision='merge' if p['left']['name'] == p['right']['name'] else 'separate',
                reason='合成演示固定判定，非真实语义判断', evidence_ids=[])
                for p in payload['pairs']})
        else:
            raise AssertionError('Unexpected demo stage: ' + stage)
        validator(result)
        return json.loads(json.dumps(result, ensure_ascii=False))
