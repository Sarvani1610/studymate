"""Text helpers shared by ingestion, retrieval and the generators."""
import math
import re
import unicodedata

STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each either else etc even ever every few for
from further get gets got had has have having he her here hers herself him himself his how however i if in
into is it its itself just let may me might more most much must my myself no nor not now of off often on
once only or other our ours ourselves out over own per rather same she should so some such than that the
their theirs them themselves then there these they this those through thus to too under until up upon us
use used uses using very via was we were what when where which while who whom why will with within without
would yet you your yours yourself yourselves one two three first second new also like well many make made
figure fig section chapter page slide example examples e.g i.e see shown following given called
""".split())

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-']*[a-zA-Z0-9]|[a-zA-Z]")
_SENT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")
_ABBREV = ("e.g.", "i.e.", "etc.", "vs.", "Dr.", "Mr.", "Mrs.", "Ms.", "Fig.", "fig.", "No.", "al.")


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("­", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tokenize(text):
    out = []
    for w in _WORD_RE.findall(text or ""):
        w = w.lower()
        if w.endswith("'s"):
            w = w[:-2]
        w = w.strip("'")
        if w:
            out.append(w)
    return out


def content_words(text):
    return [w for w in tokenize(text) if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]


def estimate_tokens(text):
    # Rough rule of thumb for English with BPE tokenizers: about 4 chars per token.
    return max(1, math.ceil(len(text or "") / 4))


def split_sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    protected = text
    for i, abbr in enumerate(_ABBREV):
        protected = protected.replace(abbr, f"__ABBR{i}__")
    parts = _SENT_RE.split(protected)
    out = []
    for part in parts:
        for i, abbr in enumerate(_ABBREV):
            part = part.replace(f"__ABBR{i}__", abbr)
        part = part.strip()
        if part:
            out.append(part)
    return out


def truncate(text, limit=240):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def normalize_question(text):
    text = re.sub(r"\s+", " ", (text or "").strip().lower())
    return re.sub(r"[?!.]+$", "", text)


def ngrams(words, n):
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
