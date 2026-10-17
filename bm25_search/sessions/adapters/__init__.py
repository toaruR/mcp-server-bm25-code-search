def adapter_for(source):
    from . import agy, claude_code, codex, cursor
    return {"claude_code": claude_code, "codex": codex, "agy": agy, "cursor": cursor}[source.agent]
