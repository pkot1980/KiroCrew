"""``delete_items_batch`` must never run on the event loop.

It rebuilds the entire entity graph (``store._load_graph``: clear + two full table
scans) inside its own ``BEGIN``/``COMMIT``, so on a large library it blocks the loop
past the stall watchdog. The watchdog then exits, the supervisor respawns, and the
fresh process runs the same scan -- a crash loop, observed repeatedly against a
2900-document folder source with the frozen stack pinning
``store.delete_items_batch <- ingestion._ingest_file_body <- folder_watcher._do_scan``.

Two tests, deliberately different in kind:

* the AST ratchet pins every call site at once and fails if any future edit calls it
  straight from a coroutine body, including sites this PR has not seen;
* the behavioural test proves the offload is real rather than lexical -- it asserts
  the THREAD the call lands on, so a refactor that keeps ``asyncio.to_thread`` in the
  source but hands it something already-invoked still fails.
"""

from __future__ import annotations

import ast
import json
import pathlib
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.store import KnowledgeStore

# A nested def / lambda is a separate execution frame -- a sync helper or a thread
# target -- so a call inside one is not running on the loop. Mirrors the scoping the
# repo's other on-loop guards use.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _called_names(body: list[ast.stmt]) -> set[tuple[str, int]]:
    """Every name called directly in *body*, paired with its line number.

    Nested scopes are skipped: a call inside a nested ``def``/``lambda`` runs in
    that frame (a sync helper, or a thread target), not in the enclosing one.
    """
    out: set[tuple[str, int]] = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            func.attr if isinstance(func, ast.Attribute)
            else getattr(func, "id", None)
        )
        if called:
            out.add((called, node.lineno))
    return out


def _sync_helpers_reaching(tree: ast.Module, name: str) -> set[str]:
    """Sync ``def`` names in this module that reach *name* without a thread hop.

    Computed as a fixpoint, so a chain of sync helpers is followed to any depth.
    Calling one of these from a coroutine body blocks the loop exactly as much
    as calling *name* itself -- the indirection is invisible to a lexical scan,
    which is how ``_skip_as_duplicate`` kept a synchronous
    ``delete_items_batch`` alive after every direct call site was offloaded.

    Scope is deliberately one module: resolving a call to another module's
    method needs import and receiver-type resolution, which an AST scan cannot
    do honestly. Same-file indirection is what the real defect used.
    """
    sync_defs = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    reaching: set[str] = set()
    while True:
        grew = False
        for fname, fn in sync_defs.items():
            if fname in reaching:
                continue
            targets = {called for called, _ in _called_names(fn.body)}
            if name in targets or targets & reaching:
                reaching.add(fname)
                grew = True
        if not grew:
            return reaching


def _on_loop_call_sites(name: str) -> list[str]:
    """Every call reaching *name* from an ``async def`` body without a hop.

    Covers both the direct call and a call to a same-module sync helper that
    reaches it (see ``_sync_helpers_reaching``).
    """
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:  # pragma: no cover - syntax is enforced elsewhere
            continue
        indirect = _sync_helpers_reaching(tree, name)
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
            for called, lineno in sorted(_called_names(fn.body), key=lambda c: c[1]):
                if called == name:
                    found.append(
                        f"{path.relative_to(_SRC)}:{lineno} in async {fn.name}")
                elif called in indirect:
                    found.append(
                        f"{path.relative_to(_SRC)}:{lineno} in async {fn.name} "
                        f"(via sync {called})")
    return found


def test_delete_items_batch_is_never_called_on_the_event_loop():
    """Ratchet: hand it to a worker, never call it from a coroutine body."""
    offenders = _on_loop_call_sites("delete_items_batch")
    assert offenders == [], (
        "delete_items_batch runs on the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nOffload it: await asyncio.to_thread(store.delete_items_batch, ids, ...). "
          "The connection is autocommit (isolation_level=None), so no enclosing "
          "transaction spans the call and the worker's thread-local connection may "
          "take the write lock on its own."
    )


