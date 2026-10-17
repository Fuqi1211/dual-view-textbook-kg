# API reference

All paths refer to local files. Relative paths are resolved against the process working directory. HTTP JSON keys match the Python parameter names.

| Python function | HTTP endpoint | Required parameters | Optional parameters |
|---|---|---|---|
| `build_structure` | `POST /v1/structure/build` | `source_graph`, `output_dir`, `book_id`, `book_title` | — |
| `export_structure` | `POST /v1/structure/export` | `project_root`, `book_id`, `output_dir` | — |
| `prepare_concepts` | `POST /v1/concepts/prepare` | `project_root`, `output_dir` | `book_ids`, `config` |
| `build_concepts` | `POST /v1/concepts/build` | `corpus`, `output_dir` | `config`, `schema`, `prompts`, `allow_api`, `max_chunks` |
| `inspect_run` | `GET /v1/runs/status` | `output_dir` query parameter | — |
| `export_process` | `POST /v1/runs/export-process` | `output_dir`, `destination` | — |
| — | `GET /health` | — | — |

`runtime` and `projection` can be injected through Python only. The caller owns their lifetime. An internally created model runtime is closed when the build returns or fails. Use `Neo4jProjection` from `domain_ckg_workspace.ckg_incremental.neo4j_store` for the optional database projection.

## Input and return values

`build_structure` accepts the existing physical paragraph graph format, including `headings` and `nodes`, with paragraph source spans. It returns the generated hierarchy path, counts, and verification status. It does not accept a PDF directly. `export_structure` verifies the publication manifest and graph hashes, then copies the existing source and hierarchy graph bytes without changing IDs. Both require a new output directory.

`prepare_concepts` returns a complete corpus dictionary and writes `corpus.json`. Its evidence rows preserve `source_id`, `book_id`, heading paths, page metadata, and character offsets. Existing output can be reused only with identical input.

`build_concepts` accepts this corpus file, checks its fingerprint, invokes the incremental engine, and returns the same summary as `inspect_run`. Real model requests require `allow_api=True`; the HTTP value must be the JSON boolean `true`. `max_chunks` must be a positive integer. Omit it to process all source chunks. Identical input and output paths resume saved work. A different input fingerprint is rejected.

`inspect_run` returns current state, API state, errors, graph path, and existing process-artifact paths. It is read-only and does not create a run directory.

`export_process` creates a new directory outside the original run directory. It copies analysis and decision files and exports model attempt records to readable JSONL. A finished run should be used for a consistent final audit export. Outputs may contain source text.

## Published textbook input

The published-source adapter supports the original versioned textbook layout. The default pointer file is:

```text
<project_root>/研究方案_20261002/24本文本图_当前版本.json
```

Its fields are:

```json
{
  "version": "example-v1",
  "data_directory": "data",
  "manifest_path": "data/manifest.json",
  "manifest_sha256": "<SHA-256 of manifest.json>",
  "hierarchy_manifest_path": "data/hierarchy_manifest.json"
}
```

The source manifest lists each book's `book_id`, `book_title`, `paragraphs`, and `graph_sha256`. The hierarchy manifest lists `book_id`, graph `path`, and graph `sha256`. The hierarchy graph references its physical graph with `source_graph.path` and `source_graph.sha256`. All referenced paths must remain inside `project_root`.

`examples/make_fixture.py` creates a complete synthetic instance of this layout. For an existing v6 publication, reuse the current pointer rather than rebuilding its graph.

## HTTP example

Start the server:

```bash
python -m dual_kg serve --port 8765
```

Export a published graph:

```bash
curl -X POST http://127.0.0.1:8765/v1/structure/export \
  -H 'Content-Type: application/json' \
  -d '{"project_root":"./textbook_workspace","book_id":"example_book","output_dir":"./outputs/structure_export"}'
```

Inspect progress in a separate request:

```bash
curl --get http://127.0.0.1:8765/v1/runs/status \
  --data-urlencode 'output_dir=./outputs/concepts'
```

Requests are synchronous. Long builds require a suitable HTTP client timeout; CLI or Python is preferable for full-corpus execution. The service listens on the loopback interface, accepts JSON objects up to 1 MiB, and rejects requests carrying a browser Origin header.

| Status | Meaning |
|---|---|
| `200` | Request completed; inspect the returned pipeline phase |
| `400` | Invalid input, missing file, or failed provenance check |
| `403` | Browser-origin request rejected |
| `404` | Unknown endpoint |
| `409` | Output already exists or another build holds the run lock |
| `500` | Build or server operation failed; inspect the run directory |

## Pipeline states

- `running`: the wrapper is active; the engine state reports its current stage.
- `partial`: the selected chunk limit was processed.
- `complete_with_skips`: some source chunks or prior checks were unsuccessful.
- `complete`: the automatic pipeline finished for the full input.
- `failed`: an exception was recorded and propagated.

Automatic completion does not imply human acceptance. `human_accepted` remains false until a separate evaluation establishes it. Database writes are reported explicitly as `database_written`.
