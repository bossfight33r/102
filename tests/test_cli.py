from typer.testing import CliRunner

from radar.cli import app

runner = CliRunner()


def test_help():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("doctor", "quota", "niche", "channel"):
        assert cmd in r.output


def test_doctor(cli_app):
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "БД" in r.output and "Квота" in r.output


def test_niche_add_list(cli_app):
    r = runner.invoke(
        app, ["niche", "add", "cooking", "--name", "Кулинария", "-q", "рецепт", "--per-day", "1"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["niche", "list"])
    assert "cooking" in r.output and "ai-tools" in r.output
    r = runner.invoke(app, ["niche", "add", "Bad Id", "--name", "x"])
    assert r.exit_code == 1


def test_channel_add_approve_hide(cli_app):
    r = runner.invoke(app, ["channel", "add", "@techguru", "--niche", "ai-tools"])
    assert r.exit_code == 0, r.output
    assert "watching" in r.output
    r = runner.invoke(app, ["channel", "hide", "UCtechguru00000000000000"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["channel", "list", "--status", "hidden"])
    assert "Tech Guru" in r.output
    r = runner.invoke(app, ["channel", "approve", "UCnope"])
    assert r.exit_code == 1


def test_quota_cmd(cli_app):
    runner.invoke(app, ["channel", "add", "@techguru"])
    r = runner.invoke(app, ["quota"])
    assert r.exit_code == 0
    assert "1/10000" in r.output


def test_poll_score_outliers(cli_app):
    runner.invoke(app, ["channel", "add", "@smallbuilder"])
    r = runner.invoke(app, ["poll"])
    assert r.exit_code == 0, r.output
    assert "new_videos=57" in r.output
    r = runner.invoke(app, ["score"])
    assert "outliers=3" in r.output, r.output
    r = runner.invoke(app, ["outliers", "--format", "long"])
    assert "sbBREAKOUT1" in r.output and "tgLONGOUT01" in r.output and "tgSHORTOUT1" not in r.output
    r = runner.invoke(app, ["discover"])
    assert "searches=2" in r.output
