.PHONY: up down ps logs build test backup restore seed

up:
	docker compose up -d

down:
	docker compose stop

ps:
	docker compose ps

logs:
	docker compose logs -f

build:
	docker compose build

test:
	cd core && pip install -e ".[dev]" && pytest -q

backup:
	bash scripts/backup.sh

restore:
	bash scripts/restore.sh $(FILE)

seed:
	bash scripts/seed-env.sh
