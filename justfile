default:
    @just --list

install:
    uv sync

test *args:
    uv run pytest {{args}}

lint:
    uv run ruff check .

# Audit the locked runtime and dev dependencies for known vulnerabilities.
audit:
    uv export --frozen --no-hashes --no-emit-project | uvx pip-audit --disable-pip --no-deps -r /dev/stdin

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
