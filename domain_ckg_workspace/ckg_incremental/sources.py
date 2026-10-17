from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from ..ckg_build.source_adapter import POINTER, prepare_source
from .runtime import digest, save


SOURCE_SCHEMA_VERSION = "incremental_sources_v2"


def split_text(text, limit=650):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Text split limit must be a positive integer")
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            candidates = list(re.finditer(r"[。！？；\n.!?;]", text[start + limit // 2:end]))
            if candidates:
                end = start + limit // 2 + candidates[-1].end()
        yield start, end, text[start:end]
        start = end


def _section_metadata(paragraph, book_id):
    path_ids = [value for value in paragraph.get("path_ids", []) if value != paragraph["id"]]
    title_path = list(paragraph.get("path_titles", []))
    section_id = (path_ids[-1] if path_ids else
                  paragraph.get("subsection_id") or paragraph.get("section_id") or
                  paragraph.get("parent_id") or paragraph.get("heading_id") or
                  paragraph.get("chapter_id") or book_id)
    return {
        "section_id": section_id, "section_path_ids": path_ids, "title_path": title_path,
        "chapter_id": paragraph.get("chapter_id"),
        "source_section_id": paragraph.get("section_id"),
        "subsection_id": paragraph.get("subsection_id"),
    }


def _preceding_context(previous, limit):
    if not previous or limit < 1:
        return [], False
    source_id = previous[-1]["source_id"]
    paragraph = []
    for row in reversed(previous):
        if row["source_id"] != source_id:
            break
        paragraph.append(row)
    paragraph.reverse()
    selected, size = [], 0
    for row in reversed(paragraph):
        if size + len(row["text"]) > limit:
            break
        selected.append(row)
        size += len(row["text"])
    selected.reverse()
    whole = bool(selected and selected[0]["start"] == 0
                 and selected[-1]["end"] == selected[-1]["paragraph_length"])
    return selected, bool(selected and not whole)


def _section_chunks(book_id, paragraphs, chunk_chars, context_chars):
    if not paragraphs:
        return []
    units = []
    for rows in paragraphs:
        # 证据虽可细分，未超长的整个来源段落仍作为不可分的装箱单位。
        if sum(len(row["text"]) for row in rows) <= chunk_chars:
            units.append(rows)
            continue
        current, size = [], 0
        for row in rows:
            if current and size + len(row["text"]) > chunk_chars:
                units.append(current)
                current, size = [], 0
            current.append(row)
            size += len(row["text"])
        if current:
            units.append(current)
    packed, current, size = [], [], 0
    for unit in units:
        unit_size = sum(len(row["text"]) for row in unit)
        if current and size + unit_size > chunk_chars:
            packed.append(current)
            current, size = [], 0
        current.extend(unit)
        size += unit_size
    if current:
        packed.append(current)
    chunks, previous = [], []
    for part_index, core in enumerate(packed, 1):
        context, partial = _preceding_context(previous, context_chars)
        first = core[0]
        core_ids, context_ids = [row["id"] for row in core], [row["id"] for row in context]
        chunks.append({
            "id": "chunk_" + digest([SOURCE_SCHEMA_VERSION, book_id, first["section_segment_id"], core_ids, context_ids])[:28],
            "book_id": book_id, "title_path": first["title_path"], "section_id": first["section_id"],
            "section_path_ids": first["section_path_ids"], "section_segment_id": first["section_segment_id"],
            "chapter_id": first["chapter_id"], "part_index": part_index, "part_count": len(packed),
            "evidence_ids": core_ids, "context_evidence_ids": context_ids,
            "core_chars": sum(len(row["text"]) for row in core),
            "context_chars": sum(len(row["text"]) for row in context),
            "context_is_partial_paragraph": partial,
            "paragraph_ids": list(dict.fromkeys(row["source_id"] for row in core)),
        })
        previous.extend(core)
    return chunks


def prepare_corpus(project_root, output, book_ids=None, chunk_chars=4500, evidence_chars=650, context_chars=650):
    for value, label in ((chunk_chars, "chunk_chars"), (evidence_chars, "evidence_chars")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")
    if evidence_chars > chunk_chars:
        raise ValueError("Evidence split size cannot exceed core chunk size")
    if isinstance(context_chars, bool) or not isinstance(context_chars, int) or context_chars < 0:
        raise ValueError("context_chars must be a nonnegative integer")
    root, output = Path(project_root), Path(output)
    destination = output / "corpus.json"
    if destination.exists():
        old = json.loads(destination.read_text("utf-8"))
        if old.get("schema_version") != SOURCE_SCHEMA_VERSION:
            raise ValueError("Source chunking schema changed to v2; use a new run directory and preserve the old corpus")
    pointer = json.loads((root / POINTER).read_text("utf-8-sig"))
    manifest = json.loads((root / pointer["manifest_path"]).read_text("utf-8-sig"))
    known = {book["book_id"] for book in manifest["books"]}
    if book_ids and not set(book_ids) <= known:
        raise ValueError("Requested book is absent from published source manifest")
    books, evidence, chunks = [], [], []
    for entry in manifest["books"]:
        if book_ids and entry["book_id"] not in book_ids:
            continue
        prepared = prepare_source(root, entry["book_id"])
        books.append({"id": prepared["book_id"], "title": prepared["book_title"],
                      "source_fingerprint": digest(prepared["source_manifest"]),
                      "paragraph_count": len(prepared["paragraphs"])})
        book_evidence, section_paragraphs = [], []
        current_section, segment_id = None, None
        for order, p in enumerate(prepared["paragraphs"]):
            text = p.get("text", p.get("content", ""))
            if not isinstance(text, str):
                raise ValueError("Source paragraph text must be a string")
            if not text.strip():
                continue
            metadata = _section_metadata(p, prepared["book_id"])
            section_key = digest([metadata["section_id"], metadata["section_path_ids"], metadata["title_path"]])
            if current_section != section_key:
                chunks.extend(_section_chunks(prepared["book_id"], section_paragraphs, chunk_chars, context_chars))
                section_paragraphs = []
                current_section = section_key
                # 非连续的同名小节不重排到一起，也不越过中间小节借用上下文。
                segment_id = "section_segment_" + digest([prepared["book_id"], section_key, p["id"]])[:28]
            paragraph_rows = []
            pages = p.get("pdf_pages", [])
            for start, end, piece in split_text(text, evidence_chars):
                row = {"id": "ev_" + digest([prepared["book_id"], p["id"], start, end, piece])[:28],
                       "book_id": prepared["book_id"], "source_id": p["id"],
                       **metadata, "section_segment_id": segment_id, "text": piece,
                       "start": start, "end": end, "paragraph_order": order,
                       "paragraph_length": len(text),
                       "paragraph_boundary_status": p.get("boundary_status"),
                       "natural_paragraph_boundary_verified": p.get("natural_paragraph_boundary_verified", False),
                       "page_start": p.get("page_start") if p.get("page_start") is not None else (min(pages) if pages else None),
                       "page_end": p.get("page_end") if p.get("page_end") is not None else (max(pages) if pages else None)}
                book_evidence.append(row)
                paragraph_rows.append(row)
            section_paragraphs.append(paragraph_rows)
        evidence.extend(book_evidence)
        chunks.extend(_section_chunks(prepared["book_id"], section_paragraphs, chunk_chars, context_chars))
    # 多本书交错调度，后续波次共同使用最新全局图谱。
    queues = defaultdict(list)
    for chunk in chunks:
        queues[chunk["book_id"]].append(chunk)
    scheduled = []
    for index in range(max((len(q) for q in queues.values()), default=0)):
        scheduled.extend(q[index] for q in queues.values() if index < len(q))
    bundle = {"books": books, "evidence": evidence, "chunks": scheduled,
              "source_version": pointer["version"], "schema_version": SOURCE_SCHEMA_VERSION,
              "chunking": {"unit": "deepest_available_heading", "core_char_limit": chunk_chars,
                           "evidence_char_limit": evidence_chars, "context_char_limit": context_chars,
                           "preserve_source_paragraphs": True, "part_index_base": 1}}
    bundle["fingerprint"] = digest(bundle)
    if destination.exists():
        old = json.loads(destination.read_text("utf-8"))
        if old["fingerprint"] != bundle["fingerprint"]:
            raise ValueError("Prepared source changed; use a new run directory")
        return old
    save(destination, bundle)
    return bundle


class SourceCorpus:
    def __init__(self, bundle):
        self.bundle = bundle
        self.evidence = {row["id"]: row for row in bundle["evidence"]}
        self.books = defaultdict(list)
        self.sections = defaultdict(list)
        self.source_order = {}
        for order, row in enumerate(bundle["evidence"]):
            self.books[row["book_id"]].append(row["id"])
            section_key = (row["book_id"], row.get("section_segment_id") or row.get("section_id")
                           or digest(row.get("title_path", [])))
            self.sections[section_key].append(row["id"])
            self.source_order[row["id"]] = order
        self.positions = {eid: (book, i) for book, rows in self.books.items() for i, eid in enumerate(rows)}
        self.section_positions = {eid: (section, i) for section, rows in self.sections.items() for i, eid in enumerate(rows)}

    def rows(self, ids):
        return [self.evidence[eid] for eid in dict.fromkeys(ids)]

    def expand(self, ids, radius=2):
        if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
            raise ValueError("Context radius must be a nonnegative integer")
        ids = list(ids)
        selected = set(ids)
        for eid in ids:
            section, i = self.section_positions[eid]
            selected.update(self.sections[section][max(0, i - radius):i + radius + 1])
        return self.rows(sorted(selected, key=self.source_order.__getitem__))

    def search(self, text, limit=6, book_id=None):
        terms = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_+.-]*|[\u4e00-\u9fff]{2,}", text))
        terms.update(text[i:i + 2] for i in range(len(text) - 1) if all("\u4e00" <= c <= "\u9fff" for c in text[i:i + 2]))
        scored = []
        for row in self.evidence.values():
            if book_id and row["book_id"] != book_id:
                continue
            score = sum(min(len(t), 12) for t in terms if t in row["text"])
            if score:
                scored.append((score, row["id"]))
        return self.rows([eid for _, eid in sorted(scored, reverse=True)[:limit]])
