.PHONY: setup test lint fmt bot tick doctor demo

setup:
	command -v uv >/dev/null || (echo "Установите uv: brew install uv" && exit 1)
	uv sync
	test -f .env || cp .env.example .env
	test -f config/settings.yaml || cp config/settings.example.yaml config/settings.yaml
	test -f config/niches.yaml || cp config/niches.example.yaml config/niches.yaml
	test -f config/channel_profile.yaml || cp config/channel_profile.example.yaml config/channel_profile.yaml
	mkdir -p data/exports data/cache
	@echo "Готово. Заполните .env и config/*.yaml, затем: uv run radar doctor"

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

bot:
	uv run radar bot

tick:
	uv run radar tick

doctor:
	uv run radar doctor

demo:
	RADAR_FAKE=1 RADAR_DATA_DIR=data/demo RADAR_NOW=2026-10-06T06:00:00+00:00 uv run radar tick
