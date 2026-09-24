from assistant import keys
from config import settings


def test_existing_key_survives_login_secret_rotation(monkeypatch):
    monkeypatch.setattr(settings, 'assistant_key_secret', '')
    monkeypatch.setattr(settings, 'jwt_secret', 'original-test-signing-secret')
    encrypted = keys.encrypt('test-provider-key-never-real')
    monkeypatch.setattr(settings, 'assistant_key_secret', 'original-test-signing-secret')
    monkeypatch.setattr(settings, 'jwt_secret', 'rotated-test-signing-secret')
    assert keys.decrypt(encrypted) == 'test-provider-key-never-real'
    assert keys.decrypt(keys.encrypt('new-test-provider-key')) == 'new-test-provider-key'


def test_wrong_configured_secret_fails_closed_without_login_fallback(monkeypatch):
    monkeypatch.setattr(settings, 'assistant_key_secret', '')
    monkeypatch.setattr(settings, 'jwt_secret', 'original-test-secret')
    encrypted = keys.encrypt('test-provider-key')
    monkeypatch.setattr(settings, 'assistant_key_secret', 'different-test-secret')
    assert keys.decrypt(encrypted) is None
    assert keys.last_four(encrypted) is None


def test_same_persistent_secret_decrypts_across_new_fernet_instances(monkeypatch):
    monkeypatch.setattr(settings, 'assistant_key_secret', 'persistent-test-secret')
    encrypted = keys.encrypt('test-provider-key')
    assert keys.decrypt(encrypted) == 'test-provider-key'
    assert keys.decrypt('corrupted-ciphertext') is None
    assert keys.decrypt(None) is None
