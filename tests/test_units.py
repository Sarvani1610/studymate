from app.services.chunking import chunk_segments
from app.services.embeddings import LocalHashEmbedder, cosine
from app.services.extraction import Segment, strip_repeated_lines
from app.services.keywords import extract_keyphrases
from app.services.llm import extractive_answer, parse_json_loose
from app.services.rate_limit import parse_limit
from app.services.retrieval import BM25
from app.utils.text import split_sentences


def test_sentence_split_keeps_abbreviations():
    sents = split_sentences("Trees are graphs, e.g. a heap. They have no cycles! Do they? Yes.")
    assert sents[0] == "Trees are graphs, e.g. a heap."
    assert len(sents) == 4


def test_chunks_respect_sections_and_budget():
    segments = [
        Segment("Intro sentence one. " * 40, section="Intro"),
        Segment("Other topic here. " * 5, section="Other"),
    ]
    chunks = chunk_segments(segments, target_tokens=60, overlap_sentences=1)
    assert all(c.token_count <= 90 for c in chunks)
    assert {c.section for c in chunks} == {"Intro", "Other"}
    assert not any("Intro sentence" in c.text and "Other topic" in c.text for c in chunks)


def test_repeated_header_lines_removed():
    pages = [f"CS 2028 Fall 2026\nreal content {i}\nPage {i} of 6" for i in range(6)]
    cleaned = strip_repeated_lines(pages)
    assert all("CS 2028" not in p and "Page" not in p for p in cleaned)
    assert "real content 3" in cleaned[3]


def test_local_embedder_similarity_makes_sense():
    emb = LocalHashEmbedder(384)
    heap = emb.embed_one("binary heap priority queue extract min")
    heap2 = emb.embed_one("priority queues are implemented with heaps")
    unrelated = emb.embed_one("photosynthesis converts light into chemical energy")
    assert abs(cosine(heap, heap) - 1.0) < 1e-6
    assert cosine(heap, heap2) > cosine(heap, unrelated)


def test_bm25_ranks_matching_doc_first():
    docs = [["heap", "priority", "queue"], ["hash", "table", "bucket"], ["tree", "rotation"]]
    scores = BM25(docs).score(["hash", "bucket"])
    assert scores.index(max(scores)) == 1


def test_keyphrases_prefer_phrases():
    texts = ["A binary search tree keeps keys ordered. Binary search tree lookups are fast."] * 3 + [
        "A hash table uses buckets. The hash table resizes when full."] * 3
    phrases = extract_keyphrases(texts, top_n=5)
    assert "binary search tree" in phrases
    assert "hash table" in phrases


def test_extractive_answer_cites_passages():
    passages = [(1, "A heap is a complete binary tree. It is stored in an array."),
                (2, "Hash tables map keys to buckets.")]
    answer = extractive_answer("What is a heap?", passages)
    assert "[1]" in answer and "[2]" not in answer


def test_parse_json_loose_handles_fences():
    assert parse_json_loose('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('here you go {"cards": []} thanks') == {"cards": []}


def test_parse_limit():
    assert parse_limit("20/minute") == (20, 60)
    assert parse_limit("5/hours") == (5, 3600)
