from pathlib import Path

import pytest

from radar.config import Settings, load_app_config, load_niches, load_profile, resolve_config_file

ROOT = Path(__file__).resolve().parents[1]


def test_examples_load():
    cfg = load_app_config(ROOT / "config")
    assert cfg.quota.costs["search.list"] == 100
    assert cfg.quota.daily_budget == 10_000
    niches = load_niches(ROOT / "config")
    assert niches and niches[0].id == "ai-tools"
    profile = load_profile(ROOT / "config")
    assert profile is not None and profile.topics


def test_fallback_to_example(tmp_path: Path):
    (tmp_path / "settings.example.yaml").write_text("digest:\n  send_hour: 9\n")
    assert resolve_config_file(tmp_path, "settings").name == "settings.example.yaml"
    (tmp_path / "settings.yaml").write_text("digest:\n  send_hour: 7\n")
    assert load_app_config(tmp_path).digest.send_hour == 7


def test_empty_dir_defaults(tmp_path: Path):
    assert load_app_config(tmp_path).scoring.min_views == 1000
    assert load_niches(tmp_path) == []
    assert load_profile(tmp_path) is None


def test_unknown_config_key_rejected(tmp_path: Path):
    (tmp_path / "settings.yaml").write_text("scoring:\n  typo_key: 1\n")
    with pytest.raises(ValueError):
        load_app_config(tmp_path)


def test_duplicate_niche_ids(tmp_path: Path):
    (tmp_path / "niches.yaml").write_text("niches:\n  - {id: a, name: A}\n  - {id: a, name: B}\n")
    with pytest.raises(ValueError):
        load_niches(tmp_path)


def test_admin_ids_parsing(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "123, 456")
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaFAKEFAKEFAKE")
    s = Settings(_env_file=None)
    assert s.admin_ids == [123, 456]
    assert "AIzaFAKEFAKEFAKE" not in repr(s)
    assert "AIzaFAKEFAKEFAKE" in s.secret_values()
