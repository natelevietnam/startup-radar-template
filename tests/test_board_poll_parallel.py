"""poll_resolved: hosts run alongside each other, each host still one at a time.

Polling 220 boards serially cost the SUM of the three hosts, because the
one-request-per-second limit is per host: Ashby's 122 boards spent ~122s
waiting on Ashby's own rate limit while Greenhouse sat idle. The fix gives each
provider one worker, so the run costs the slowest host instead of all of them.

The property worth testing is not "it is faster" — a timing assertion on a busy
machine is a flaky test. It is the two-part shape the politeness promise depends
on:

  * different providers overlap in time (otherwise there was no point), and
  * within one provider, no two fetches are ever in flight at once (otherwise
    one worker per host is not actually holding the host to one at a time, and
    _throttle is being asked to referee a race it was never given).

A fake provider module stands in for the HTTP, so this exercises the scheduling
and nothing else.
"""

from __future__ import annotations

import threading
import time

import pytest

import database
from sources.jobs import resolve as R


class FakeProvider:
    """Records the overlap of its own calls, and sleeps so overlap is visible."""

    def __init__(self, name: str, delay: float = 0.05):
        self.name = name
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.windows: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def fetch_board(self, token, company_name, use_cache=True):
        start = time.monotonic()
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.in_flight -= 1
                self.windows.append((start, time.monotonic()))
        return [{"provider": self.name, "provider_job_id": f"{token}-1",
                 "company_name": company_name}]


def _overlaps(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


@pytest.fixture
def staged(monkeypatch):
    fakes = {n: FakeProvider(n) for n in ("ashby", "greenhouse", "lever")}
    boards = []
    for name, count in (("ashby", 4), ("greenhouse", 4), ("lever", 2)):
        for i in range(count):
            boards.append({"canon": f"{name}{i}", "company_name": f"Co {name}{i}",
                           "provider": name, "token": f"{name}{i}"})
    monkeypatch.setattr(R, "PROVIDERS", fakes)
    monkeypatch.setattr(database, "resolved_boards", lambda: boards)
    monkeypatch.setattr(database, "upsert_board",
                        lambda *a, **k: pytest.fail("no board failed; nothing to write"))
    return fakes, boards


def test_every_board_is_polled_exactly_once(staged):
    fakes, boards = staged
    rows = R.poll_resolved()
    assert len(rows) == len(boards)
    assert len({r["provider_job_id"] for r in rows}) == len(boards)


def test_one_host_never_has_two_fetches_in_flight(staged):
    """One worker per host is what keeps the per-host rate limit honest."""
    fakes, _ = staged
    R.poll_resolved()
    for name, fake in fakes.items():
        assert fake.max_in_flight == 1, f"{name} overlapped with itself"


def test_different_hosts_do_overlap(staged):
    """The whole reason for the change: hosts stop queueing behind each other."""
    fakes, _ = staged
    R.poll_resolved()
    assert _overlaps(fakes["ashby"].windows, fakes["greenhouse"].windows), \
        "ashby and greenhouse ran strictly one after the other"


def test_an_unknown_provider_is_skipped_not_crashed(monkeypatch):
    fake = FakeProvider("ashby")
    boards = [{"canon": "a", "company_name": "A", "provider": "ashby", "token": "a"},
              {"canon": "b", "company_name": "B", "provider": "nosuch", "token": "b"}]
    monkeypatch.setattr(R, "PROVIDERS", {"ashby": fake})
    monkeypatch.setattr(database, "resolved_boards", lambda: boards)
    rows = R.poll_resolved()
    assert len(rows) == 1


def test_no_resolved_boards_is_not_a_zero_worker_pool(monkeypatch):
    """ThreadPoolExecutor(max_workers=0) raises; an empty board list must not."""
    monkeypatch.setattr(R, "PROVIDERS", {"ashby": FakeProvider("ashby")})
    monkeypatch.setattr(database, "resolved_boards", lambda: [])
    assert R.poll_resolved() == []


def test_a_failing_board_is_recorded_after_the_workers_join(monkeypatch):
    """Writes happen on the calling thread, so SQLite sees one writer."""
    class Boom(FakeProvider):
        def fetch_board(self, token, company_name, use_cache=True):
            raise R.BoardError("HTTP 404 — no such board token")

    written: list[tuple] = []
    main_thread = threading.current_thread().ident
    monkeypatch.setattr(R, "PROVIDERS", {"ashby": Boom("ashby")})
    monkeypatch.setattr(database, "resolved_boards",
                        lambda: [{"canon": "a", "company_name": "A",
                                  "provider": "ashby", "token": "a"}])
    monkeypatch.setattr(database, "upsert_board",
                        lambda *a, **k: written.append((threading.current_thread().ident, k)))
    rows = R.poll_resolved()
    assert rows == []
    assert len(written) == 1
    assert written[0][0] == main_thread, "board error was written from a worker thread"
    assert "404" in written[0][1]["error"]
