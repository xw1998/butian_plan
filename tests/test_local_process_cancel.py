import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def test_cancel_running_process_and_prevent_reexecution(tmp_path, operator_local_provider):
    marker = tmp_path / 'started.txt'
    service, provider, session, remote = operator_local_provider(
        [sys.executable, '-c', 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(30)', str(marker)], '工作')
    started = threading.Event()
    original_factory = provider.execution_request_factory
    def observed_factory(*args):
        request = original_factory(*args)
        started.set()
        return request
    provider.execution_request_factory = observed_factory
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: list(provider.stream(remote, '工作')))
        assert started.wait(5)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert marker.read_text() == 'started'
        assert provider.cancel(remote).status == 'cancelled'
        events = future.result(timeout=15)
    assert events[-1].status == 'cancelled'
    assert not provider._executor_sessions
    assert list(provider.stream(remote, '重复工作'))[-1].status == 'cancelled'
