import pytest
import subprocess
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bm25_search.indexer import (
    collect_files,
    filter_by_extension,
    chunk_text,
    chunk_lines,
    sync_index,
    build_index,
    copy_index_from_parent,
    warmstart_from_parent,
)


def test_git_file_collection_and_extension_filtering(tmp_path):
    # Setup mock git repo or directory walk
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "src" / "style.css").write_text("body { color: red; }", encoding="utf-8")
    (tmp_path / "src" / "binary.bin").write_bytes(b"\x00\x01\x02")

    files = collect_files(tmp_path)
    assert "src/main.py" in files
    assert "src/style.css" in files
    assert "src/binary.bin" not in files


def test_80_20_chunking_overlap():
    lines = [f"line {i}\n" for i in range(1, 151)]
    chunks = chunk_lines(lines, chunk_size=80, overlap=20)
    
    assert len(chunks) == 3
    assert chunks[0]["start_line"] == 1
    assert chunks[0]["end_line"] == 80

    assert chunks[1]["start_line"] == 61
    assert chunks[1]["end_line"] == 140

    assert chunks[2]["start_line"] == 121
    assert chunks[2]["end_line"] == 150


def test_chunk_boundary_snaps_forward_to_blank_line():
    # Ideal 80-line boundary lands mid-block; a blank line a few lines later
    # (line 83, 1-based) should pull the chunk end forward to just past it.
    lines = [f"line {i}\n" for i in range(1, 151)]
    lines[82] = "\n"  # 0-based index 82 == line 83 (1-based), within lookahead

    chunks = chunk_lines(lines, chunk_size=80, overlap=20)
    assert chunks[0]["end_line"] == 83
    assert lines[chunks[0]["end_line"] - 1].strip() == ""


def test_chunk_boundary_snap_can_be_disabled():
    lines = [f"line {i}\n" for i in range(1, 151)]
    lines[82] = "\n"

    chunks = chunk_lines(lines, chunk_size=80, overlap=20, snap_boundaries=False)
    assert chunks[0]["end_line"] == 80


def test_incremental_indexing_with_git_diff(tmp_path):
    (tmp_path / "app.py").write_text("def run(): pass\n", encoding="utf-8")
    db_path = tmp_path / ".bm25_index.db"

    rep1 = build_index(tmp_path, db_path=db_path)
    assert rep1["indexed"] >= 1

    # Modify file
    (tmp_path / "app.py").write_text("def run(): print('v2')\n", encoding="utf-8")
    rep2 = sync_index(tmp_path, db_path=db_path)
    assert rep2["updated"] >= 1


def test_worktree_warmstart_copy(tmp_path):
    parent = tmp_path / "parent"
    target = tmp_path / "target"
    parent.mkdir()
    target.mkdir()

    (parent / "a.py").write_text("x = 1\n", encoding="utf-8")
    (target / "a.py").write_text("x = 1\n", encoding="utf-8")

    build_index(parent)
    assert (parent / ".bm25_index.db").exists()

    res = warmstart_from_parent(parent, target)
    assert res["copied"] is True
    assert (target / ".bm25_index.db").exists()
