default:
    @just --list

install:
    uv sync

test *args:
    uv run pytest {{args}}

lint:
    uv run ruff check .

format:
    uv run ruff format .
    uv run ruff check --fix .

typecheck:
    uv run mypy

check:
    just lint
    just typecheck
    just test

loc:
    cloc --by-file src/ tests/ --include-lang=Python

deploy:
    cd /Users/jochen/workspaces/ws-archive/ops-control && just deploy-one archive

manage *args:
    cd src/django && uv run python manage.py {{args}}

dev:
    cd src/django && uv run python manage.py runserver

migrate:
    just manage migrate

makemigrations:
    just manage makemigrations
