import json

import pytest

from data.dhanlive import auth


def test_a_non_owner_force_refresh_leaves_the_cached_token_alone(monkeypatch, tmp_path):
    cache = tmp_path / "dhan_access_token.json"
    cache.write_text(json.dumps({"accessToken": "saved-by-the-auth-job", "expiryTime": "2099-01-01T00:00:00"}))
    monkeypatch.setattr(auth, "DEFAULT_TOKEN_CACHE", cache)
    monkeypatch.setattr(auth.clear_cached_access_token, "__defaults__", (cache,))
    monkeypatch.setattr(auth.load_cached_access_token, "__defaults__", (cache,))
    monkeypatch.setattr(auth.load_cached_access_token_payload, "__defaults__", (cache,))
    monkeypatch.setattr(auth, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(auth, "is_consent_owner", lambda: False)
    monkeypatch.setattr(auth, "_record_dhan_auth_fallback", lambda **k: None)
    monkeypatch.setattr(auth, "get_token_id_from_auto_login", lambda: pytest.fail("a non-owner must never log in"))
    # the caller's failing token IS the cached one: the dead-token path
    with pytest.raises(auth.DhanAuthError):
        auth.force_refresh_access_token(current_token="saved-by-the-auth-job")
    assert cache.exists(), "a process that may not log in must not delete the token"


def test_cache_write_is_atomic(tmp_path):
    cache = tmp_path / "dhan_access_token.json"
    auth.cache_access_token({"accessToken": "t", "expiryTime": "2099-01-01T00:00:00"}, cache)
    assert json.loads(cache.read_text())["accessToken"] == "t"
    assert not list(tmp_path.glob(".*.tmp")), "no temp file left behind"
