#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Crash-resilience / no-corruption test (PRD 7g, no-corruption part).

Repeatedly (>= 20 iterations):

  1. open the collection,
  2. perform real review writes in a *child process* (answering due cards and
     adding notes in a tight loop, committing as it goes),
  3. simulate an abrupt stop by ``SIGKILL``-ing that child mid-write (no clean
     close, no atexit, no flush),
  4. reopen the same collection and run a DB integrity + sanity check,

and assert ZERO corruption across every iteration. Anki stores collections in
SQLite WAL mode with ``synchronous=FULL``, so a hard kill mid-transaction must
roll back cleanly on reopen — this test proves that empirically against the
real engine.

Usage (from the repo root, via the desktop env)::

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/crash_test.py
    PYTHONPATH=out/pylib out/pyenv/bin/python tools/crash_test.py --iterations 30

Or simply ``make crash-test``.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time

# Allow running as a plain script (``python tools/crash_test.py``).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _bench_common as common  # noqa: E402


def _crash_master_path(num_cards: int, seed: int) -> str:
    return os.path.join(common.CACHE_DIR, f"crash_master_{num_cards}_{seed}.anki2")


def _crash_live_path(num_cards: int, seed: int) -> str:
    # The single file that is crashed and reopened on every iteration.
    return os.path.join(common.CACHE_DIR, f"crash_live_{num_cards}_{seed}.anki2")


# ---------------------------------------------------------------------------
# Child worker: writes continuously until killed.
# ---------------------------------------------------------------------------


def run_worker(path: str, heartbeat: str, seed: int) -> int:
    """Open the collection and write continuously until the process is killed.

    Bumps a heartbeat file after each committed write so the parent can confirm
    writes are actually in flight before it pulls the trigger.
    """
    from anki.collection import Collection

    rng = random.Random(seed)
    col = Collection(path)
    deck_id = col.decks.id(common.DECK_NAME)
    assert deck_id is not None
    common.prepare_for_review(col, deck_id)
    notetype = col.models.by_name("Basic")
    assert notetype is not None

    writes = 0

    def bump() -> None:
        nonlocal writes
        writes += 1
        # Atomic-ish heartbeat: write count to a temp then replace.
        tmp = f"{heartbeat}.tmp"
        with open(tmp, "w") as fh:
            fh.write(str(writes))
        os.replace(tmp, heartbeat)

    while True:
        # Real review write: answer the card at the top of the queue.
        card = col.sched.getCard()
        if card is not None:
            col.sched.answerCard(card, rng.randint(1, 4))
            bump()

        # Keep continuous write pressure (and never run dry across iterations)
        # by also inserting fresh notes.
        for _ in range(5):
            note = col.new_note(notetype)
            note["Front"] = f"crash {writes} {rng.random()}"
            note["Back"] = "x"
            note.tags = ["MCAT::Physics::Core"]
            col.add_note(note, deck_id)
            bump()


# ---------------------------------------------------------------------------
# Parent: spawn, kill mid-write, verify integrity.
# ---------------------------------------------------------------------------


