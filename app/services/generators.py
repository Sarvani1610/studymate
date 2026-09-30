"""Flashcard and quiz generation.

Both use the LLM in JSON mode when a generative provider is configured. With
the stub provider they fall back to heuristics that still produce reasonable
cards: definition sentences become "What is X?" cards, and key sentences become
cloze style multiple choice questions with distractors drawn from other
key phrases in the same course.
"""
import logging
import random
import re

from ..errors import UpstreamError, ValidationError
from ..models import Chunk, Document
from ..utils.text import split_sentences, truncate
from . import quota
from .keywords import definitions, extract_keyphrases, topic_for, topic_pattern
from .llm import get_llm
from .prompts import FLASHCARD_SYSTEM, FLASHCARD_USER, QUIZ_SYSTEM, QUIZ_USER

log = logging.getLogger(__name__)
MATERIAL_TOKENS = 3000


def load_chunks(course_id, document_ids=None, topics=None, limit=60):
    query = Chunk.query.join(Document).filter(Chunk.course_id == course_id, Document.status == "ready")
    if document_ids:
        query = query.filter(Chunk.document_id.in_(document_ids))
    chunks = query.order_by(Chunk.document_id, Chunk.ordinal).all()
    if topics:
        lowered = [t.lower() for t in topics]
        focused = [c for c in chunks if any(t in c.text.lower() for t in lowered)]
        if focused:
            chunks = focused
    if not chunks:
        raise ValidationError("No processed material found. Upload a document and wait for it to finish.")
    if len(chunks) > limit:
        step = len(chunks) / limit
        chunks = [chunks[int(i * step)] for i in range(limit)]  # even spread across the material
    return chunks


def _material(chunks, budget=MATERIAL_TOKENS):
    parts, used = [], 0
    for chunk in chunks:
        if parts and used + chunk.token_count > budget:
            break
        parts.append(chunk.text)
        used += chunk.token_count
    return "\n\n".join(parts)


def _clean(text, limit):
    return truncate(re.sub(r"\s+", " ", str(text or "")).strip(), limit)


# ------------------------------------------------------------------ flashcards

