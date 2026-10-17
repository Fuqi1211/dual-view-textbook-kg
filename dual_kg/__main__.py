from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import build_concepts, build_structure, export_process, export_structure, inspect_run, prepare_concepts


def main(argv=None):
    parser = argparse.ArgumentParser(description='双视角知识图谱接口')
    commands = parser.add_subparsers(dest='command', required=True)
    structure = commands.add_parser('structure-export', help='核验并导出已有发布图')
    structure.add_argument('--project-root', required=True)
    structure.add_argument('--book-id', required=True)
    structure.add_argument('--output', required=True)
    materialize = commands.add_parser('structure-build', help='从已有物理段落图生成层级视图')
    materialize.add_argument('--source-graph', required=True)
    materialize.add_argument('--book-id', required=True)
    materialize.add_argument('--book-title', required=True)
    materialize.add_argument('--output', required=True)
    prepare = commands.add_parser('concepts-prepare', help='从发布图准备证据片段')
    prepare.add_argument('--project-root', required=True)
    prepare.add_argument('--book-id', action='append')
    prepare.add_argument('--output', required=True)
    prepare.add_argument('--config')
    build = commands.add_parser('concepts-build', help='调用原增量引擎构建或恢复')
    build.add_argument('--corpus', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--config')
    build.add_argument('--schema')
    build.add_argument('--prompts')
    build.add_argument('--allow-api', action='store_true')
    build.add_argument('--max-chunks', type=int)
    status = commands.add_parser('status')
    status.add_argument('--output', required=True)
    process = commands.add_parser('process-export', help='导出可读模型响应、分析和决策')
    process.add_argument('--output', required=True)
    process.add_argument('--destination', required=True)
    demo = commands.add_parser('demo', help='完整运行合成示例，无真实模型调用')
    demo.add_argument('--output', required=True)
    serve = commands.add_parser('serve', help='启动仅本机可访问的同步 HTTP 接口')
    serve.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == 'structure-export':
        result = export_structure(args.project_root, args.book_id, args.output)
    elif args.command == 'structure-build':
        result = build_structure(args.source_graph, args.output, book_id=args.book_id, book_title=args.book_title)
    elif args.command == 'concepts-prepare':
        bundle = prepare_concepts(args.project_root, args.output, book_ids=args.book_id, config=args.config)
        result = dict(corpus=str(Path(args.output).resolve() / 'corpus.json'),
                      books=len(bundle['books']), evidence=len(bundle['evidence']), chunks=len(bundle['chunks']))
    elif args.command == 'concepts-build':
        result = build_concepts(args.corpus, args.output, config=args.config, schema=args.schema,
            prompts=args.prompts, allow_api=args.allow_api, max_chunks=args.max_chunks)
    elif args.command == 'status':
        result = inspect_run(args.output)
    elif args.command == 'process-export':
        result = export_process(args.output, args.destination)
    elif args.command == 'demo':
        from examples.make_fixture import make_fixture
        from examples.offline_runtime import OfflineRuntime
        root = Path(args.output).resolve()
        root.mkdir(parents=True, exist_ok=False)
        project = make_fixture(root / 'fixture')
        export_structure(project, 'demo_book', root / 'structure')
        build_structure(project / 'data/demo_book/text_graph.json', root / 'structure_build',
                        book_id='demo_book', book_title='合成教材：存储基础')
        prepare_concepts(project, root / 'prepared')
        result = build_concepts(root / 'prepared/corpus.json', root / 'concepts', runtime=OfflineRuntime())
    else:
        from .http import create_server
        server = create_server(port=args.port)
        print(f'HTTP interface: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
