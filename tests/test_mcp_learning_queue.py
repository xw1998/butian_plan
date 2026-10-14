import pytest
from butian.mcp_server import call_tool
from butian.runtime.service import ButianService


def test_standard_queue_requires_owner_authentication(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        service.store.set_member_token('a', 'test-a')
        args = dict(member_id='a', actor_member_id='a', token='test-a', url='https://example.org/source')
        first = call_tool(service, 'butian_knowledge_source_enqueue', args)
        assert call_tool(service, 'butian_knowledge_source_enqueue', args) == first
        with pytest.raises(ValueError):
            call_tool(service, 'butian_knowledge_source_enqueue', dict(args, member_id='b'))
        with pytest.raises(ValueError):
            call_tool(service, 'butian_knowledge_source_queue', dict(args, member_id='b'))
        rows = call_tool(service, 'butian_knowledge_source_queue', args)['queue']
        assert len(rows) == 1 and rows[0]['owner_member_id'] == 'a'
    finally:
        service.close()

def test_queue_schema_exposes_required_authentication():
    from butian.mcp_server import TOOLS
    for tool in TOOLS:
        if tool['name'] in {'butian_knowledge_source_enqueue', 'butian_knowledge_source_queue'}:
            schema = tool['inputSchema']
            assert {'actor_member_id', 'token'} <= set(schema['required'])
            assert {'actor_member_id', 'token'} <= set(schema['properties'])
