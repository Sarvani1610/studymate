"""Sentence aware chunking.

Chunks are packed from whole sentences up to a token target, with a small
sentence overlap between neighbours so an answer that straddles a boundary is
still retrievable. Chunks never cross a section heading, because mixing two
topics in one chunk hurts retrieval more than a slightly short chunk does.
"""
from dataclasses import dataclass

from ..utils.text import estimate_tokens, split_sentences


@dataclass
class ChunkDraft:
    text: str
    page: int | None
    section: str | None
    token_count: int


def chunk_segments(segments, target_tokens=220, overlap_sentences=1, min_tokens=25):
    drafts = []
    groups = []
    for seg in segments:
        key = seg.section
        if groups and groups[-1][0] == key:
            groups[-1][1].append(seg)
        else:
            groups.append((key, [seg]))

    for section, segs in groups:
        sentences = []
        for seg in segs:
            for sentence in split_sentences(seg.text):
                sentences.append((sentence, seg.page))
        drafts.extend(_pack(sentences, section, target_tokens, overlap_sentences))

    return _merge_small(drafts, min_tokens, target_tokens)


def _pack(sentences, section, target, overlap):
    out = []
    current = []
    current_tokens = 0
    for sentence, page in sentences:
        tokens = estimate_tokens(sentence)
        if tokens > target:
            # A very long "sentence" (tables, code, lists without punctuation). Hard split on words.
            if current:
                out.append(_finish(current, section))
                current, current_tokens = [], 0
            for piece in _hard_split(sentence, target):
                out.append(ChunkDraft(piece, page, section, estimate_tokens(piece)))
            continue
        if current and current_tokens + tokens > target:
            out.append(_finish(current, section))
            current = current[-overlap:] if overlap else []
            current_tokens = sum(estimate_tokens(s) for s, _ in current)
        current.append((sentence, page))
        current_tokens += tokens
    if current:
        out.append(_finish(current, section))
    return out


def _finish(sentences, section):
    text = " ".join(s for s, _ in sentences)
    page = next((p for _, p in sentences if p is not None), None)
    return ChunkDraft(text, page, section, estimate_tokens(text))


def _hard_split(text, target):
    words = text.split()
    size = max(20, target * 3)  # about 3 words per 4 tokens, rounded down on purpose
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def _merge_small(drafts, min_tokens, target):
    merged = []
    for draft in drafts:
        if (
            merged
            and draft.token_count < min_tokens
            and merged[-1].section == draft.section
            and merged[-1].token_count + draft.token_count <= int(target * 1.3)
        ):
            prev = merged[-1]
            text = prev.text + " " + draft.text
            merged[-1] = ChunkDraft(text, prev.page, prev.section, estimate_tokens(text))
        else:
            merged.append(draft)
    return merged
