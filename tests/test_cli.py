import pytest
from typer.testing import CliRunner

from contextforge._metadata import APP_NAME, __version__
from contextforge.cli.main import app


@pytest.mark.parametrize("arguments", [["version"], ["--version"]])
def test_version_command(arguments: list[str]) -> None:
    runner = CliRunner()

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert result.stdout == f"{APP_NAME} {__version__}\n"
    assert result.stderr == ""


def test_doctor_command() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "ContextForge is installed." in result.stdout