def _read_heartbeat(path: str) -> int:
    try:
        with open(path) as fh:
            return int(fh.read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def spawn_writer(path: str, heartbeat: str, seed: int) -> subprocess.Popen:
    script = os.path.abspath(__file__)
    return subprocess.Popen(
        [
            sys.executable,
            script,
            "--worker",
            "--path",
            path,
            "--heartbeat",
            heartbeat,
            "--seed",
            str(seed),
        ],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def check_integrity(path: str) -> tuple[bool, str]:
    """Reopen the collection and verify no corruption.

    Returns ``(ok, detail)``. ``ok`` is True only if the collection opens, the
    SQLite physical integrity check passes, and Anki's own database sanity check
    finds zero problems.
    """
    from anki.collection import Collection

    try:
        col = Collection(path)
    except Exception as err:  # pragma: no cover - failure path
        return False, f"reopen failed: {err!r}"

    try:
        physical = col.db.scalar("pragma integrity_check")
        if physical != "ok":
            return False, f"pragma integrity_check = {physical!r}"

        fk = col.db.all("pragma foreign_key_check")
        if fk:
            return False, f"foreign_key_check found {len(fk)} violation(s)"

        _msg, sane = col.fix_integrity()
        if not sane:
            return False, f"sanity check reported problems: {_msg!r}"

        card_count = col.db.scalar("select count(*) from cards")
        return True, f"ok (cards={card_count})"
    except Exception as err:  # pragma: no cover - failure path
        return False, f"integrity check raised: {err!r}"
    finally:
        col.close(downgrade=False)


def run_iteration(
    index: int, path: str, seed: int, rng: random.Random
) -> tuple[bool, str]:
    heartbeat = os.path.join(common.CACHE_DIR, f"hb_{os.getpid()}_{index}")
    for stale in (heartbeat, f"{heartbeat}.tmp"):
        if os.path.exists(stale):
            os.unlink(stale)

    proc = spawn_writer(path, heartbeat, seed + index)
    try:
        # Wait until the worker has actually committed several writes, so the
        # kill lands while the DB is being mutated.
        deadline = time.time() + 15.0
        while _read_heartbeat(heartbeat) < 3:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                return False, f"worker exited before writing: {err[:400]}"
            if time.time() > deadline:
                return False, "worker never reached write threshold"
            time.sleep(0.005)

        # Let it run a little longer at a random offset, then kill it abruptly
        # mid-write (SIGKILL: no cleanup, no flush, no clean close).
        time.sleep(rng.uniform(0.01, 0.25))
        writes_at_kill = _read_heartbeat(heartbeat)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - safety net
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        for leftover in (heartbeat, f"{heartbeat}.tmp"):
            if os.path.exists(leftover):
                os.unlink(leftover)

    ok, detail = check_integrity(path)
    return ok, f"killed@{writes_at_kill} writes -> {detail}"


def main_parent(args: argparse.Namespace) -> int:
    print("=" * 78)
    print("Anki crash-resilience / no-corruption test (PRD 7g)")
    print("=" * 78)

    os.makedirs(common.CACHE_DIR, exist_ok=True)
    master = _crash_master_path(args.cards, args.seed)
    live = _crash_live_path(args.cards, args.seed)

    if args.rebuild or not os.path.exists(master):
        print(f"generating deterministic crash deck ({args.cards} cards)...")
        common.generate_collection(master, args.cards, args.seed)

    # Fresh live copy that we will crash repeatedly.
    import shutil

    for ext in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(live + ext):
            os.unlink(live + ext)
    shutil.copy(master, live)

    print(
        f"running {args.iterations} crash iterations against {live}\n"
        f"(open -> review writes -> SIGKILL mid-write -> reopen -> integrity)\n"
    )

    rng = random.Random(args.seed)
    failures: list[tuple[int, str]] = []
    for i in range(1, args.iterations + 1):
        ok, detail = run_iteration(i, live, args.seed, rng)
        status = "PASS" if ok else "FAIL"
        print(f"  iter {i:>3}/{args.iterations}  {status}  {detail}")
        if not ok:
            failures.append((i, detail))

    print()
    passed = args.iterations - len(failures)
    print(f"result: {passed}/{args.iterations} iterations with ZERO corruption")

    if not args.keep:
        for ext in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(live + ext):
                os.unlink(live + ext)

    if failures:
        print("\nCORRUPTION DETECTED:")
        for idx, detail in failures:
            print(f"  iter {idx}: {detail}")
        return 1

    print("PASS: no corruption across all iterations.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=25, help=">= 20 required")
    parser.add_argument("--cards", type=int, default=3_000, help="crash deck size")
    parser.add_argument("--seed", type=int, default=777, help="generation seed")
    parser.add_argument("--rebuild", action="store_true", help="regenerate the deck")
    parser.add_argument("--keep", action="store_true", help="keep the live DB file")
    # Hidden worker mode (re-invoked as a child process).
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--path", help=argparse.SUPPRESS)
    parser.add_argument("--heartbeat", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        assert args.path and args.heartbeat
        return run_worker(args.path, args.heartbeat, args.seed)

    if args.iterations < 20:
        parser.error("crash test requires at least 20 iterations (PRD 7g)")
    return main_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
