from __future__ import annotations

import hashlib
import json
from pathlib import Path


def make_fixture(root):
    root = Path(root)
    def write(relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
        return hashlib.sha256(path.read_bytes()).hexdigest()
    source_hash = write('data/demo_book/text_graph.json', dict(
        schema_version='pdf-natural-paragraph-graph-v6', note='合成示例，不是生产教材构建结果',
        headings=[dict(id='chapter_1', type='chapter', label='第一章 存储系统', parent_id=None, level=1, source_row=0),
                  dict(id='section_1', type='section', label='1.1 缓存', parent_id='chapter_1', level=2, source_row=1)],
        nodes=[dict(id='paragraph_1', type='paragraph', heading_id='section_1',
            text='缓存保存近期数据，是存储系统的一部分。存储系统负责保存和读取数据。',
            assignment_basis='synthetic_fixture', source_spans=[dict(row_index=1, record_index=0, start=0)])]))
    graph = dict(book_title='合成教材：存储基础', root_id='demo_book',
        source_graph=dict(path='text_graph.json', sha256=source_hash), human_verified=False,
        nodes=[dict(id='demo_book', type='book', label='存储基础'),
               dict(id='chapter_1', type='chapter', label='第一章 存储系统', parent_id='demo_book'),
               dict(id='section_1', type='section', label='1.1 缓存', parent_id='chapter_1'),
               dict(id='paragraph_1', type='paragraph', parent_id='section_1', chapter_id='chapter_1',
                    section_id='section_1', path_ids=['demo_book', 'chapter_1', 'section_1', 'paragraph_1'],
                    text='缓存保存近期数据，是存储系统的一部分。存储系统负责保存和读取数据。',
                    page_start=1, page_end=1, boundary_status='synthetic',
                    natural_paragraph_boundary_verified=False)],
        edges=[dict(source='demo_book', target='chapter_1', type='contains'),
               dict(source='chapter_1', target='section_1', type='contains'),
               dict(source='section_1', target='paragraph_1', type='contains')])
    hierarchy_hash = write('data/demo_book/hierarchy_graph.json', graph)
    manifest_hash = write('data/manifest.json', dict(books=[dict(book_id='demo_book',
        book_title=graph['book_title'], paragraphs=1, graph_sha256=source_hash)]))
    write('data/hierarchy_manifest.json', dict(books=[dict(book_id='demo_book',
        path='demo_book/hierarchy_graph.json', sha256=hierarchy_hash)]))
    write('研究方案_20261002/24本文本图_当前版本.json', dict(version='synthetic-demo',
        data_directory='data', manifest_path='data/manifest.json', manifest_sha256=manifest_hash,
        hierarchy_manifest_path='data/hierarchy_manifest.json'))
    return root
