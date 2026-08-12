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
    truncate_bytes,
    zero_match_fallback,
    format_results,
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
