"""Key concept extraction with TF-IDF over a course's chunks.

Used to name topics for quiz questions and flashcards, and to seed the study
guide. Candidates are single words and two or three word phrases that do not
start or end with a stopword. Phrases get a boost because "binary search tree"
is a much more useful topic label than "tree".
"""
import math
import re
from collections import Counter, defaultdict

from ..utils.text import STOPWORDS, tokenize

DETERMINERS = {"a", "an", "the", "each", "every", "this", "these", "that", "its", "their", "any", "our"}


def _candidates(text, evidence=None):
    out = []
    for sentence in re.split(r"(?<=[.!?;:])\s+|\n+", text):
        out.extend(_sentence_candidates(tokenize(sentence), evidence))
    return out


def _singular(word):
    """Very small plural folding so "trees" and "tree" count as one concept."""
    if len(word) <= 4 or not word.endswith("s") or word.endswith(("ss", "us", "is", "ous")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    return word[:-1]


def _sentence_candidates(words, evidence=None):
    words = [_singular(w) for w in words]
    out = []
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            if any(len(w) < 3 or w.isdigit() for w in gram):
                continue
            if n > 1 and any(w in STOPWORDS for w in gram[1:-1]):
                continue
            term = " ".join(gram)
            out.append(term)
            # Noun phrase evidence: the phrase follows an article somewhere ("a binary heap").
            if evidence is not None and i > 0 and words[i - 1] in DETERMINERS:
                evidence[term] += 1
    return out


def extract_keyphrases(texts, top_n=20, min_df=2):
    if not texts:
        return []
    doc_freq = Counter()
    term_freq = Counter()
    evidence = Counter()
    for text in texts:
        cands = _candidates(text, evidence)
        term_freq.update(cands)
        doc_freq.update(set(cands))

    n_docs = len(texts)
    required_df = min_df if n_docs >= 4 else 1
    scores = {}
    for term, tf in term_freq.items():
        df = doc_freq[term]
        if df < required_df:
            continue
        if term.count(" ") >= 2 and tf < 2:
            continue  # one-off three word windows are almost always noise
        idf = math.log((1 + n_docs) / (1 + df)) + 1
        length_boost = 1.0 + 0.25 * term.count(" ")
        noun_like = 1.0 if evidence[term] else 0.35
        scores[term] = (1 + math.log(tf)) * idf * length_boost * noun_like

    # When a longer phrase almost always appears wherever a shorter one does,
    # the longer one is the real concept ("binary search tree" vs "search tree").
    for term in sorted(scores, key=lambda t: -t.count(" ")):
        if " " not in term or term not in scores:
            continue
        for part in (term.rsplit(" ", 1)[0], term.split(" ", 1)[1]):
            if part in scores and term_freq[term] >= 2 and term_freq[term] >= 0.6 * term_freq[part]:
                scores[term] = max(scores[term], scores[part] * 1.05)
                del scores[part]

    # Drop words that are already covered by a stronger phrase, and replace a
    # chosen single word when a phrase containing it shows up later.
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    chosen = []
    for term, _ in ranked:
        words = term.split()
        if any(set(words) <= set(other.split()) for other in chosen):
            continue
        if len(words) > 1 and any(len(set(words) & set(other.split())) >= 2 for other in chosen):
            continue  # overlapping window of a phrase we already have
        if len(words) > 1:
            chosen = [o for o in chosen if not (" " not in o and o in words)]
        chosen.append(term)
        if len(chosen) >= top_n:
            break
    return chosen


def topic_pattern(topic):
    """Whole word match that also accepts the plural form."""
    return re.compile(rf"\b{re.escape(topic)}(?:s|es)?\b", re.I)


def topic_for(text, topics):
    """Pick the best matching known topic for a piece of text."""
    best, best_score = None, 0
    for topic in topics:
        if topic_pattern(topic).search(text):
            score = len(topic)
            if score > best_score:
                best, best_score = topic, score
    return best


def definitions(texts, terms):
    """Find a sentence that defines each term, like 'A heap is a tree based ...'."""
    found = defaultdict(str)
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            low = sentence.lower()
            for term in terms:
                if term in found:
                    continue
                pattern = rf"\b{re.escape(term)}(?:s|es)?\b\s+(is|are|refers to|means|describes|is defined as)\b"
                if re.search(pattern, low) and 30 <= len(sentence) <= 400:
                    found[term] = sentence.strip()
    return dict(found)
