from dataclasses import asdict

from .identity import digest, message_uid


def normalize(session, sid, config):
    result, seen = [], set()
    limit = config.data.get("tool_output", {}).get("store_limit_chars", 16000)
    for message in session.messages:
        if message.role not in ("user", "assistant", "tool"):
            continue
        # Do not deduplicate distinct repeated human messages by their body.
        native = str(message.native_id)
        if native in seen:
            session.diagnostics.append({"code": "duplicate_message_id"})
            continue
        seen.add(native)
        original = message.body
        body = config.replace(original)
        redacted = body != original
        truncated = message.truncated
        if message.role == "tool" and len(body) > limit:
            marker = "\n[output truncated]\n"
            half = (limit - len(marker)) // 2
            body = body[:half] + marker + body[-half:]
            truncated = True
        row = asdict(message)
        row.update(uid=message_uid(sid, native), body=body, body_hash=digest(body), redacted=redacted,
                   truncated=truncated, ordinal=len(result))
        result.append(row)
    return result