def generate_flashcards(user, chunks, count=10):
    llm = get_llm()
    texts = [c.text for c in chunks]
    topics = extract_keyphrases(texts, top_n=40)
    tokens = 0

    cards = []
    if llm.generative:
        material = _material(chunks)
        quota.ensure_budget(user, len(material) // 4)
        data, result = llm.complete_json(FLASHCARD_SYSTEM, FLASHCARD_USER.format(count=count, text=material),
                                         task="flashcards")
        tokens = result.tokens_in + result.tokens_out
        for item in (data.get("cards") or [])[:count]:
            front, back = _clean(item.get("front"), 300), _clean(item.get("back"), 600)
            if front and back:
                cards.append({"front": front, "back": back,
                              "topic": _clean(item.get("topic"), 120) or topic_for(front + " " + back, topics)})
    else:
        defs = definitions(texts, topics)
        for term, sentence in defs.items():
            article = "an" if term[0] in "aeiou" else "a"
            cards.append({"front": f"What is {article} {term}?", "back": sentence, "topic": term})
        if len(cards) < count:
            for card in _cloze_cards(chunks, topics, count - len(cards)):
                if card["front"] not in {c["front"] for c in cards}:
                    cards.append(card)
    quota.record(user.id, tokens)
    return cards[:count], tokens


def _cloze_cards(chunks, topics, count):
    out = []
    for chunk in chunks:
        for sentence in split_sentences(chunk.text):
            if not 50 <= len(sentence) <= 300:
                continue
            topic = topic_for(sentence, topics)
            if not topic:
                continue
            blanked = topic_pattern(topic).sub("_____", sentence, count=1)
            if blanked == sentence:
                continue
            out.append({"front": f"Fill in the blank: {blanked}", "back": topic, "topic": topic,
                        "chunk_id": chunk.id})
            break
        if len(out) >= count:
            break
    return out


# -------------------------------------------------------------------- quizzes

def generate_quiz(user, chunks, count=5, difficulty="medium", focus_topics=None, seed=None):
    llm = get_llm()
    texts = [c.text for c in chunks]
    topics = extract_keyphrases(texts, top_n=40)
    tokens = 0
    questions = []

    if llm.generative:
        material = _material(chunks)
        focus_line = f"Focus on: {', '.join(focus_topics)}." if focus_topics else ""
        quota.ensure_budget(user, len(material) // 4)
        data, result = llm.complete_json(
            QUIZ_SYSTEM,
            QUIZ_USER.format(count=count, difficulty=difficulty, focus_line=focus_line, text=material),
            task="quiz",
        )
        tokens = result.tokens_in + result.tokens_out
        for item in data.get("questions") or []:
            q = _validate_question(item, topics)
            if q:
                questions.append(q)
    else:
        questions = _cloze_questions(chunks, topics, count, difficulty, focus_topics, seed)

    if not questions:
        raise UpstreamError("Could not generate questions from this material. Try a longer document.")
    quota.record(user.id, tokens)
    for index, q in enumerate(questions[:count]):
        q["id"] = index
    return questions[:count], tokens


def _validate_question(item, topics):
    try:
        options = [_clean(o, 200) for o in item["options"]]
        answer = int(item["answer_index"])
        question = _clean(item["question"], 400)
    except (KeyError, TypeError, ValueError):
        return None
    if len(options) != 4 or not 0 <= answer < 4 or not question or len(set(options)) != 4:
        return None
    return {
        "question": question,
        "options": options,
        "answer_index": answer,
        "explanation": _clean(item.get("explanation"), 600),
        "topic": _clean(item.get("topic"), 120) or topic_for(question, topics) or "general",
    }


def _cloze_questions(chunks, topics, count, difficulty, focus_topics, seed):
    rng = random.Random(seed)
    if len(topics) < 4:
        raise ValidationError("Not enough distinct concepts in this material to build a quiz yet.")
    preferred = [t.lower() for t in (focus_topics or [])]
    candidates = []
    for chunk in chunks:
        for sentence in split_sentences(chunk.text):
            if not 50 <= len(sentence) <= 320:
                continue
            topic = topic_for(sentence, topics)
            if topic:
                boost = 1 if any(p in sentence.lower() for p in preferred) else 0
                candidates.append((boost, rng.random(), topic, sentence))
    candidates.sort(reverse=True)

    questions, used_topics, used_sentences = [], set(), set()
    for _, _, topic, sentence in candidates:
        if topic in used_topics or sentence in used_sentences:
            continue
        stem = topic_pattern(topic).sub("_____", sentence, count=1)
        if stem == sentence:
            continue
        pool = [t for t in topics if t != topic and t not in topic and topic not in t]
        # Distractors with the same shape as the answer (word count) are harder to rule out by
        # length alone. Hard quizzes also prefer ones that share a word with the answer.
        same_shape = [t for t in pool if t.count(" ") == topic.count(" ")]
        if len(same_shape) >= 3:
            pool = same_shape
        if difficulty == "hard":
            related = [t for t in pool if set(t.split()) & set(topic.split())]
            if len(related) >= 3:
                pool = related
        if len(pool) < 3:
            continue
        distractors = rng.sample(pool[:15], 3) if len(pool) >= 15 else rng.sample(pool, 3)
        options = distractors + [topic]
        rng.shuffle(options)
        questions.append({
            "question": f"Which term best completes this statement? {stem}",
            "options": options,
            "answer_index": options.index(topic),
            "explanation": sentence,
            "topic": topic,
        })
        used_topics.add(topic)
        used_sentences.add(sentence)
        if len(questions) >= count:
            break
    return questions


def grade(quiz, answers):
    """answers is a list of {"question_id": int, "choice": int}."""
    by_id = {a["question_id"]: a.get("choice") for a in answers}
    breakdown, score = [], 0
    for q in quiz.questions:
        choice = by_id.get(q["id"])
        correct = choice == q["answer_index"]
        score += 1 if correct else 0
        breakdown.append({
            "question_id": q["id"],
            "choice": choice,
            "correct": correct,
            "answer_index": q["answer_index"],
            "topic": q.get("topic") or "general",
            "explanation": q.get("explanation"),
        })
    return score, breakdown
