from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import unicodedata
from typing import Any

try:
    from .source_adapter import prepare_source
except ImportError:
    from source_adapter import prepare_source


DEFAULT_LEGACY = Path("工作区/concept-kg-ablation-20260905/domain_ckg_workspace/outputs/csapp_flash_final_20260918_0845")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        for folded in unicodedata.normalize("NFKC", char).casefold():
            if folded.isalnum():
                chars.append(folded)
                offsets.append(index)
    return "".join(chars), offsets


def _grams(text: str) -> set[str]:
    if len(text) < 4:
        return {text} if text else set()
    return {text[index:index + 4] for index in range(len(text) - 3)}


def _current_slice(paragraph: dict[str, Any], start: int, end: int, match_type: str, score: float) -> dict[str, Any]:
    source = paragraph["text"]
    return {
        "paragraph_id": paragraph["id"],
        "start": start,
        "end": end,
        "text": source[start:end],
        "match_type": match_type,
        "score": round(score, 4),
    }


class ParagraphIndex:
    def __init__(self, paragraphs: list[dict[str, Any]]) -> None:
        self.paragraphs = paragraphs
        self.normalized: list[str] = []
        self.offsets: list[list[int]] = []
        self.grams: list[set[str]] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        for index, paragraph in enumerate(paragraphs):
            normalized, offsets = _normalized(paragraph.get("text") or "")
            grams = _grams(normalized)
            self.normalized.append(normalized)
            self.offsets.append(offsets)
            self.grams.append(grams)
            for gram in grams:
                self.postings[gram].append(index)

    def _rank(self, query: str) -> list[tuple[int, float]]:
        query_grams = _grams(query)
        if not query_grams:
            return []
        # 稀有片段优先，避免每条旧证据扫描整本书。
        anchors = sorted(query_grams, key=lambda gram: (len(self.postings.get(gram, [])), gram))[:96]
        counts: Counter[int] = Counter()
        for gram in anchors:
            counts.update(self.postings.get(gram, []))
        candidate_ids = [index for index, _ in counts.most_common(180)]
        ranked = []
        for index in candidate_ids:
            common = len(query_grams & self.grams[index])
            denominator = max(1, min(len(query_grams), len(self.grams[index])))
            ranked.append((index, common / denominator))
        return sorted(ranked, key=lambda row: (-row[1], row[0]))

    def map_evidence(self, evidence: dict[str, Any]) -> dict[str, Any]:
        old_text = evidence.get("text") or ""
        query, _ = _normalized(old_text)
        ranked = self._rank(query)
        query_grams = _grams(query)
        included = {index for index, _ in ranked}
        for index, _ in ranked[:24]:
            for neighbor in (index - 2, index - 1, index + 1, index + 2):
                if 0 <= neighbor < len(self.paragraphs) and neighbor not in included:
                    common = len(query_grams & self.grams[neighbor])
                    denominator = max(1, min(len(query_grams), len(self.grams[neighbor])))
                    ranked.append((neighbor, common / denominator))
                    included.add(neighbor)
        ranked.sort(key=lambda row: (-row[1], row[0]))
        verified: list[dict[str, Any]] = []
        for index, score in ranked:
            paragraph = self.paragraphs[index]
            text = paragraph.get("text") or ""
            current_norm = self.normalized[index]
            if len(current_norm) < 20 or min(len(query), len(current_norm)) < 20:
                continue
            if old_text and old_text in text:
                start = text.index(old_text)
                verified.append(_current_slice(paragraph, start, start + len(old_text), "exact", 1.0))
            elif text and text in old_text and len(text) <= 4000:
                verified.append(_current_slice(paragraph, 0, len(text), "exact", 1.0))
            elif query in current_norm:
                offset = current_norm.index(query)
                start = self.offsets[index][offset]
                end = self.offsets[index][offset + len(query) - 1] + 1
                verified.append(_current_slice(paragraph, start, end, "normalized", 1.0))
            elif current_norm in query and len(text) <= 4000:
                verified.append(_current_slice(paragraph, 0, len(text), "normalized", 1.0))
        if verified:
            verified.sort(key=lambda item: (item["paragraph_id"], item["start"]))
            text_counts = Counter(item["text"] for item in verified)
            for item in verified:
                item["ambiguous_match"] = text_counts[item["text"]] > 1
            covered = [False] * len(query)
            for item in verified:
                matched, _ = _normalized(item["text"])
                if not matched:
                    continue
                at = query.find(matched)
                while at >= 0:
                    covered[at:at + len(matched)] = [True] * len(matched)
                    at = query.find(matched, at + 1)
            covered_count = sum(covered)
            fraction = covered_count / len(query) if query else 0.0
            partial = fraction < 0.98
            context: list[dict[str, Any]] = []
            if partial:
                verified_ids = {item["paragraph_id"] for item in verified}
                neighbor_ids: set[int] = set()
                for index, paragraph in enumerate(self.paragraphs):
                    if paragraph["id"] not in verified_ids:
                        continue
                    neighbor_ids.update(range(max(0, index - 2), min(len(self.paragraphs), index + 3)))
                scored_neighbors = []
                for index in neighbor_ids:
                    paragraph = self.paragraphs[index]
                    if paragraph["id"] in verified_ids or len(self.normalized[index]) < 20:
                        continue
                    common = len(query_grams & self.grams[index])
                    score = common / max(1, min(len(query_grams), len(self.grams[index])))
                    if score >= 0.2:
                        scored_neighbors.append((index, score))
                for index, score in sorted(scored_neighbors, key=lambda row: (-row[1], row[0]))[:4]:
                    paragraph = self.paragraphs[index]
                    end = min(len(paragraph.get("text") or ""), 1200)
                    context.append(_current_slice(paragraph, 0, end, "fuzzy_candidate", score))
            kinds = {item["match_type"] for item in verified}
            status = "needs_semantic_review" if partial else ("ambiguous" if any(item["ambiguous_match"] for item in verified) else ("exact" if kinds == {"exact"} else "normalized"))
            return {
                "legacy_evidence_id": evidence.get("id"),
                "source_mapping_status": status,
                "current_evidence": verified + context,
                "verified_match_types": sorted(kinds),
                "normalized_legacy_text_coverage": round(fraction, 4),
                "normalized_legacy_chars_unmatched": len(query) - covered_count,
                "context_candidate_count": len(context),
                "legacy_evidence": evidence,
            }
        fuzzy = []
        for index, score in ranked[:3]:
            if score < 0.3:
                continue
            paragraph = self.paragraphs[index]
            end = min(len(paragraph.get("text") or ""), 1200)
            fuzzy.append(_current_slice(paragraph, 0, end, "fuzzy_candidate", score))
        return {
            "legacy_evidence_id": evidence.get("id"),
            "source_mapping_status": "needs_semantic_review" if fuzzy else "unresolved",
            "current_evidence": fuzzy,
            "verified_match_types": [],
            "normalized_legacy_text_coverage": 0.0,
            "normalized_legacy_chars_unmatched": len(query),
            "context_candidate_count": len(fuzzy),
            "legacy_evidence": evidence,
        }


