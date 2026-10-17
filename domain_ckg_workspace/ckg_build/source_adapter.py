from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


POINTER = Path("研究方案_20261002/24本文本图_当前版本.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _within(root: Path, relative: str | Path) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Source path escapes project root: {relative}")
    return path


def prepare_source(project_root: Path, book_id: str) -> dict[str, Any]:
    """Freeze the published pointer and expose every paragraph in its hierarchy graph."""
    root = project_root.resolve()
    pointer_path = _within(root, POINTER)
    pointer = _read_json(pointer_path)
    manifest_path = _within(root, pointer["manifest_path"])
    manifest_hash = _sha256(manifest_path)
    if manifest_hash != pointer["manifest_sha256"]:
        raise ValueError("Published manifest hash differs from the current pointer")
    manifest = _read_json(manifest_path)
    book = next((entry for entry in manifest["books"] if entry["book_id"] == book_id), None)
    if book is None:
        raise ValueError(f"Book {book_id} is absent from the published manifest")
    hierarchy_manifest_path = _within(root, pointer["hierarchy_manifest_path"])
    hierarchy_manifest = _read_json(hierarchy_manifest_path)
    hierarchy_book = next((entry for entry in hierarchy_manifest["books"] if entry["book_id"] == book_id), None)
    if hierarchy_book is None:
        raise ValueError(f"Book {book_id} is absent from the hierarchy manifest")
    graph_path = _within(root, Path(pointer["data_directory"]) / hierarchy_book["path"])
    graph_hash = _sha256(graph_path)
    if graph_hash != hierarchy_book["sha256"]:
        raise ValueError("Hierarchy graph hash differs from the hierarchy manifest")
    graph = _read_json(graph_path)
    if graph.get("source_graph", {}).get("sha256") != book["graph_sha256"]:
        raise ValueError("Hierarchy source graph hash differs from the published manifest")
    source_graph_path = _within(root, Path(pointer["data_directory"]) / book_id / graph["source_graph"]["path"])
    source_graph_hash = _sha256(source_graph_path)
    if source_graph_hash != book["graph_sha256"]:
        raise ValueError("Actual source graph hash differs from the published manifest")
    nodes = {node["id"]: node for node in graph["nodes"]}
    paragraphs: list[dict[str, Any]] = []
    for node in graph["nodes"]:
        if node.get("type") != "paragraph":
            continue
        paragraph = dict(node)
        paragraph["path_titles"] = [
            nodes[node_id].get("label") or nodes[node_id].get("title") or ""
            for node_id in node.get("path_ids", [])[:-1]
            if node_id in nodes
        ]
        paragraphs.append(paragraph)
    if len(paragraphs) != book["paragraphs"]:
        raise ValueError("Hierarchy paragraph count differs from the published manifest")
    source_manifest = {
        "pointer_path": str(pointer_path),
        "pointer_sha256": _sha256(pointer_path),
        "pointer": pointer,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "manifest_version": manifest.get("version"),
        "book_manifest": book,
        "hierarchy_manifest_path": str(hierarchy_manifest_path),
        "hierarchy_manifest_sha256": _sha256(hierarchy_manifest_path),
        "hierarchy_book_manifest": hierarchy_book,
        "hierarchy_graph_path": str(graph_path),
        "hierarchy_graph_sha256": graph_hash,
        "source_graph_path": str(source_graph_path),
        "source_graph_sha256": source_graph_hash,
        "hierarchy_schema_version": graph.get("schema_version"),
        "nonprose_source": graph.get("nonprose_source"),
        "human_verified": graph.get("human_verified", False),
        "semantic_assignment_verified": graph.get("semantic_assignment_verified", False),
    }
    return {
        "source_manifest": source_manifest,
        "book_id": book_id,
        "book_title": graph.get("book_title") or book["book_title"],
        "paragraphs": paragraphs,
    }
