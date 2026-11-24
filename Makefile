.PHONY: up down restart logs test smoke clean

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

test:
	python -m pytest

smoke:
	python smoke_test.py

clean:
	docker compose down -v
