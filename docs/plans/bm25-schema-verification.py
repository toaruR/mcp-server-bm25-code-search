import re
import sqlite3

con = sqlite3.connect(":memory:")
con.executescript(
    """
-- chunks: source of truth (chunk-level granularity, joined to FTS by rowid)
CREATE TABLE chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    raw_snippet TEXT NOT NULL,   -- original text, shown to the agent
    index_text TEXT NOT NULL     -- pre-tokenized text, fed to FTS5 only
);

-- FTS5 external-content table: rowid = chunks.chunk_id, no data duplication
CREATE VIRTUAL TABLE code_fts USING fts5(
    filepath,
    index_text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- keep FTS index in sync with chunks (standard external-content recipe)
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO code_fts(rowid, filepath, index_text) VALUES (new.chunk_id, new.filepath, new.index_text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO code_fts(code_fts, rowid, filepath, index_text) VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO code_fts(code_fts, rowid, filepath, index_text) VALUES ('delete', old.chunk_id, old.filepath, old.index_text);
  INSERT INTO code_fts(rowid, filepath, index_text) VALUES (new.chunk_id, new.filepath, new.index_text);
END;

CREATE TABLE file_metadata (
    filepath TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    file_hash TEXT NOT NULL
);
"""
)

# ---- pre-tokenization pipeline (Python-side, since stdlib sqlite3 can't
# register custom FTS5 tokenizers) ----

CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]+")
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[぀-ヿ㐀-鿿]+")


def split_identifier(word: str):
    """camelCase / snake_case -> subword tokens (keeps the original token too)."""
    parts = []
    for piece in word.split("_"):
        if not piece:
            continue
        subs = CAMEL_RE.findall(piece)
        parts.extend(s.lower() for s in subs if s)
    return parts


def cjk_bigrams(text: str):
    return [text[i:i + 2] for i in range(len(text) - 1)] if len(text) > 1 else [text]


def pre_tokenize(raw_text: str) -> str:
    tokens = []
    for m in WORD_RE.finditer(raw_text):
        w = m.group(0)
        if CJK_RE.fullmatch(w):
            tokens.extend(cjk_bigrams(w))
        else:
            tokens.append(w.lower())
            if "_" in w or re.search(r"[a-z][A-Z]", w):
                tokens.extend(split_identifier(w))
    return " ".join(tokens)


# ---- insert test data ----
samples = [
    ("src/auth/userProfile.ts", 1, 80,
     "export function getUserProfile(userId: string) { /* fetch user_profile record */ }"),
    ("src/auth/session_token.py", 1, 80,
     "def validate_session_token(token): pass  # 有効期限を検証する"),
    ("docs/spec.md", 1, 80,
     "アクセストークンの有効期限はログイン後に更新される仕様です。"),
]

for filepath, s, e, raw in samples:
    con.execute(
        "INSERT INTO chunks (filepath, start_line, end_line, raw_snippet, index_text) VALUES (?,?,?,?,?)",
        (filepath, s, e, raw, pre_tokenize(filepath) + " " + pre_tokenize(raw)),
    )
con.commit()

def build_match_expr(raw_query: str, mode: str = "OR") -> str:
    """Apply the SAME pre-tokenizer to the query, then join as an FTS5 MATCH expr.
    Without this, a raw multi-char CJK query never matches the 2-gram index,
    because unicode61 tokenizes '有効期限' as ONE token on the query side too."""
    tokens = pre_tokenize(raw_query).split()
    if not tokens:
        return raw_query
    quoted = [f'"{t}"' for t in tokens]
    return f" {mode} ".join(quoted)


def search(raw_query, top_k=5, mode="OR"):
    query = build_match_expr(raw_query, mode)
    print(f"\n--- query: {raw_query!r} -> MATCH {query!r} ---")
    rows = con.execute(
        """
        SELECT c.filepath, c.start_line, c.end_line, c.raw_snippet,
               bm25(code_fts, 3.0, 1.0) AS score
        FROM code_fts
        JOIN chunks c ON c.chunk_id = code_fts.rowid
        WHERE code_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """,
        (query, top_k),
    ).fetchall()
    for r in rows:
        print(r)
    if not rows:
        print("ZERO MATCH")

# 1) subword match: query "profile" should hit getUserProfile via camelCase split
search("profile")
# 2) snake_case subword match
search("token")
# 3) Japanese multi-char query -> must be pre-tokenized into bigrams on the query side too
search("有効期限")
search("期限")
# 4) path boost: query matching only the filename should still surface it near top
search("userProfile")

# 5) test update trigger: change a chunk's content, confirm old text no longer matches
con.execute("UPDATE chunks SET raw_snippet=?, index_text=? WHERE chunk_id=1",
            ("renamed, no longer about profiles", pre_tokenize("renamed, no longer about profiles")))
con.commit()
search("profile")

# 6) test delete trigger
con.execute("DELETE FROM chunks WHERE chunk_id=2")
con.commit()
search("token")

print("\nOK: schema + triggers + pre-tokenization pipeline verified.")
