from drift_agent.redaction import (
    REDACTED,
    hash_payload,
    is_sensitive_name,
    redact_mapping,
    redact_text,
)


def test_sensitive_names_are_detected():
    for name in ("password", "client_secret", "storage_key", "connection_string", "api_key", "sas_token"):
        assert is_sensitive_name(name) is True, name


def test_harmless_names_are_not_flagged():
    for name in ("tags", "key_vault_id", "owner", "sku_name", "location"):
        assert is_sensitive_name(name) is False, name


def test_redact_text_scrubs_credentials():
    samples = {
        "Authorization: Bearer abcdefghijklmnop": "abcdefghijklmnop",
        "password = hunter2hunter2": "hunter2hunter2",
        "client_secret: s3cr3t-value": "s3cr3t-value",
        "token ghp_0123456789abcdefghijABCDEFGHIJ": "ghp_0123456789abcdefghijABCDEFGHIJ",
        "https://acct.blob.core.windows.net/c?sig=AbCdEf123&sv=2022": "AbCdEf123",
    }
    for text, secret in samples.items():
        scrubbed = redact_text(text)
        assert secret not in scrubbed
        assert REDACTED in scrubbed


def test_private_key_blocks_are_fully_removed():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert "MIIEowIBAAKCAQEA1234" not in redact_text(pem)


def test_redact_mapping_keeps_structure_only():
    redacted = redact_mapping({"tags": {"owner": "platform"}, "ports": [22, 443]})
    assert redacted == {"tags": {"owner": REDACTED}, "ports": [REDACTED, REDACTED]}


def test_hash_payload_is_stable_sha256():
    assert hash_payload("abc") == hash_payload(b"abc")
    assert len(hash_payload("abc")) == 64
    assert hash_payload("abc") != hash_payload("abd")
