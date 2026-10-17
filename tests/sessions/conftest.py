import pytest

from bm25_search.sessions.search import CURRENT_SESSION_ENV


@pytest.fixture(autouse=True)
def no_calling_session(monkeypatch):
    # Tests often run inside an agent shell that exports its own session id.
    for name in CURRENT_SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
