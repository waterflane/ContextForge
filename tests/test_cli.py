from typer.testing import CliRunner

from contextforge.cli.main import app


def test_version_command() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "ContextForge 0.1.0" in result.stdout


def test_doctor_command() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "ContextForge is installed." in result.stdout
