from .identity import digest
from .tokenizer import tokenize


def chunks(messages, title, tool_limit=2000):
    pending, size = [], 0

    def pack(parts):
        body = "\n\n".join(p["text"] for p in parts)
        refs = [{k: p[k] for k in ("message_uid", "body_hash", "start_char", "end_char")} for p in parts]
        return dict(body=body, parts=refs, index_title=tokenize(title), index_body=tokenize(body),
                    index_terms=tokenize(" ".join(p["terms"] for p in parts)), chunk_hash=digest(body),
                    occurred_at=max((p["occurred_at"] for p in parts if p["occurred_at"]), default=None))

    for message in messages:
        if message["kind"] == "retrieved_context" or message["inherited"]:
            continue
        body = message["body"]
        ranges = [(0, len(body))]
        if message["role"] == "tool" and len(body) > tool_limit:
            half = tool_limit // 2
            ranges = [(0, half), (len(body) - half, len(body))]
        for first, last in ranges:
            start = first
            while start < last:
                end = min(start + 4000, last)
                if end < last:
                    cut = body.rfind("\n", start + 2000, end)
                    if cut > start:
                        end = cut + 1
                text = body[start:end]
                if pending and (size + len(text) > 4000 or pending[-1]["occurred_at"] != message["occurred_at"]):
                    yield pack(pending)
                    pending, size = [], 0
                pending.append(dict(message_uid=message["uid"], body_hash=message["body_hash"], start_char=start,
                                    end_char=end, text=text, occurred_at=message["occurred_at"],
                                    terms=text if message["role"] == "tool" else ""))
                size += len(text)
                if size >= 2000:
                    yield pack(pending)
                    pending, size = [], 0
                start = end if end == last else max(start + 1, end - 300)
    if pending:
        yield pack(pending)
