import re

from ..tokenizer import pre_tokenize
from .models import SessionError


def tokenize(text):
    return pre_tokenize(text) + " " + " ".join(re.findall(r"\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)*)\b", text))


def match_query(query):
    if not isinstance(query, str) or not 1 <= len(query.encode("utf-8")) <= 1024:
        raise SessionError("invalid_query")
    tokens = list(dict.fromkeys(tokenize(query).split()))
    if not tokens or len(tokens) > 64:
        raise SessionError("invalid_query")
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
