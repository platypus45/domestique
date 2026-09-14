"""cache.cached computes a cold key once, however many threads ask for it at
the same moment. Before the lock a cold home page fired four concurrent
archive parses through it (the audit's performance lens, 2026-09-14)."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import cache  # noqa: E402


def _slow_counter(calls, delay=0.2):
    def fn():
        calls.append(threading.get_ident())
        time.sleep(delay)
        return {"n": len(calls)}
    return fn


def test_concurrent_cold_reads_compute_once():
    cache.clear_cache()
    calls: list = []
    fn = _slow_counter(calls)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(cache.cached("sf-key", fn)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, f"fn ran {len(calls)} times for one cold key"
    assert results == [{"n": 1}] * 4


def test_distinct_keys_do_not_wait_for_each_other():
    cache.clear_cache()
    calls: list = []
    fn = _slow_counter(calls, delay=0.3)
    started = time.perf_counter()
    threads = [threading.Thread(target=lambda k=k: cache.cached(k, fn)) for k in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    assert len(calls) == 3
    assert elapsed < 0.75, f"three keys took {elapsed:.2f}s: they serialised"


def test_failure_still_sticks_briefly_and_does_not_deadlock():
    cache.clear_cache()

    def boom():
        raise RuntimeError("no network")
    assert cache.cached("bad-key", boom) == {}
    # A second call inside the 30 s window is served from the stuck value.
    assert cache.cached("bad-key", boom) == {}
