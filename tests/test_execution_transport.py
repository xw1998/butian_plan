import sqlite3
import threading

import pytest

from butian.runtime.execution_contract import ExecutionCapabilities, ExecutionObservation
from butian.runtime.execution_protocol import MessageVerifier
from butian.runtime.execution_transport import LoopbackExecutionClient, LoopbackExecutionServer


class Adapter:
    def capabilities(self):
        return ExecutionCapabilities('owner', 'instance', {})

    def inspect(self, execution_id):
        return ExecutionObservation(execution_id, 'owner', 'instance', 1, 'unknown')


def test_actual_loopback_round_trip_and_identity():
    key = b'z' * 32
    server_db = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
    client_db = sqlite3.connect(':memory:', isolation_level=None)
    server = LoopbackExecutionServer(('127.0.0.1', 0), Adapter(), MessageVerifier(server_db, key, 'owner', 'instance'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LoopbackExecutionClient('http://127.0.0.1:' + str(server.server_port), MessageVerifier(client_db, key, 'owner', 'instance'))
        assert client.capabilities().executor_id == 'owner'
        assert client.inspect('e1').state == 'unknown'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server_db.close()
        client_db.close()
    assert not thread.is_alive()


@pytest.mark.parametrize('endpoint', ['http://localhost:80', 'http://192.168.1.1:80', 'https://127.0.0.1:80', 'http://user@127.0.0.1:80', 'http://127.0.0.1:80/?x=1'])
def test_nonlocal_or_ambiguous_endpoint_rejected(endpoint):
    db = sqlite3.connect(':memory:', isolation_level=None)
    try:
        with pytest.raises(ValueError, match='executor_loopback_required'):
            LoopbackExecutionClient(endpoint, MessageVerifier(db, b'z' * 32, 'owner', 'instance'))
    finally:
        db.close()


def test_maximum_tool_output_fits_signed_wire_budget():
    from butian.runtime.execution_protocol import MAX_MESSAGE_BYTES, canonical, sign_message
    # Two bounded streams, with JSON's worst-case six-byte escaping.
    text = '\x00' * (4 * 1024 * 1024)
    message = sign_message(b'z' * 32, 'owner', 'instance', 'inspect',
                           {'stdout': text, 'stderr': text})
    assert len(canonical(message)) <= MAX_MESSAGE_BYTES
