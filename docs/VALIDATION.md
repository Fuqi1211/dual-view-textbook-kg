# Validation

The release is checked with network-free synthetic data and the existing implementation's regression tests.

| Suite | Cases | Scope |
|---|---:|---|
| Public interfaces | 10 | Structure export and materialization, provenance checks, concept builds, resume, failure handling, locks, process export, and local HTTP responses |
| Concept engine regressions | 90 | Extraction, semantic identity, merging, storage, index reuse, memory behavior, and projection input handling |
| Structure cross-page regressions | 4 | Existing v6 cross-page paragraph boundary rules |

```bash
python -m pip install '.[legacy-pdf]'
python -m unittest discover -s tests
python -m unittest discover -s upstream_tests
python -m unittest discover -s legacy_text_graph_tools -p test_v6_cross_page.py
```

Run the offline example to inspect both interfaces and the incremental engine:

```bash
python -m dual_kg demo --output ./demo_output
python -m dual_kg process-export --output ./demo_output/concepts --destination ./demo_process
```

The example uses a synthetic textbook and fixed model outputs. It produces two concept nodes and one evidence-linked relation. It is an integration check, not an extraction-quality benchmark.

The source snapshot was also checked against a private textbook publication: the source manifest, hierarchy graph, and physical source hashes were consistent for 24 books and 117,692 paragraphs. These books and their graphs are not distributed in this repository. Hash consistency does not establish full OCR correctness or human semantic acceptance.

Real model endpoints, paid embeddings, production Neo4j writes, and a complete reconstruction of historical PDF repair inputs are outside the offline test scope. Historical repair scripts can require original PDFs, OCR outputs, prior graph versions, and manually reviewed evidence.

The interfaces save processing records and separate automatic completion from human acceptance. Synthetic results and historical checks should not be presented as new paper experiments.
