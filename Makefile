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

LAUNCH_DIR := $(HOME)/Library/LaunchAgents
UID_NUM := $(shell id -u)

.PHONY: launchd-install launchd-uninstall launchd-status logs-rotate-install

launchd-install:
	mkdir -p data/logs $(LAUNCH_DIR)
	for job in tick bot; do \
		sed "s#__PROJECT_DIR__#$(CURDIR)#g" deploy/com.outlierradar.$$job.plist > $(LAUNCH_DIR)/com.outlierradar.$$job.plist; \
		launchctl bootout gui/$(UID_NUM)/com.outlierradar.$$job 2>/dev/null || true; \
		launchctl bootstrap gui/$(UID_NUM) $(LAUNCH_DIR)/com.outlierradar.$$job.plist; \
	done
	@echo "Установлено. Статус: make launchd-status"

launchd-uninstall:
	for job in tick bot; do \
		launchctl bootout gui/$(UID_NUM)/com.outlierradar.$$job 2>/dev/null || true; \
		rm -f $(LAUNCH_DIR)/com.outlierradar.$$job.plist; \
	done

launchd-status:
	@for job in tick bot; do \
		echo "== $$job"; launchctl print gui/$(UID_NUM)/com.outlierradar.$$job 2>/dev/null | grep -E "state|last exit|runs" || echo "не установлен"; \
	done

logs-rotate-install:
	sed "s#__PROJECT_DIR__#$(CURDIR)#g" deploy/outlierradar.newsyslog.conf | sudo tee /etc/newsyslog.d/outlierradar.conf >/dev/null
	@echo "Ротация логов data/logs/*.log: 5 файлов по 10 МБ (newsyslog)"
