# Dual-View Textbook Knowledge Graph

Build and inspect two connected views of textbook knowledge: a **document structure graph** and an **evidence-grounded concept graph**. Both workflows are exposed as Python functions, command-line commands, and local HTTP endpoints.

- **Structure view:** book, chapter, section, subsection, and paragraph nodes with containment, reading order, and source provenance. Existing published graphs can be verified and exported without changing their identifiers.
- **Concept view:** an initial domain prior G0, textbook evidence extraction, semantic identity resolution, versioned concept and relation updates, and resumable processing.
- **Visible processing:** source chunks, analysis plans, decisions, model response attempts, revision history, and final graphs are saved locally and can be exported for inspection.

[中文使用说明](docs/README.zh-CN.md) · [API reference](docs/API.md) · [Pipeline and source map](docs/构建过程与源码对应.md)

## Installation

Python 3.10 or later is required.

```bash
git clone https://github.com/Fuqi1211/dual-view-textbook-kg.git
cd dual-view-textbook-kg
python -m pip install .
```

Optional dependencies:

```bash
python -m pip install '.[neo4j]'       # graph database projection
python -m pip install '.[legacy-pdf]'  # historical PDF processing scripts
```

## Run the offline example

```bash
python -m dual_kg demo --output ./demo_output
python -m dual_kg status --output ./demo_output/concepts
python -m dual_kg process-export --output ./demo_output/concepts --destination ./demo_process
```

The example runs both graph interfaces and the incremental engine on a synthetic textbook. It requires no API key, network access, or graph database. Its fixed model responses test integration and must not be used as extraction-quality results. Output directories for the demo and process export must not already exist.

## Python interfaces

```python
from dual_kg import (
    build_structure,
    export_structure,
    prepare_concepts,
    build_concepts,
    inspect_run,
    export_process,
)

# Materialize the structure view from an existing physical paragraph graph.
structure = build_structure(
    "inputs/text_graph.json",
    "outputs/structure",
    book_id="example_book",
    book_title="Example textbook",
)

# Alternatively, verify and export a graph from a published textbook workspace.
structure = export_structure("textbook_workspace", "example_book", "outputs/exported")

# Prepare evidence chunks from the published graphs, preserving paragraph IDs.
corpus = prepare_concepts("textbook_workspace", "outputs/prepared", book_ids=["example_book"])

# Real model requests require explicit opt-in and environment-based credentials.
result = build_concepts(
    "outputs/prepared/corpus.json",
    "outputs/concepts",
    config="config/incremental.example.yaml",
    allow_api=True,
    max_chunks=2,
)
state = inspect_run("outputs/concepts")
export_process("outputs/concepts", "outputs/process")
```

These are separate entry points: use a fresh output directory for each export. `build_structure` accepts the physical paragraph graph schema, including `headings`, `nodes`, and source spans. It materializes the hierarchy using the existing structure builder; it does not run OCR or the historical PDF repair chain. Existing v6 structure graphs can be reused directly with `export_structure`.

`prepare_concepts` supports the versioned publication layout described in [the input format](docs/API.md#published-textbook-input). Alternatively, `build_concepts` accepts an already prepared, fingerprinted `corpus.json`. The offline example creates a minimal valid publication layout for reference.

The example configuration contains provider and model names, not credentials. Review the endpoints, model availability, and concurrency before a real run. Set the environment variable named in the configuration, such as `SILICONFLOW_API_KEY`. Model and embedding requests may incur provider charges.

## CLI and HTTP

```bash
python -m dual_kg --help
python -m dual_kg structure-build --source-graph inputs/text_graph.json --book-id example_book --book-title "Example textbook" --output outputs/structure
python -m dual_kg concepts-build --corpus outputs/prepared/corpus.json --output outputs/concepts --allow-api --max-chunks 2
python -m dual_kg serve --port 8765
```

The HTTP service listens on `127.0.0.1`. It provides synchronous build endpoints and a separate status endpoint. Full request schemas and examples are in [the API reference](docs/API.md). It is intended for local integration and should not be exposed directly to the public internet.

To resume a concept build, reuse the same corpus and run directory. Omit `max_chunks` to process the full input and run the final checks. `max_chunks=N` selects the first N source chunks, rather than N additional chunks.

## Inspect intermediate work

| Artifact | Contents |
|---|---|
| `corpus.json` | Evidence text, paragraph IDs, hierarchy paths, offsets, and source chunks |
| `g0.json` | Initial domain prior without textbook evidence |
| `analysis/*.json` | Per-chunk analysis plans and graph versions |
| `decisions/*.json` | Committed decisions, revisions, and merge records |
| `graph.sqlite` | Transactional working graph, claims, events, evidence, and checkpoints |
| `runtime/model_cache.sqlite` | Model responses, failed attempts, and embedding cache |
| `run_state.json` | Progress, skipped inputs, and database projection status |
| `run_manifest.json` | Input, model, prompt, and implementation fingerprints |
| `graph.json` | Exported working graph |
| Process export `model_attempts.jsonl` | Readable model response and attempt records |

The offline runtime does not create a real model cache. An empty model-attempt export is expected for that example.

`partial`, `complete_with_skips`, and `failed` require different handling. `complete` means the automatic pipeline finished for the specified input; it does not imply human acceptance. Neo4j projection is optional and is available through the Python `projection` argument. Without it, `database_written` is false.

## Repository layout

```text
dual_kg/                         Public API, CLI, local HTTP service, packaged resources
domain_ckg_workspace/ckg_incremental/  Incremental concept graph engine
domain_ckg_workspace/ckg_build/    Published-source adapter and supporting implementation
legacy_text_graph_tools/         Structure graph construction, repair, and verification
config/                          Provider configuration and domain schema
prompts/incremental_v5/          Stage-specific model prompts
examples/                        Synthetic textbook and offline runtime
tests/                           Public interface tests
upstream_tests/                  Concept engine regression tests
docs/                            Input contracts, pipeline description, and validation
```

Historical PDF processing scripts are included to make the construction steps inspectable. Some require original PDFs, OCR output, prior graph versions, and repair evidence; they are not a one-command PDF-to-v6 workflow. See [the pipeline description](docs/构建过程与源码对应.md).

## Tests

```bash
python -m pip install '.[legacy-pdf]'
python -m unittest discover -s tests
python -m unittest discover -s upstream_tests
python -m unittest discover -s legacy_text_graph_tools -p test_v6_cross_page.py
```

The three suites cover 10 API tests, 90 concept-engine regression tests, and 4 cross-page structure tests. No real model API requests are needed. The validation scope and reproducibility limits are documented in [VALIDATION.md](docs/VALIDATION.md).

## Data and license

Source code is released under the [MIT License](LICENSE). The repository contains synthetic examples and does not distribute copyrighted textbooks, private model caches, credentials, or production graph databases. Generated outputs may contain textbook excerpts; check the source permissions before sharing them.

The repository implements graph construction and provenance interfaces. It does not include a question-answering controller or claim-support-group system, and its offline example is not a paper benchmark result.
