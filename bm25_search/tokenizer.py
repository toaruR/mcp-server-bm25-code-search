"""Pre-tokenization pipeline for the BM25 code-search index.

This module implements the Python-side pre-tokenizer described in the design
spec (section 4.3).  It exists because the Python standard-library ``sqlite3``
module cannot register a custom FTS5 tokenizer, so CamelCase / snake_case
splitting and CJK 2-gram expansion must be performed in Python *before* the
text is handed to the stock ``unicode61`` tokenizer.

Public surface
--------------
* :func:`split_identifier` -- camelCase / snake_case -> subword tokens.
* :func:`cjk_bigrams`      -- Japanese (CJK) run -> 2-gram tokens.
* :func:`pre_tokenize`     -- full pipeline, returns space-joined tokens.
* :func:`pre_tokenize_query` -- same pipeline + OR-join for an FTS5 MATCH expr.

The implementation mirrors the verified design-spec pipeline (the same regexes
and ordering used in the schema-verification script and ``bm25_search/db.py``).
"""

from __future__ import annotations

import re

# --- character-class regexes (verified design-spec pipeline) -----------------

# Splits a single identifier piece into subwords:
#   getUserProfile -> ['get', 'User', 'Profile']
#   session_token -> ['session', 'token']
#   v2API         -> ['v', '2', 'API']
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Matches a run of CJK / kana characters (hiragana, katakana, CJK ideographs).
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]+")

# Splits raw text into tokens: ASCII identifiers (incl. digits/underscore)
# or contiguous CJK runs.  Punctuation / whitespace act as separators.
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[぀-ヿ㐀-鿿]+")


def split_identifier(word: str) -> list[str]:
    """camelCase / snake_case -> lower-cased subword tokens (originals kept).

    The original *word* is always emitted separately by :func:`pre_tokenize`;
    this helper returns only the subword pieces.
    """
    parts: list[str] = []
    for piece in word.split("_"):
        if not piece:
            continue
        subs = _CAMEL_RE.findall(piece)
        parts.extend(s.lower() for s in subs if s)
    return parts


def cjk_bigrams(text: str) -> list[str]:
    """Japanese (CJK) 2-gram.  A single character passes through unchanged."""
    if len(text) <= 1:
        return [text]
    return [text[i:i + 2] for i in range(len(text) - 1)]


def pre_tokenize(raw_text: str) -> str:
    """Tokenize *raw_text* into whitespace-joined tokens suitable for FTS5.

    * ASCII identifiers keep their original (lower-cased) form and gain
      subword tokens (camelCase / snake_case split).
    * Runs of CJK characters are expanded into 2-grams.

    Example::

        >>> pre_tokenize("getUserProfile 有効期限")
        'getuserprofile get user profile 有効 効期 期限'
    """
    tokens: list[str] = []
    for m in _WORD_RE.finditer(raw_text):
        w = m.group(0)
        if _CJK_RE.fullmatch(w):
            tokens.extend(cjk_bigrams(w))
        else:
            tokens.append(w.lower())
            if "_" in w or re.search(r"[a-z][A-Z]", w):
                tokens.extend(split_identifier(w))
    return " ".join(tokens)


def pre_tokenize_query(raw_query: str) -> str:
    """Apply :func:`pre_tokenize` to a query and build an OR-joined MATCH expr.

    A raw multi-character CJK query never matches a 2-gram index unless the
    query side is tokenized the same way (``unicode61`` tokenizes the query
    too, turning ``有効期限`` into a single token).  We therefore pre-tokenize
    the query and join every resulting token with ``OR``, quoting each token so
    the output is directly usable as an FTS5 ``MATCH`` condition::

        >>> pre_tokenize_query("有効期限")
        '\"有効\" OR \"効期\" OR \"期限\"'

    The default join is OR (not AND) because AND would collapse recall too
    aggressively; callers that need exact phrasing can post-process the result.
    """
    tokens = pre_tokenize(raw_query).split()
    if not tokens:
        return raw_query
    quoted = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted)
