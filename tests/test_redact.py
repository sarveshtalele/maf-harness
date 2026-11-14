from aqh import redact as r


def test_secret_shapes_are_masked_and_words_are_not():
    openai = "sk-" + "FAKE" * 6
    github = "ghp_" + "FAKE" * 9
    text = f"key {openai} token {github} password=hunter2hunter2 normal words: admin password policy"
    out = r.redact(text)
    for secret in (openai, github, "hunter2hunter2"):
        assert secret not in out
    assert "sk-F" in out and "password=***" in out  # first four characters stay so the model can tell what it was
    assert "normal words: admin password policy" in out


def test_registered_secret_is_masked_everywhere_and_short_ones_are_ignored(monkeypatch):
    monkeypatch.setattr(r, "_known_secrets", set())
    r.register_secret("a-registered-value-123")
    r.register_secret("admin")  # shorter than MIN_MASKED: would corrupt ordinary words
    out = r.redact("x a-registered-value-123 y admin z")
    assert out == "x *** y admin z"
    assert r.MIN_MASKED == 12


def test_ordinary_text_that_looks_like_a_key_or_a_login_field_survives():
    for text in ("task-runner-configuration.ts", "disk-usage-analyzer-report", "PWD=/Users/me/repo",
                 "def login(password: str = None):", "my-ghp_ish-name", "skeleton-key-management-service-notes"):
        assert r.redact(text) == text, text
    assert "hunter2" not in r.redact('password="hunter2hunter2"')
    assert r.redact("password=hunter2hunter2") == "password=***"
    assert "hunter2" not in r.redact('{"password": "hunter2hunter2"}')
    assert "AKIA" + "A" * 16 not in r.redact("id=" + "AKIA" + "A" * 16)
