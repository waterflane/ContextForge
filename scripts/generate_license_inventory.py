"""Write a deterministic license inventory for the active Python environment."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import distributions
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    packages: list[dict[str, object]] = []
    for installed in distributions():
        metadata = installed.metadata
        name = metadata.get("Name")
        version = metadata.get("Version")
        if not name or not version:
            continue
        license_classifiers = sorted(
            classifier
            for classifier in metadata.get_all("Classifier", [])
            if classifier.startswith("License ::")
        )
        project_urls = sorted(metadata.get_all("Project-URL", []))
        packages.append(
            {
                "name": name,
                "version": version,
                "license_expression": metadata.get("License-Expression"),
                "license": metadata.get("License"),
                "license_classifiers": license_classifiers,
                "project_urls": project_urls,
            }
        )

    packages.sort(key=lambda package: str(package["name"]).casefold())
    payload = {
        "schema_version": 1,
        "environment": "installed runtime dependencies",
        "packages": packages,
    }
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {len(packages)} package records to {args.output}")


if __name__ == "__main__":
    main()
