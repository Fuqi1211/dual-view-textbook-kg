from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import importlib.util
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import yaml

from domain_ckg_workspace.ckg_build.source_adapter import POINTER, _within, prepare_source
from domain_ckg_workspace.ckg_incremental.engine import IncrementalEngine
from domain_ckg_workspace.ckg_incremental.runtime import ModelRuntime, digest, save
from domain_ckg_workspace.ckg_incremental.sources import prepare_corpus

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = Path(__file__).resolve().parent / 'resources'
DEFAULT_CONFIG = RESOURCES / 'config/incremental.example.yaml'
DEFAULT_SCHEMA = RESOURCES / 'config/schema.yaml'
DEFAULT_PROMPTS = RESOURCES / 'prompts/incremental_v5'
_STRUCTURE_LOCK = threading.Lock()


def _read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def build_structure(source_graph, output_dir, *, book_id, book_title):
    """调用原层级构建器，从已有物理段落图生成层级视图；不重跑 OCR 或修复。"""
    if not isinstance(book_id, str) or not book_id or Path(book_id).name != book_id or book_id in {'.', '..'}:
        raise ValueError('book_id must be a simple directory name')
    source_graph, output = Path(source_graph).resolve(), Path(output_dir).resolve()
    source_data = _read(source_graph)
    if not isinstance(source_data.get('headings'), list) or not isinstance(source_data.get('nodes'), list):
        raise ValueError('Expected an existing physical paragraph graph with headings and nodes')
    output.mkdir(parents=True, exist_ok=False)
    folder = output / book_id
    folder.mkdir()
    shutil.copyfile(source_graph, folder / 'text_graph.json')
    spec = importlib.util.spec_from_file_location('_dual_kg_hierarchy', ROOT / 'legacy_text_graph_tools/build_hierarchy_view.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 原脚本通过 OUT 寻址，每次独立加载以保持原实现和运行指纹。
    with _STRUCTURE_LOCK:
        module.OUT = output
        result = module.build(dict(book_id=book_id, book_title=book_title))
    result['hierarchy_graph'] = str(folder / 'hierarchy_graph.json')
    result['human_verified'] = False
    save(output / 'build_manifest.json', result)
    return result


@contextmanager
def _run_lock(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.incremental.lock').open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b' ')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Another builder holds this run lock') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another builder holds this run lock') from None
            yield


def export_structure(project_root, book_id, output_dir):
    """核验并原样导出已发布结构图，不重新分段或生成节点。"""
    project_root, output = Path(project_root).resolve(), Path(output_dir).resolve()
    prepared = prepare_source(project_root, book_id)
    pointer = prepared['source_manifest']['pointer']
    manifest = _read(_within(project_root, pointer['hierarchy_manifest_path']))
    entry = next(b for b in manifest['books'] if b['book_id'] == book_id)
    hierarchy = _within(project_root, Path(pointer['data_directory']) / entry['path'])
    graph = _read(hierarchy)
    source = _within(project_root, Path(pointer['data_directory']) / book_id / graph['source_graph']['path'])
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(hierarchy, output / 'hierarchy_graph.json')
    shutil.copyfile(source, output / 'text_graph.json')
    save(output / 'source_manifest.json', prepared['source_manifest'])
    result = dict(book_id=book_id, book_title=prepared['book_title'],
                  source_version=pointer['version'], paragraph_count=len(prepared['paragraphs']),
                  hierarchy_graph=str(output / 'hierarchy_graph.json'),
                  text_graph=str(output / 'text_graph.json'),
                  human_verified=prepared['source_manifest']['human_verified'],
                  hierarchy_sha256=entry['sha256'])
    save(output / 'export_manifest.json', result)
    return result


def prepare_concepts(project_root, output_dir, *, book_ids=None, config=None):
    """从已发布文本图生成证据与片段，保留来源 ID、层级、页码和字符范围。"""
    settings = yaml.safe_load(Path(config or DEFAULT_CONFIG).read_text('utf-8-sig'))
    with _run_lock(output_dir):
        return prepare_corpus(Path(project_root), Path(output_dir), book_ids,
                              settings.get('chunk_chars', 4500), settings.get('evidence_chars', 650),
                              settings.get('context_chars', 650))


def build_concepts(corpus, output_dir, *, config=None, schema=None, prompts=None,
                   allow_api=False, max_chunks=None, runtime=None, projection=None):
    """调用原增量引擎；自定义 runtime 由调用者管理，默认真实请求需显式启用。"""
    if runtime is None and not allow_api:
        raise ValueError('Real model calls require allow_api=True; offline callers may supply runtime')
    if max_chunks is not None and (isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or max_chunks < 1):
        raise ValueError('max_chunks must be a positive integer')
    bundle = _read(corpus)
    if digest({k: v for k, v in bundle.items() if k != 'fingerprint'}) != bundle['fingerprint']:
        raise ValueError('Corpus fingerprint mismatch')
    settings = yaml.safe_load(Path(config or DEFAULT_CONFIG).read_text('utf-8-sig'))
    schema_data = yaml.safe_load(Path(schema or DEFAULT_SCHEMA).read_text('utf-8-sig'))
    output = Path(output_dir).resolve()
    owned_runtime = runtime is None
    engine = None
    with _run_lock(output):
        saved_corpus = output / 'corpus.json'
        if saved_corpus.exists() and _read(saved_corpus)['fingerprint'] != bundle['fingerprint']:
            raise ValueError('Output contains a different corpus; use a new run directory')
        save(saved_corpus, bundle)
        save(output / 'api_state.json', dict(phase='running', started_unix=time.time()))
        try:
            if owned_runtime:
                runtime = ModelRuntime(output / 'runtime', Path(prompts or DEFAULT_PROMPTS), settings)
            engine = IncrementalEngine(bundle, output, schema_data, settings, runtime, projection)
            engine.run(max_chunks)
            state = _read(output / 'run_state.json')
            save(output / 'api_state.json', dict(phase=state['phase'], finished_unix=time.time()))
        except Exception as exc:
            # 外层仅记录失败状态并重新抛出；片段容错仍由原引擎处理。
            save(output / 'last_error.json', dict(error_type=type(exc).__name__))
            save(output / 'api_state.json', dict(phase='failed', finished_unix=time.time()))
            raise
        finally:
            if engine is not None:
                engine.store.close()
            if owned_runtime and runtime is not None:
                runtime.close()
    return inspect_run(output)


def inspect_run(output_dir):
    """只读查看状态和过程文件位置，构建未开始时也不创建文件。"""
    output = Path(output_dir).resolve()
    def optional(name):
        return _read(output / name) if (output / name).exists() else None
    api_state = optional('api_state.json') or dict(phase='not_started')
    result = dict(output_dir=str(output), graph=str(output / 'graph.json'),
                  state=optional('run_state.json'), api_state=api_state,
                  error=optional('last_error.json') if api_state['phase'] == 'failed' else None,
                  artifacts={name: str(output / name) for name in (
                      'corpus.json', 'g0.json', 'analysis', 'decisions', 'graph.sqlite',
                      'runtime/model_cache.sqlite', 'run_manifest.json', 'run_state.json',
                      'semantic_merge_initial.json', 'semantic_merge_final.json', 'database_state.json')
                      if (output / name).exists()})
    cache = output / 'runtime/model_cache.sqlite'
    if cache.exists():
        with closing(sqlite3.connect(cache.as_uri() + '?mode=ro', uri=True)) as db:
            result['model_attempts'] = dict(db.execute('SELECT stage, COUNT(*) FROM attempts GROUP BY stage'))
    return result


def export_process(output_dir, destination):
    """将模型尝试记录转成可读 JSONL，原运行目录保持只读。"""
    output, destination = Path(output_dir).resolve(), Path(destination).resolve()
    if not output.is_dir():
        raise FileNotFoundError(output)
    if destination.is_relative_to(output):
        raise ValueError('Process export destination must be outside the source run directory')
    report = inspect_run(output)
    destination.mkdir(parents=True, exist_ok=False)
    cache = output / 'runtime/model_cache.sqlite'
    target = destination / 'model_attempts.jsonl'
    with target.open('w', encoding='utf-8') as stream:
        if cache.exists():
            with closing(sqlite3.connect(cache.as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                for row in db.execute('SELECT id,key,stage,created,status,value FROM attempts ORDER BY id'):
                    value = dict(row)
                    value['value'] = json.loads(value['value'])
                    stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    for name in ('analysis', 'decisions'):
        if (output / name).is_dir():
            shutil.copytree(output / name, destination / name)
    report['model_attempts'] = str(target)
    save(destination / 'process_report.json', report)
    return report
