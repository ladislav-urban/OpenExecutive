"""Opening the vector store from several threads at once (a turn's knowledge
and failure retrievals run side by side in worker threads) never fails:
chromadb's PersistentClient isn't safe to open concurrently on one path."""
from __future__ import annotations

import threading
from pathlib import Path

from openexecutive.knowledge.store import ChromaDBStore


def test_concurrent_opens_of_a_fresh_store_all_succeed(tmp_path: Path) -> None:
    for trial in range(5):
        path = tmp_path / f"chroma_{trial}"
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def open_store(
            path: Path = path, barrier: threading.Barrier = barrier, errors: list[BaseException] = errors,
        ) -> None:
            barrier.wait()
            try:
                ChromaDBStore(persist_directory=path)
            except BaseException as exc:  # noqa: BLE001 - every failure counts
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