def _items_with_mapping(items: list[dict[str, Any]], mappings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for item in items:
        linked = [mappings[evidence["id"]] for evidence in item.get("evidence", []) if evidence.get("id") in mappings]
        current = [entry for mapping in linked for entry in mapping["current_evidence"]]
        statuses = {mapping["source_mapping_status"] for mapping in linked}
        if not linked:
            status = "unresolved"
        elif statuses & {"unresolved", "needs_semantic_review"}:
            status = "needs_semantic_review"
        elif "ambiguous" in statuses:
            status = "ambiguous"
        elif "normalized" in statuses:
            status = "normalized"
        else:
            status = "exact"
        enriched.append({**item, "raw": item, "current_evidence": current, "source_mapping_status": status})
    return enriched


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def prepare_bundle(project_root: Path, book_id: str, legacy_root: Path | None, output: Path) -> dict[str, Any]:
    root = project_root.resolve()
    legacy = (legacy_root or root / DEFAULT_LEGACY).resolve()
    candidates_path = legacy / "csapp_flash_full" / "candidates.json"
    book_path = legacy / "inputs" / "book.json"
    prior_path = legacy / "inputs" / "prior.json"
    audit_path = legacy / "final_audit.json"
    source = prepare_source(root, book_id)
    source_manifest = source["source_manifest"]
    input_hashes = {name: _sha256(path) for name, path in {
        "candidates": candidates_path, "book": book_path, "prior": prior_path, "final_audit": audit_path,
    }.items()}
    fingerprint_payload = {
        "pointer": source_manifest["pointer_sha256"],
        "manifest": source_manifest["manifest_sha256"],
        "graph": source_manifest["hierarchy_graph_sha256"],
        "source_graph": source_manifest["source_graph_sha256"],
        "legacy": input_hashes,
        "adapter_version": 3,
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")).hexdigest()
    output = output.resolve()
    bundle_path = output / "prepared_bundle.json"
    if output.exists():
        if bundle_path.exists():
            prior_bundle = _load(bundle_path)
            if prior_bundle.get("source_manifest", {}).get("input_fingerprint") == fingerprint:
                return {"paths": {name: str(output / name) for name in ("prepared_bundle.json", "source_manifest.json", "input_units.jsonl", "legacy_mapping.jsonl", "coverage.json")}, "coverage": prior_bundle["coverage"], "input_fingerprint": fingerprint, "reused": True}
        raise FileExistsError(f"Output folder already exists with different or incomplete inputs: {output}")
    candidates = _load(candidates_path)
    original_book = _load(book_path)
    prior = _load(prior_path)
    audit = _load(audit_path)
    if original_book.get("book_title") != source["book_title"]:
        raise ValueError("Legacy and published book titles differ")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in candidates.get("concepts", []) + candidates.get("relations", []):
        for evidence in item.get("evidence", []):
            evidence_id = evidence.get("id")
            if not evidence_id:
                continue
            if evidence_id in evidence_by_id and evidence_by_id[evidence_id] != evidence:
                raise ValueError(f"Legacy evidence ID has inconsistent contents: {evidence_id}")
            evidence_by_id[evidence_id] = evidence
    index = ParagraphIndex(source["paragraphs"])
    mappings = {evidence_id: index.map_evidence(evidence) for evidence_id, evidence in evidence_by_id.items()}
    concepts = _items_with_mapping(candidates.get("concepts", []), mappings)
    relations = _items_with_mapping(candidates.get("relations", []), mappings)
    mapping_counts = dict(Counter(mapping["source_mapping_status"] for mapping in mappings.values()))
    partial_count = sum(bool(mapping["verified_match_types"]) and mapping["normalized_legacy_text_coverage"] < 0.98 for mapping in mappings.values())
    contextual_count = sum(mapping["context_candidate_count"] for mapping in mappings.values())
    coverage = {
        "paragraphs": len(source["paragraphs"]),
        "paragraphs_retrieval_ineligible": sum(not paragraph.get("retrieval_eligible", False) for paragraph in source["paragraphs"]),
        "paragraphs_without_chapter": sum(not paragraph.get("chapter_id") for paragraph in source["paragraphs"]),
        "legacy_concepts": len(concepts),
        "legacy_relations": len(relations),
        "legacy_unique_evidence": len(mappings),
        "source_mapping_status_counts": mapping_counts,
        "legacy_evidence_with_partial_verified_coverage": partial_count,
        "contextual_or_retrieval_candidates": contextual_count,
        "concept_mapping_status_counts": dict(Counter(item["source_mapping_status"] for item in concepts)),
        "relation_mapping_status_counts": dict(Counter(item["source_mapping_status"] for item in relations)),
        "prior_concepts": len(prior.get("concepts", [])),
        "prior_relations": len(prior.get("relations", [])),
        "legacy_generation": {
            "candidate_status": candidates.get("status"),
            "semantic_completeness_verified": candidates.get("semantic_completeness_verified"),
            "unresolved_generation_contexts": candidates.get("unresolved_generation_contexts"),
            "audit_scope": audit.get("scope"),
            "audit_counts": {key: audit.get("counts", {}).get(key) for key in ("at_cap_chunks", "incomplete_or_unknown_chunks", "generation_errors", "quarantined_items")},
            "semantic_correctness_verified": audit.get("semantic_correctness_verified"),
        },
        "source_human_verified": source_manifest["human_verified"],
        "source_semantic_assignment_verified": source_manifest["semantic_assignment_verified"],
        "nonprose_source": source_manifest["nonprose_source"],
    }
    source_manifest.update({"legacy_input_paths": {"candidates": str(candidates_path), "book": str(book_path), "prior": str(prior_path), "final_audit": str(audit_path)}, "legacy_input_sha256": input_hashes, "input_fingerprint": fingerprint})
    bundle = {
        **source,
        "concepts": concepts,
        "relations": relations,
        "prior_concepts": prior.get("concepts", []),
        "prior_relations": prior.get("relations", []),
        "prior_metadata": {key: value for key, value in prior.items() if key not in ("concepts", "relations")},
        "legacy_candidate_metadata": {key: value for key, value in candidates.items() if key not in ("concepts", "relations")},
        "source_mappings": list(mappings.values()),
        "coverage": coverage,
    }
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "source_manifest.json", source_manifest)
    _write_jsonl(output / "input_units.jsonl", source["paragraphs"])
    _write_jsonl(output / "legacy_mapping.jsonl", list(mappings.values()))
    _write_json(output / "coverage.json", coverage)
    _write_json(bundle_path, bundle)
    return {"paths": {name: str(output / name) for name in ("prepared_bundle.json", "source_manifest.json", "input_units.jsonl", "legacy_mapping.jsonl", "coverage.json")}, "coverage": coverage, "input_fingerprint": fingerprint, "reused": False}


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a published paragraph graph and legacy candidates")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_bundle(args.project_root, args.book_id, args.legacy_root, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
