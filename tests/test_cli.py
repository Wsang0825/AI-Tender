from typer.testing import CliRunner

from tender_ai.cli.main import app


runner = CliRunner()


def test_cli_doctor_succeeds():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert '"ok": true' in result.stdout


def test_cli_init_db_seeds_sources(tmp_path):
    result = runner.invoke(app, ["init-db", "--database", str(tmp_path / "tender.db")])
    assert result.exit_code == 0
    assert "database initialized" in result.stdout


def test_cli_sources_json():
    result = runner.invoke(app, ["sources", "--json"])
    assert result.exit_code == 0
    assert "中国政府采购网" in result.stdout


def test_cli_recalc_empty_database(tmp_path):
    result = runner.invoke(app, ["recalc", "--database", str(tmp_path / "tender.db"), "--now", "2026-08-28 12:00"])
    assert result.exit_code == 0
    assert '"total": 0' in result.stdout
