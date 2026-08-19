import json
import pytest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.search import (
    init_db,
    insert_chunk,
    search,
    search_multi,
    compute_confidence,
    low_confidence_hint,
    truncate_bytes,
    zero_match_fallback,
    format_results,
    format_json,
    main,
)


def test_bm25_search_scoring_and_path_boost():
    conn = init_db(":memory:")
    try:
        # File 1: match in body
        insert_chunk(conn, "src/util.py", 1, 10, "def get_user_profile(): return 1")
        # File 2: match in filepath (should be boosted)
        insert_chunk(conn, "src/user_profile.py", 1, 10, "def run(): pass")

        results = search(conn, "user_profile", top_k=5)
        assert len(results) == 2
        # Path match should rank first (lower/more negative score in BM25)
        assert results[0]["filepath"] == "src/user_profile.py"
    finally:
        conn.close()


def test_max_bytes_truncation():
    # Test Japanese multibyte truncation safety
    text = "あいうえおかきくけこ"  # Each CJK character is 3 bytes in UTF-8
    truncated = truncate_bytes(text, max_bytes=10)
    assert len(truncated.encode("utf-8")) <= 10
    # Must not raise UnicodeDecodeError or return invalid surrogate
    truncated.encode("utf-8").decode("utf-8")


def test_zero_match_fallback_response():
    fallback = zero_match_fallback(query="nonexistent_symbol_12345")
    assert fallback["status"] == "zero_match"
    assert "Recommended Fallback" in fallback["message"]
    assert fallback["query"] == "nonexistent_symbol_12345"


