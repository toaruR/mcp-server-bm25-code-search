"""Reproducible synthetic session benchmark, never imports real user logs."""
import argparse
import json
import platform
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bm25_search.sessions.config import Config
from bm25_search.sessions.db import Database
from bm25_search.sessions.identity import session_uid
from bm25_search.sessions.service import Service


def p95(values):
    return sorted(values)[min(len(values) - 1, int(len(values) * .95))]


def peak_memory():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in ("peak", "working", "quota_peak_paged", "quota_paged", "quota_peak_nonpaged", "quota_nonpaged", "pagefile", "peak_pagefile")]
        info = Counters()
        info.cb = ctypes.sizeof(info)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        return info.peak if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb) else None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=100000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.workdir = args.workdir.resolve()
    if args.workdir.exists() or args.messages < 30000:
        parser.error("workdir must be new; messages >= 30000")
    root, source = args.workdir / "project", args.workdir / "input"
    root.mkdir(parents=True)
    source.mkdir()
    cases = json.loads((Path(__file__).resolve().parents[1] / "tests/sessions/fixtures/retrieval_cases.json").read_text(encoding="utf-8"))
    config = Config(root)
    config.add_source("benchmark", "codex", [str(source)])
    registered = config.sources[0]
    per_session = args.messages // 100
    padding = "background trace auxiliary " * 25
    for index in range(100):
        path = source / ("session-%03d.jsonl" % index)
        with path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(type="session_meta", timestamp="2026-01-01T00:00:00Z", payload=dict(id="benchmark-%03d" % index, cwd=str(root))), ensure_ascii=False) + "\n")
            for ordinal in range(per_session):
                body = cases[index]["text"] if index < len(cases) and ordinal == per_session // 2 else padding + " record_%d_%d" % (index, ordinal)
                event = dict(type="response_item", timestamp="2026-01-01T00:00:00Z", payload=dict(type="message", id="m_%d" % ordinal, role="assistant" if ordinal % 2 else "user", content=[dict(type="output_text" if ordinal % 2 else "input_text", text=body)]))
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("Synthetic dataset generated", flush=True)
    service = Service(root, "codex")
    start = time.perf_counter()
    imported = service.sync()
    import_seconds = time.perf_counter() - start
    if imported["status"] != "succeeded":
        raise RuntimeError(imported)
    print("Import completed", flush=True)
    times, read_times, ratios, hits = [], [], [], 0
    for index, case in enumerate(cases):
        wanted = session_uid(registered, "benchmark-%03d" % index)
        for repeat in range(5):
            start = time.perf_counter()
            found = service.call("session_search", dict(query=case["query"], top_k=5))
            elapsed = (time.perf_counter() - start) * 1000
            if repeat > 0:
                times.append(elapsed)
            correct = next((r for r in found["results"] if r["session_id"] == wanted), None)
            if repeat == 0:
                hits += correct is not None
            if correct:
                start = time.perf_counter()
                read = service.call("session_read", dict(session_id=wanted, anchor=correct["anchor"]))
                elapsed = (time.perf_counter() - start) * 1000
                if repeat > 0:
                    read_times.append(elapsed)
                if repeat == 0:
                    with Database(root) as db:
                        baseline = sum(len(r[0].encode("utf-8")) for r in db.connection.execute("SELECT body FROM messages WHERE session_uid=?", (wanted,)))
                    returned = len(json.dumps([found, read], ensure_ascii=False).encode("utf-8"))
                    ratios.append(1 - returned / baseline)
    # Append exactly 100 messages to a long existing session.
    with (source / "session-000.jsonl").open("a", encoding="utf-8") as stream:
        for index in range(100):
            stream.write(json.dumps(dict(type="response_item", timestamp="2026-01-02T00:00:00Z", payload=dict(type="message", id="append_%d" % index, role="assistant", content=[dict(type="output_text", text="incremental benchmark " + str(index))]))) + "\n")
    start = time.perf_counter()
    incremental = service.sync()
    incremental_seconds = time.perf_counter() - start
    status = service.status()
    result = dict(dataset="synthetic shared-vocabulary distractors with 30 manually defined query targets", queries=len(cases), hit_at_5=hits / len(cases),
                  import_seconds=import_seconds, incremental_100_seconds=incremental_seconds, incremental_status=incremental["status"],
                  search_p95_ms=p95(times), read_p95_ms=p95(read_times), median_context_bytes_reduction=statistics.median(ratios),
                  messages=status["messages"], chunks=status["chunks"], database_bytes=(root / ".bm25_sessions.db").stat().st_size,
                  wal_bytes=(root / ".bm25_sessions.db-wal").stat().st_size if (root / ".bm25_sessions.db-wal").exists() else 0,
                  peak_process_memory_bytes=peak_memory(),
                  environment=dict(os=platform.system(), machine=platform.machine(), python=platform.python_version(), sqlite=sqlite3.sqlite_version),
                  limitation="Synthetic fixture quality is not a real-world relevance guarantee; latency measured after import, with repeated warm queries.")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
