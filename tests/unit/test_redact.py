"""Redacting writer (plan P1, review F-005; patterns per BOOTSTRAP-NOTES #7)."""

import json

from gauntlet.logging.redact import RedactingWriter, Redactor


def redactor(env=None):
    return Redactor(env=env or {})


def test_env_secret_value_masked_with_name():
    r = Redactor(env={"OPENAI_API_KEY": "supersecretvalue123"})
    text, hits = r.redact("calling api with supersecretvalue123 now")
    assert "supersecretvalue123" not in text
    assert "[REDACTED:env:OPENAI_API_KEY]" in text
    assert hits[0].pattern == "env:OPENAI_API_KEY"
    assert hits[0].count == 1


def test_short_env_value_not_masked():
    # PASSWORD=dev must not nuke every 'dev' in a transcript
    r = Redactor(env={"PASSWORD": "dev"})
    text, hits = r.redact("dev tooling for devs")
    assert text == "dev tooling for devs"
    assert hits == []


def test_non_secret_env_name_not_masked():
    r = Redactor(env={"HOME": "/Users/somebody/longpath"})
    text, hits = r.redact("path is /Users/somebody/longpath")
    assert "/Users/somebody/longpath" in text
    assert hits == []


def test_sk_pattern_requires_word_boundary_and_length():
    # The BOOTSTRAP-NOTES #7 regression: naive `sk-` matched "ask-with-warning"
    r = redactor()
    text, hits = r.redact("the fallback is ask-with-warning behavior")
    assert text == "the fallback is ask-with-warning behavior"
    assert hits == []
    # short sk- prefixes stay (not plausibly a key)
    text, hits = r.redact("see sk-12 in the diagram")
    assert "sk-12" in text


def test_real_looking_keys_are_masked():
    r = redactor()
    cases = {
        "sk-" + "a1B2" * 8: "openai-style-key",
        "sk-ant-" + "a1B2" * 8: "anthropic-key",
        "ghp_" + "A" * 36: "github-token",
        "github_pat_" + "A" * 30: "github-pat",
        "glpat-" + "x" * 20: "gitlab-token",
        "AKIA" + "Z" * 16: "aws-access-key-id",
        "xoxb-12345678901234567890": "slack-token",
    }
    for secret, pattern_name in cases.items():
        text, hits = r.redact(f"token is {secret} ok")
        assert secret not in text, pattern_name
        assert f"[REDACTED:{pattern_name}]" in text
        assert any(h.pattern == pattern_name for h in hits)


def test_private_key_block_masked():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\nmore\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text, hits = redactor().redact(f"dump:\n{pem}\nend")
    assert "MIIEowIBAAKC" not in text
    assert "[REDACTED:private-key-block]" in text


def test_env_masking_runs_before_patterns():
    # An env secret that also matches a generic pattern reports the env name,
    # which is the more diagnosable label.
    secret = "sk-" + "q" * 24
    r = Redactor(env={"MY_API_KEY": secret})
    text, hits = r.redact(f"using {secret}")
    assert "[REDACTED:env:MY_API_KEY]" in text
    assert hits[0].pattern == "env:MY_API_KEY"


def test_writer_write_text_creates_dirs_and_redacts(tmp_path):
    writer = RedactingWriter(redactor())
    target = tmp_path / "deep" / "nested" / "transcript.md"
    secret = "ghp_" + "B" * 36
    hits = writer.write_text(target, f"pushed with {secret}")
    assert target.read_text() == "pushed with [REDACTED:github-token]"
    assert hits[0].pattern == "github-token"
    assert writer.hits_log[0][0] == target


def test_writer_append_jsonl_redacts_values(tmp_path):
    writer = RedactingWriter(Redactor(env={"NPM_TOKEN": "tok_abcdefgh12345678"}))
    target = tmp_path / "events.jsonl"
    writer.append_jsonl(target, {"cmd": "publish", "env": "tok_abcdefgh12345678"})
    writer.append_jsonl(target, {"cmd": "clean"})
    lines = target.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["env"] == "[REDACTED:env:NPM_TOKEN]"
    assert json.loads(lines[1]) == {"cmd": "clean"}


# F-004 (P1 review round 1): writers serialize before redacting, and
# json.dumps escapes quotes/backslashes/newlines — the escaped form must
# also be masked.
def test_json_escaped_secret_variants_masked(tmp_path):
    secrets = {
        "QUOTE_TOKEN": 'abc"def"ghi12345',
        "BACKSLASH_TOKEN": "abc\\def\\ghi12345",
        "NEWLINE_TOKEN": "abc\ndef\nghi12345",
    }
    for name, value in secrets.items():
        writer = RedactingWriter(Redactor(env={name: value}))
        target = tmp_path / f"{name}.jsonl"
        writer.append_jsonl(target, {"payload": value})
        line = target.read_text()
        assert "def" not in line, name  # no fragment of the secret survives
        assert json.loads(line)["payload"] == f"[REDACTED:env:{name}]"


def test_escaped_variant_hits_aggregate_per_env_name():
    secret = 'top"secret"value99'
    r = Redactor(env={"MY_KEY": secret})
    # raw and JSON-escaped forms in the same text -> one aggregated hit entry
    text, hits = r.redact(f"raw={secret} escaped={json.dumps(secret)}")
    assert secret not in text
    assert '\\"' not in text
    assert len([h for h in hits if h.pattern == "env:MY_KEY"]) == 1
    assert hits[0].count == 2


def test_writer_no_hits_for_clean_text(tmp_path):
    writer = RedactingWriter(redactor())
    hits = writer.write_text(tmp_path / "clean.md", "nothing secret here")
    assert hits == []
    assert writer.hits_log == []
