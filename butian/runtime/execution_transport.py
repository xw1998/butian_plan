"""Bounded authenticated loopback transport, independent of MCP sessions."""
from __future__ import annotations

import base64
import json
import threading
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .execution_contract import CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, ExecutionRequest
from .execution_protocol import MAX_MESSAGE_BYTES, MessageVerifier, canonical, sign_message


def request_payload(request: ExecutionRequest) -> dict:
    value = asdict(request)
    value['stdin'] = base64.b64encode(request.stdin).decode('ascii')
    return value


def decode_request(value: dict) -> ExecutionRequest:
    if not isinstance(value, dict) or set(value) != {'execution_id', 'request_sha256', 'argv', 'cwd', 'stdin', 'timeout', 'output_limit', 'permit'}:
        raise ValueError('execution_request_fields_invalid')
    try:
        stdin = base64.b64decode(value['stdin'], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError('execution_request_input_invalid') from exc
    if not isinstance(value['argv'], list) or not isinstance(value['permit'], dict):
        raise ValueError('execution_request_invalid')
    return ExecutionRequest(**{**value, 'argv': tuple(value['argv']), 'stdin': stdin})


class LoopbackExecutionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, adapter, verifier: MessageVerifier):
        if address[0] != '127.0.0.1':
            raise ValueError('executor_loopback_required')
        self.adapter, self.verifier = adapter, verifier
        # CLI verifier and owner use the same SQLite connection; nonce writes
        # must not interleave with an owner's savepoint transaction.
        self.auth_lock = getattr(adapter, '_lock', threading.RLock())
        super().__init__(address, ExecutionHandler)


class ExecutionHandler(BaseHTTPRequestHandler):
    server_version = 'ButianExecutor/1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            if self.path != '/execution/v1' or self.client_address[0] != '127.0.0.1':
                raise ValueError('executor_endpoint_invalid')
            if any(self.headers.get(name) is not None for name in ('Origin', 'Forwarded', 'X-Forwarded-For', 'Transfer-Encoding')):
                raise ValueError('executor_proxy_or_browser_rejected')
            expected_host = '127.0.0.1:' + str(self.server.server_address[1])
            if self.headers.get('Host') != expected_host or self.headers.get_content_type() != 'application/json':
                raise ValueError('executor_request_headers_invalid')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError('executor_message_length_invalid')
            size = int(lengths[0])
            if size <= 0 or size > MAX_MESSAGE_BYTES:
                raise ValueError('executor_message_too_large')
            self.connection.settimeout(5)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('executor_message_incomplete')
            message = json.loads(raw)
            with self.server.auth_lock:
                verified = self.server.verifier.verify(message)
            payload, action = verified['payload'], verified['action']
            adapter = self.server.adapter
            if action == 'capabilities':
                result = asdict(adapter.capabilities())
            elif action == 'start':
                result = asdict(adapter.start(decode_request(payload)))
            elif action == 'inspect':
                result = asdict(adapter.inspect(payload['execution_id']))
            elif action == 'events':
                result = [asdict(value) for value in adapter.events(payload['execution_id'], payload.get('after_sequence', 0))]
            elif action == 'cancel':
                result = asdict(adapter.cancel(payload['execution_id'], reason=payload['reason']))
            elif action == 'authorize':
                result = adapter.authorize(payload['authorization_id'], payload['generation'], payload['expires_at'])
            elif action == 'renew':
                result = adapter.renew(payload['execution_id'], payload['lease_seconds'])
            elif action == 'revoke':
                result = adapter.revoke(payload['authorization_id'], payload['generation'])
            else:
                raise ValueError('executor_action_invalid')
            response = sign_message(self.server.verifier._key, verified['executor_id'], verified['instance_id'],
                                    action, {'request_nonce': verified['nonce'], 'result': result})
            self._reply(200, response)
        except (ValueError, TypeError, KeyError):
            # Payloads and adapter exception strings may contain sensitive data.
            self._reply(400, {'error': 'executor_request_rejected'})
        except Exception:
            self._reply(503, {'error': 'executor_operation_unconfirmed'})

    def _reply(self, status, value):
        encoded = canonical(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(encoded)


class LoopbackExecutionClient:
    def __init__(self, endpoint: str, verifier: MessageVerifier, *, timeout: float = 10):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'} or parsed.port is None):
            raise ValueError('executor_loopback_required')
        self.endpoint, self.verifier, self.timeout = endpoint.rstrip('/') + '/execution/v1', verifier, timeout
        self._lock = threading.RLock()
        # Never send executor authentication through an environment HTTP proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, action: str, payload: dict):
        with self._lock:
            message = sign_message(self.verifier._key, self.verifier.executor_id, self.verifier.instance_id, action, payload)
            request = urllib.request.Request(self.endpoint, data=canonical(message), headers={'Content-Type': 'application/json'}, method='POST')
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_MESSAGE_BYTES + 1)
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError('executor_message_too_large')
            verified = self.verifier.verify(json.loads(raw))
            if verified['action'] != action or verified['payload'].get('request_nonce') != message['nonce']:
                raise ValueError('executor_response_mismatch')
            return verified['payload']['result']

    def capabilities(self):
        value = self._call('capabilities', {})
        return ExecutionCapabilities(value['executor_id'], value['instance_id'],
                                     {name: CapabilityEvidence(**evidence) for name, evidence in value['features'].items()})

    def start(self, request):
        return ExecutionObservation(**self._call('start', request_payload(request)))

    def inspect(self, execution_id):
        return ExecutionObservation(**self._call('inspect', {'execution_id': execution_id}))

    def events(self, execution_id, after_sequence=0):
        return [ExecutionObservation(**value) for value in self._call('events', {'execution_id': execution_id, 'after_sequence': after_sequence})]

    def cancel(self, execution_id, *, reason):
        return ExecutionObservation(**self._call('cancel', {'execution_id': execution_id, 'reason': reason}))

    def authorize(self, authorization_id, generation, expires_at):
        return self._call('authorize', {'authorization_id': authorization_id, 'generation': generation, 'expires_at': expires_at})

    def renew(self, execution_id, lease_seconds):
        return self._call('renew', {'execution_id': execution_id, 'lease_seconds': lease_seconds})

    def revoke(self, authorization_id, generation):
        return self._call('revoke', {'authorization_id': authorization_id, 'generation': generation})

    def close(self):
        pass  # Closing a client does not shut down an independently owned server.