@pytest.mark.asyncio
async def test_handle_deleted_runs_the_delete_off_the_loop_thread(tmp_path, monkeypatch):
    """The offload is real: the delete executes on some other thread."""
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("src", "local_folder", str(tmp_path))
        item_id = store.add_item("title", "body", "doc", source_id=source_id)

        seen_threads: list[int] = []
        real = store.delete_items_batch

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "delete_items_batch", recording)

        watcher = FolderWatcher(store, MagicMock())
        await watcher._handle_deleted(
            source_id, "gone.md", {"item_ids": json.dumps([item_id])})

        assert seen_threads, (
            "delete_items_batch was never called -- this test no longer exercises "
            "the deleted-file path and would pass vacuously")
        assert threading.get_ident() not in seen_threads, (
            "delete_items_batch ran on the event-loop thread; it must be handed to "
            "asyncio.to_thread")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duplicate_skip_runs_the_delete_off_the_loop_thread(tmp_path):
    """The duplicate gate's delete executes on some other thread.

    The gate is reached through a plain ``def`` helper, so the lexical ratchet
    above cannot see the hop; only asserting the THREAD proves it is real.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        # Holder: another source already owns this exact text. Equal source_type
        # means neither outranks the other, which is the branch that refuses the
        # write -- and therefore the branch that deletes the superseded items.
        holder = store.add_source(name="holder", source_type="artifact",
                                  uri="artifact://holder")
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])

        target = store.add_source(name="target", source_type="artifact",
                                  uri="artifact://target")
        await pipeline.ingest_text("its own body", title="T", source_id=target,
                                   old_item_ids=[])
        superseded = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (target,)).fetchall()]
        assert superseded, "no items to supersede -- setup would prove nothing"

        seen_threads: list[int] = []
        real = store.delete_items_batch_in_txn

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        store.delete_items_batch_in_txn = recording  # type: ignore[method-assign]

        job = await pipeline.ingest_text("shared body", title="T", source_id=target,
                                         old_item_ids=superseded)

        assert job, "the duplicate gate did not return its terminal job id"
        assert seen_threads, (
            "delete_items_batch_in_txn was never called -- the duplicate gate was "
            "not reached and this test would pass vacuously")
        assert threading.get_ident() not in seen_threads, (
            "the duplicate gate's delete ran on the event-loop thread; the whole "
            "gate must travel through run_to_completion")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_run_to_completion_forwards_the_return_value():
    """The hop is usable for work that reports a result, not just side effects.

    Without this the duplicate gate could only be offloaded by splitting its
    delete from the job id it returns, across an await -- the exact shape that
    strands committed data.
    """
    from kiro_crew.knowledge.ingestion import run_to_completion

    assert await run_to_completion(lambda: "job-1234") == "job-1234"
    assert await run_to_completion(lambda: None) is None


@pytest.mark.asyncio
async def test_finalizer_runs_even_when_cancelled_while_queued():
    """A cancellation landing while the finalizer is still QUEUED in the
    executor must not skip it (GPT round-2 finding on #2336): a bare
    ``await asyncio.to_thread(fn)`` cancels the queued future before ``fn``
    starts, stranding the committed new items with no state finalization.

    Deterministic setup: a 1-worker executor whose only worker is held by a
    blocker, so the finalizer is provably queued when the cancel arrives.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from kiro_crew.knowledge.ingestion import run_to_completion

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    try:
        gate = threading.Event()
        ran = threading.Event()

        # Occupy the single worker so the finalizer sits in the queue.
        blocker = loop.run_in_executor(None, gate.wait)
        await asyncio.sleep(0)  # let the blocker claim the worker

        task = asyncio.ensure_future(run_to_completion(ran.set))
        await asyncio.sleep(0)  # finalizer submitted, still queued
        task.cancel()
        gate.set()  # release the worker AFTER the cancel landed

        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran.is_set(), (
            "finalizer was skipped by a cancellation that arrived while it "
            "was queued; run_to_completion must drain it before re-raising")
        await blocker
    finally:
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_duplicate_gate_reingests_when_the_holder_vanishes_before_the_lock(tmp_path):
    """A holder deleted between the probe and the write lock must not dedupe.

    The gate reads a holder and then makes the target DEPEND on it, so the two
    steps have to be one atomic unit. Moving the gate off the event loop let a
    concurrent source deletion land in the middle: the target got a terminal
    'skipped_duplicate' row while the copy it was attaching to was cascaded
    away, leaving the file on disk with its content unrecoverable.

    Simulate the interleaving deterministically by making the authoritative
    in-transaction lookup miss after the cheap probe has already hit. The gate
    must decline to dedupe and let a normal ingest proceed.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=MagicMock(), embedder=None)

        holder = store.add_source(name="holder", source_type="artifact",
                                  uri="artifact://holder")
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        target = store.add_source(name="target", source_type="artifact",
                                  uri="artifact://target")

        real_find = store.find_doc_by_content_hash
        calls: list[int] = []

        def vanishing(*args, **kwargs):
            calls.append(1)
            # First call is the unlocked probe (hit); the second is the
            # authoritative read under BEGIN IMMEDIATE -- by then the holder is
            # "deleted", so it must miss.
            return real_find(*args, **kwargs) if len(calls) == 1 else None

        store.find_doc_by_content_hash = vanishing  # type: ignore[method-assign]

        job = await pipeline.ingest_text("shared body", title="T",
                                         source_id=target, old_item_ids=[])

        assert len(calls) >= 2, (
            "the gate consulted the holder only once, so the holder is still "
            "read outside the write lock and this test proves nothing")
        rows = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE source_id = ?",
            (target,)).fetchall()
        assert rows, "the ingest recorded no job at all"
        assert all(r["status"] != "skipped_duplicate" for r in rows), (
            "target was marked a duplicate of a holder that no longer exists -- "
            f"its content is now unrecoverable (job={job}, rows={[dict(r) for r in rows]})")
        assert store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?",
            (target,)).fetchone()[0] > 0, (
            "target kept no items of its own after the holder vanished")
    finally:
        store.db.close()


@pytest.mark.asyncio
async def test_deduped_state_write_is_off_loop_and_keeps_a_late_adoption(tmp_path):
    """The terminal 'deduped' write runs off the loop AND keeps a cascade's adoption.

    Both properties belong to that one write. It takes the write lock, so running
    it on the loop thread makes ``BEGIN IMMEDIATE`` wait there on any concurrent
    writer -- the same stall this change exists to remove, reintroduced one line
    further along. And the group it records cannot be predicted:

    The pre-ingest gate commits its own transaction, so a user deleting the HOLDER
    lands in the window between that commit and this write. That cascade is benign
    by design -- it sees this folder's location row, reassigns the surviving item
    to this source, and adopts it into this very ``folder_file_state`` row. Writing
    ``item_ids='[]'`` afterwards -- which is what "the gate refused, so this file
    owns nothing" predicts -- erases the adoption and leaves the only remaining
    copy owned by this source but named by no state row: unreachable by the
    deleted-file path and undeletable, which is exactly the strand the ownership
    model exists to stop.

    The interleaving is produced deterministically by cascading the holder away the
    instant the gate returns its terminal job, with no threads involved. The scan
    must end with the row naming the item it now owns.
    """
    from unittest.mock import AsyncMock

    from kiro_crew.knowledge.ingestion import IngestionPipeline

    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "doc.md").write_text("shared body", encoding="utf-8")

        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(
            return_value=[{"category": "document", "summary": "s", "entities": []}])
        chunker = MagicMock()
        chunker.chunk.side_effect = lambda text, **kw: [
            {"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]
        reader = MagicMock()
        reader.read.return_value = ("shared body", {})
        pipeline = IngestionPipeline(
            store=store, extractor=extractor, chunker=chunker,
            reader=reader, embedder=None)

        # The holder owns this exact text first. Equal rank (both persistent
        # source types) is the branch that REFUSES the incoming copy -- a
        # transient holder would be outranked and the folder would just ingest.
        holder = store.add_source(name="holder", source_type="local_folder",
                                  uri=str(tmp_path / "other"))
        await pipeline.ingest_text("shared body", title="H", source_id=holder,
                                   old_item_ids=[])
        held = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (holder,)).fetchall()]
        assert held, "holder owns no items -- the gate would never refuse anything"

        source_id = store.add_source(name="folder", source_type="local_folder",
                                     uri=str(folder))

        watcher = FolderWatcher(store, pipeline)
        real_ingest = pipeline.ingest_file
        refused: list[str] = []

        async def cascade_between(*args, **kwargs):
            job = await real_ingest(*args, **kwargs)
            status = (pipeline.get_job_status(job) or {}).get("status") if job else None
            if status == "skipped_duplicate":
                refused.append(str(job))
                # The user deletes the holder HERE: after the gate committed its
                # attachment, before the scan records the file's terminal state.
                store.delete_source_cascade(holder)
            return job

        pipeline.ingest_file = cascade_between  # type: ignore[method-assign]

        seen_threads: list[int] = []
        real_record = watcher._record_deduped_state

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real_record(*args, **kwargs)

        watcher._record_deduped_state = recording  # type: ignore[method-assign]

        await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder",
             "properties": "{}"})

        assert refused, (
            "the pre-ingest gate never refused the file, so no cascade was "
            "interleaved and this test would pass vacuously")
        assert seen_threads, (
            "_record_deduped_state was never called -- the deduped branch was not "
            "reached and both assertions below would prove nothing")
        assert threading.get_ident() not in seen_threads, (
            "the terminal 'deduped' write ran on the event-loop thread; it takes "
            "the write lock, so it must travel through run_to_completion")

        owned = [r["id"] for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()]
        assert owned, (
            "the cascade left this source owning nothing, so there is no "
            "adoption to preserve and the assertion below proves nothing")

        row = store.db.execute(
            "SELECT item_ids, status FROM folder_file_state "
            "WHERE source_id = ?", (source_id,)).fetchone()
        assert row, "the scan recorded no state row for the file at all"
        named = json.loads(row["item_ids"] or "[]")
        assert sorted(named) == sorted(owned), (
            "the terminal 'deduped' write overwrote the adoption: this source "
            f"owns {owned} but its state row names {named}, so the last copy of "
            "the document is unreachable and undeletable "
            f"(status={row['status']!r})")
    finally:
        store.close()
