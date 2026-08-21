import click
import pytest
from typer.testing import CliRunner

from contextforge._metadata import APP_NAME, __version__
from contextforge.cli.main import app


def test_positional_metavars_survive_sequential_help_invocations() -> None:
    runner = CliRunner()
    commands = (
        (("scan", "--help"), "PATH"),
        (("context", "inspect", "--help"), "PACKAGE"),
        (("tree", "--help"), "PATH"),
        (("benchmark", "discovery", "--help"), "PATH"),
        (("scan", "--help"), "PATH"),
    )
    first_scan_output: str | None = None

    for arguments, metavar in commands:
        result = runner.invoke(app, list(arguments), terminal_width=160)
        output = click.unstyle(result.output)

        assert result.exit_code == 0
        assert metavar in output
        if arguments[0] == "scan":
            if first_scan_output is None:
                first_scan_output = output
            else:
                assert output == first_scan_output


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
