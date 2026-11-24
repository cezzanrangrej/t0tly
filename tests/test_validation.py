from __future__ import annotations
import pytest
from app.validation import validate_url, validate_alias, ValidationError


class TestValidateUrl:
    def test_valid_http(self):
        assert validate_url("http://example.com/path") == "http://example.com/path"

    def test_valid_https(self):
        assert validate_url("https://example.com") == "https://example.com"

    def test_valid_with_query(self):
        assert validate_url("https://example.com/p?q=1&r=2") == "https://example.com/p?q=1&r=2"

    def test_rejects_ftp(self):
        with pytest.raises(ValidationError, match="scheme"):
            validate_url("ftp://example.com")

    def test_rejects_bare_hostname(self):
        with pytest.raises(ValidationError):
            validate_url("example.com")

    def test_rejects_javascript(self):
        with pytest.raises(ValidationError, match="scheme"):
            validate_url("javascript:alert(1)")

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError, match="2048"):
            validate_url("https://example.com/" + "a" * 2050)

    def test_rejects_loopback_ipv4(self):
        with pytest.raises(ValidationError, match="private"):
            validate_url("http://127.0.0.1/secret")

    def test_rejects_private_10(self):
        with pytest.raises(ValidationError, match="private"):
            validate_url("http://10.0.0.1/data")

    def test_rejects_private_172(self):
        with pytest.raises(ValidationError, match="private"):
            validate_url("http://172.16.0.1/data")

    def test_rejects_private_192(self):
        with pytest.raises(ValidationError, match="private"):
            validate_url("http://192.168.1.1/data")

    def test_rejects_self_referential(self):
        # BASE_URL is http://localhost:8000 in test env
        with pytest.raises(ValidationError):
            validate_url("http://localhost:8000/something")

    def test_empty_url(self):
        with pytest.raises(ValidationError):
            validate_url("")

    def test_no_host(self):
        with pytest.raises(ValidationError):
            validate_url("https:///path")


class TestValidateAlias:
    def test_valid_simple(self):
        assert validate_alias("my-alias") == "my-alias"

    def test_valid_min_length(self):
        assert validate_alias("abc") == "abc"

    def test_valid_max_length(self):
        assert validate_alias("a" * 30) == "a" * 30

    def test_valid_underscores(self):
        assert validate_alias("my_alias_123") == "my_alias_123"

    def test_too_short(self):
        with pytest.raises(ValidationError):
            validate_alias("ab")

    def test_too_long(self):
        with pytest.raises(ValidationError):
            validate_alias("a" * 31)

    def test_space_rejected(self):
        with pytest.raises(ValidationError):
            validate_alias("hello world")

    def test_at_sign_rejected(self):
        with pytest.raises(ValidationError):
            validate_alias("hello@world")

    def test_reserved_shorten(self):
        with pytest.raises(ValidationError, match="reserved"):
            validate_alias("shorten")

    def test_reserved_health(self):
        with pytest.raises(ValidationError, match="reserved"):
            validate_alias("health")

    def test_reserved_metrics(self):
        with pytest.raises(ValidationError, match="reserved"):
            validate_alias("metrics")

    def test_reserved_case_insensitive(self):
        with pytest.raises(ValidationError, match="reserved"):
            validate_alias("HEALTH")
