import asyncio
from collections import Counter
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.intelligence import retrieve_context_candidates, store
from contextforge.intelligence.models import IndexedFileState, IndexManifest


def test_source_digest_is_checked_once_per_query_and_again_between_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "jobs.py").write_text(
        "def execute_job():\n    return 7\n", encoding="utf-8"
    )
    (tmp_path / "entry.py").write_text(
        "from jobs import execute_job\ndef launch_job():\n    return execute_job()\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    original = store.load_index_record
    loads: Counter[str] = Counter()

    def measured(
        root: str | Path,
        state: IndexedFileState,
        *,
        manifest: IndexManifest | None = None,
    ) -> bytes:
        loads[state.path] += 1
        return original(root, state, manifest=manifest)

    monkeypatch.setattr(store, "load_index_record", measured)
    first = asyncio.run(
        retrieve_context_candidates(
            tmp_path, "Find callers of execute_job", manifest=report.manifest
        )
    )
    assert loads and max(loads.values()) == 1
    first_paths = set(loads)
    loads.clear()
    second = asyncio.run(
        retrieve_context_candidates(
            tmp_path, "Find callers of execute_job", manifest=report.manifest
        )
    )
    assert set(loads) == first_paths and max(loads.values()) == 1
    assert first == second and second.provider_calls == 0
