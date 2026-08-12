import pytest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.tokenizer import split_identifier, cjk_bigrams, pre_tokenize, pre_tokenize_query


def test_camel_case_and_snake_case_splitting():
    # Test camelCase splitting
    subs1 = split_identifier("getUserProfile")
    assert "get" in subs1
    assert "user" in subs1
    assert "profile" in subs1

    # Test snake_case splitting
    subs2 = split_identifier("session_token")
    assert "session" in subs2
    assert "token" in subs2

    # Test full pre_tokenize on mixed identifiers
    res = pre_tokenize("getUserProfile session_token")
    assert "getuserprofile" in res
    assert "get" in res
    assert "user" in res
    assert "profile" in res
    assert "session_token" in res
    assert "session" in res
    assert "token" in res


def test_cjk_2gram_tokenization():
    bigrams = cjk_bigrams("有効期限")
    assert bigrams == ["有効", "効期", "期限"]

    # Test single character
    assert cjk_bigrams("有") == ["有"]

    # Test full pre_tokenize on Japanese text
    res = pre_tokenize("有効期限")
    assert "有効" in res
    assert "効期" in res
    assert "期限" in res


def test_query_pre_tokenization_or_join():
    q1 = pre_tokenize_query("有効期限")
    assert '"有効"' in q1
    assert '"効期"' in q1
    assert '"期限"' in q1
    assert " OR " in q1

    q2 = pre_tokenize_query("getUserProfile")
    assert '"getuserprofile"' in q2
    assert '"get"' in q2
    assert '"user"' in q2
    assert '"profile"' in q2
    assert " OR " in q2
