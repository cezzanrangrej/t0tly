from __future__ import annotations
import pytest
from app.idgen import (
    encode_base62,
    decode_base62,
    id_to_code,
    code_to_id,
    _feistel_encrypt,
    _feistel_decrypt,
    CODE_LENGTH,
)


class TestBase62:
    def test_round_trip_zero(self):
        assert decode_base62(encode_base62(0)) == 0

    def test_round_trip_small(self):
        for n in [1, 61, 62, 63, 999, 100_000]:
            assert decode_base62(encode_base62(n)) == n, f"failed for {n}"

    def test_round_trip_large(self):
        # max code value: 62^7 - 1
        n = 62**7 - 1
        assert decode_base62(encode_base62(n)) == n

    def test_always_7_chars(self):
        for n in [0, 1, 100, 1_000_000, 10_000_000_000]:
            code = encode_base62(_feistel_encrypt(n))
            assert len(code) == CODE_LENGTH, f"wrong length for n={n}: '{code}'"

    def test_left_padded(self):
        # Small n should still produce 7-char string
        assert len(encode_base62(0)) == CODE_LENGTH
        assert len(encode_base62(1)) == CODE_LENGTH


class TestFeistel:
    def test_bijective_small(self):
        for n in range(500):
            assert _feistel_decrypt(_feistel_encrypt(n)) == n

    def test_bijective_samples(self):
        samples = [100_000, 999_999, 2_097_151, 3_521_614_606_000]
        for n in samples:
            enc = _feistel_encrypt(n)
            assert _feistel_decrypt(enc) == n, f"not bijective for n={n}"

    def test_no_trivial_fixed_points(self):
        fixed = sum(1 for n in range(1000) if _feistel_encrypt(n) == n)
        assert fixed < 10, f"too many fixed points: {fixed}"

    def test_different_inputs_give_different_outputs(self):
        outputs = {_feistel_encrypt(n) for n in range(10_000)}
        assert len(outputs) == 10_000, "collision in Feistel outputs"


class TestIdToCode:
    def test_uniqueness_10k(self):
        codes = [id_to_code(i) for i in range(10_000)]
        assert len(set(codes)) == 10_000

    def test_all_7_chars(self):
        for i in range(100):
            assert len(id_to_code(i)) == 7

    def test_round_trip(self):
        for i in [1, 500, 99_999, 200_001, 1_000_000]:
            code = id_to_code(i)
            assert code_to_id(code) == i, f"round-trip failed: i={i}"