def test_cli_output_formats(tmp_path, capsys):
    db_path = tmp_path / "test.db"
    conn = init_db(str(db_path))
    insert_chunk(conn, "src/db.py", 1, 10, "def init_db(): pass")
    conn.close()

    # Test Markdown output via main CLI
    rc = main(["--db", str(db_path), "init_db", "--format", "markdown"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "# BM25 Search Results" in captured.out
    assert "src/db.py" in captured.out

    # Test JSON output via main CLI
    rc_json = main(["--db", str(db_path), "init_db", "--format", "json"])
    assert rc_json == 0
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert data["status"] == "ok"
    assert data["count"] >= 1


def test_search_multi_single_query_matches_plain_search():
    conn = init_db(":memory:")
    try:
        insert_chunk(conn, "src/util.py", 1, 10, "def get_user_profile(): return 1")
        insert_chunk(conn, "src/user_profile.py", 1, 10, "def run(): pass")

        plain = search(conn, "user_profile", top_k=5)
        multi = search_multi(conn, ["user_profile"], top_k=5)
        assert multi == plain
        assert "fusion_score" not in multi[0]
    finally:
        conn.close()


def test_search_multi_rrf_merges_and_ranks_convergent_hits():
    conn = init_db(":memory:")
    try:
        # Hit by both queries -> should be fused to the top.
        insert_chunk(conn, "src/auth.py", 1, 10,
                     "def authenticate_user(): validate_session_token()")
        # Hit by only the first query.
        insert_chunk(conn, "src/other_auth.py", 1, 10,
                     "def authenticate_user(): pass")
        # Hit by only the second query.
        insert_chunk(conn, "src/other_session.py", 1, 10,
                     "def validate_session_token(): pass")

        merged = search_multi(conn, ["authenticate_user", "session_token"], top_k=5)
        assert len(merged) == 3
        # The chunk hit by both queries must rank first and carry both hits.
        assert merged[0]["filepath"] == "src/auth.py"
        assert merged[0]["matched_queries"] == ["authenticate_user", "session_token"]
        assert merged[0]["fusion_score"] > merged[1]["fusion_score"]
    finally:
        conn.close()


def test_search_multi_deduplicates_and_ignores_blank_queries():
    conn = init_db(":memory:")
    try:
        insert_chunk(conn, "src/util.py", 1, 10, "def get_user_profile(): return 1")
        # Same query repeated + a blank entry should collapse to a single-query
        # (non-fused) call.
        result = search_multi(conn, ["user_profile", "  ", "user_profile"], top_k=5)
        assert result == search(conn, "user_profile", top_k=5)
    finally:
        conn.close()


def test_search_multi_empty_queries_returns_empty():
    conn = init_db(":memory:")
    try:
        assert search_multi(conn, ["", "   "], top_k=5) == []
    finally:
        conn.close()


def test_compute_confidence_labels():
    assert compute_confidence([])["label"] == "none"

    one = [{"score": -10.0}]
    assert compute_confidence(one)["label"] == "single"

    dominant = [{"score": -10.0}, {"score": -2.0}]
    conf = compute_confidence(dominant)
    assert conf["label"] == "dominant"
    assert conf["score_gap"] == pytest.approx(8.0)

    close = [{"score": -10.0}, {"score": -9.0}]
    assert compute_confidence(close)["label"] == "close_contest"


def test_compute_confidence_uses_fusion_score_when_present():
    # Raw bm25 'score' ordering does NOT match fusion_score ordering here,
    # which is realistic: fusion_score should still drive the label.
    fused = [
        {"score": -1.0, "fusion_score": 0.03},
        {"score": -5.0, "fusion_score": 0.01},
    ]
    conf = compute_confidence(fused)
    assert conf["label"] == "dominant"
    # top_score/runner_up_score still report the raw bm25 values verbatim.
    assert conf["top_score"] == -1.0
    assert conf["runner_up_score"] == -5.0


def test_search_diversify_thins_adjacent_same_file_chunks():
    conn = init_db(":memory:")
    try:
        # Adjacent overlap-derived cluster (start_line differs by < 60).
        insert_chunk(conn, "src/big.py", 1, 80, "def widget_handler(): pass")
        insert_chunk(conn, "src/big.py", 31, 110, "def widget_handler(): pass")
        # Far away in the same file -> not part of the cluster, kept.
        insert_chunk(conn, "src/big.py", 200, 280, "def widget_handler(): pass")
        # Different file -> always kept regardless of adjacency.
        insert_chunk(conn, "src/other.py", 1, 80, "def widget_handler(): pass")

        results = search(conn, "widget_handler", top_k=5, diversify=True)
        assert len(results) == 3
        same_file_starts = [r["start_line"] for r in results if r["filepath"] == "src/big.py"]
        assert len(same_file_starts) == 2
        assert not (1 in same_file_starts and 31 in same_file_starts)

        results_raw = search(conn, "widget_handler", top_k=5, diversify=False)
        assert len(results_raw) == 4
    finally:
        conn.close()


def test_low_confidence_hint_none_when_strong_and_sufficient():
    results = [{"score": -10.0}, {"score": -8.0}]
    assert low_confidence_hint(results, top_k=2) is None


def test_low_confidence_hint_sparse_hit_count():
    results = [{"score": -10.0}, {"score": -9.0}]
    assert low_confidence_hint(results, top_k=10) is not None


def test_low_confidence_hint_weak_top_score():
    results = [{"score": -0.5}]
    assert low_confidence_hint(results, top_k=1) is not None


def test_low_confidence_hint_empty_results_is_none():
    assert low_confidence_hint([]) is None


def test_format_json_includes_low_confidence_hint_key():
    conn = init_db(":memory:")
    try:
        insert_chunk(conn, "src/util.py", 1, 10, "def get_user_profile(): return 1")
        results = search(conn, "user_profile", top_k=5)
        payload = json.loads(format_json(results, query="user_profile", top_k=5))
        assert "low_confidence_hint" in payload
    finally:
        conn.close()


def test_format_json_includes_confidence():
    conn = init_db(":memory:")
    try:
        insert_chunk(conn, "src/util.py", 1, 10, "def get_user_profile(): return 1")
        insert_chunk(conn, "src/user_profile.py", 1, 10, "def run(): pass")
        results = search(conn, "user_profile", top_k=5)
        payload = json.loads(format_json(results, query="user_profile"))
        assert "confidence" in payload
        assert payload["confidence"]["label"] in ("dominant", "close_contest")
    finally:
        conn.close()
