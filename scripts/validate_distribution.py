"""Validate release archive metadata and a strict public contents allowlist."""

from __future__ import annotations

import argparse
import email
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_NAME = "contextforge-repo"
EXPECTED_LICENSE = "Apache-2.0"
VERSION_PATTERN = re.compile(r'^__version__ = "(?P<version>[^"]+)"$', re.MULTILINE)
FORBIDDEN_PARTS = {
    ".contextforge",
    ".agents",
    ".coverage",
    ".env",
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    ".review-fixture",
    "__pycache__",
    "build",
    "docs",
    "htmlcov",
    "tests",
    "dist",
    "wiki",
}
FORBIDDEN_CONTENT_PATTERNS = {
    "workspace absolute path": re.compile(
        rb"C:[\\/]+Programming[\\/]+Projects[\\/]+ContextForge", re.IGNORECASE
    ),
    "developer home path": re.compile(rb"C:[\\/]+Users[\\/]+water", re.IGNORECASE),
    "private key": re.compile(rb"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "provider credential": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
}


def _source_version(root: Path) -> str:
    metadata = (root / "src/contextforge/_metadata.py").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(metadata)
    if match is None:
        raise ValueError("could not read the source package version")
    return match.group("version")


def _metadata(payload: bytes) -> email.message.Message:
    return email.message_from_bytes(payload)


def _validate_metadata(payload: bytes, version: str) -> None:
    metadata = _metadata(payload)
    expected = {
        "Name": EXPECTED_NAME,
        "Version": version,
        "License-Expression": EXPECTED_LICENSE,
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise ValueError(
                f"invalid {field}: expected {value!r}, got {metadata.get(field)!r}"
            )
    license_files = set(metadata.get_all("License-File", []))
    if license_files != {"LICENSE", "NOTICE"}:
        raise ValueError(f"unexpected License-File metadata: {license_files!r}")


def _has_forbidden_part(path: PurePosixPath) -> bool:
    lowered = {part.lower() for part in path.parts}
    return bool(lowered & FORBIDDEN_PARTS) or any(
        part.lower().endswith((".log", ".orig", ".prof", ".rej", ".tmp"))
        for part in path.parts
    )


def _validate_content(archive_name: str, member_name: str, payload: bytes) -> None:
    for description, pattern in FORBIDDEN_CONTENT_PATTERNS.items():
        if pattern.search(payload):
            raise ValueError(
                f"{archive_name}: {description} found in packaged {member_name}"
            )


def _validate_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = [PurePosixPath(name) for name in archive.namelist()]
        metadata_names = [name for name in names if name.parts[-1:] == ("METADATA",)]
        if len(metadata_names) != 1:
            raise ValueError(f"{path.name}: expected exactly one METADATA file")
        _validate_metadata(archive.read(str(metadata_names[0])), version)
        for name in names:
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"{path.name}: unsafe archive path {name}")
            top = name.parts[0] if name.parts else ""
            if top != "contextforge" and not top.endswith(".dist-info"):
                raise ValueError(f"{path.name}: unexpected wheel path {name}")
            if _has_forbidden_part(name):
                raise ValueError(f"{path.name}: forbidden wheel path {name}")
            if name.parts and not str(name).endswith("/"):
                _validate_content(path.name, str(name), archive.read(str(name)))
        required_bridge = {
            PurePosixPath("contextforge/bridge/__init__.py"),
            PurePosixPath("contextforge/bridge/models.py"),
            PurePosixPath("contextforge/bridge/protocol.py"),
            PurePosixPath("contextforge/bridge/server.py"),
        }
        if not required_bridge.issubset(names):
            raise ValueError(f"{path.name}: bridge package is incomplete")
        if PurePosixPath("contextforge/protocol.py") in names:
            raise ValueError(f"{path.name}: obsolete bridge envelope was packaged")


def _validate_sdist(path: Path, version: str) -> None:
    expected_root = f"contextforge_repo-{version}"
    allowed_files = {
        "CHANGELOG.md",
        "LICENSE",
        "NOTICE",
        ".gitignore",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
    }
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        metadata_members = [
            member
            for member in members
            if PurePosixPath(member.name).name == "PKG-INFO"
        ]
        if len(metadata_members) != 1:
            raise ValueError(f"{path.name}: expected exactly one PKG-INFO file")
        metadata_file = archive.extractfile(metadata_members[0])
        if metadata_file is None:
            raise ValueError(f"{path.name}: PKG-INFO is not a regular file")
        _validate_metadata(metadata_file.read(), version)
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"{path.name}: unsafe archive path {name}")
            if not name.parts or name.parts[0] != expected_root:
                raise ValueError(f"{path.name}: unexpected sdist root {name}")
            relative = PurePosixPath(*name.parts[1:])
            if not relative.parts:
                continue
            allowed = (
                str(relative) in allowed_files
                or relative == PurePosixPath("src/contextforge")
                or relative.is_relative_to("src/contextforge")
            )
            if not allowed:
                raise ValueError(f"{path.name}: unexpected sdist path {relative}")
            if _has_forbidden_part(relative):
                raise ValueError(f"{path.name}: forbidden sdist path {relative}")
            if member.isfile():
                member_file = archive.extractfile(member)
                if member_file is None:
                    raise ValueError(f"{path.name}: could not read {relative}")
                _validate_content(path.name, str(relative), member_file.read())
        relative_names = {
            PurePosixPath(*PurePosixPath(member.name).parts[1:]) for member in members
        }
        required_bridge = {
            PurePosixPath("src/contextforge/bridge/__init__.py"),
            PurePosixPath("src/contextforge/bridge/models.py"),
            PurePosixPath("src/contextforge/bridge/protocol.py"),
            PurePosixPath("src/contextforge/bridge/server.py"),
        }
        if not required_bridge.issubset(relative_names):
            raise ValueError(f"{path.name}: bridge package is incomplete")
        if PurePosixPath("src/contextforge/protocol.py") in relative_names:
            raise ValueError(f"{path.name}: obsolete bridge envelope was packaged")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    version = _source_version(project_root)
    wheels = sorted(args.directory.glob("*.whl"))
    sdists = sorted(args.directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one .tar.gz sdist")
    _validate_wheel(wheels[0], version)
    _validate_sdist(sdists[0], version)
    print(f"validated {wheels[0].name} and {sdists[0].name}")


if __name__ == "__main__":
    main()
