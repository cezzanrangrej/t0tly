from __future__ import annotations
import os

# Override env before any app module is imported
os.environ["POSTGRES_DSN"] = "postgresql://urlshort:urlshort@127.0.0.1:5432/urlshort_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/1"
os.environ["BASE_URL"] = "http://127.0.0.1:8000"
os.environ.setdefault("FEISTEL_KEY1", "deadbeef01234567")
os.environ.setdefault("FEISTEL_KEY2", "cafebabe89abcdef")
os.environ.setdefault("FEISTEL_KEY3", "0011223344556677")
os.environ.setdefault("FEISTEL_KEY4", "8899aabbccddeeff")
os.environ.setdefault("RATE_LIMIT_CREATES", "10")
os.environ.setdefault("RATE_LIMIT_WINDOW", "60")
