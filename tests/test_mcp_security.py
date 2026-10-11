from butian import mcp_server
from butian.runtime.service import ButianService


def test_mcp_member_memory_requires_actor_and_token(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m1", "Member")
        service.store.set_member_token("m1", "secret")

        try:
            mcp_server.call_tool(service, "butian_memory_search", {"member_id": "m1", "query": "x"})
        except ValueError as exc:
            assert str(exc) == "actor_member_id_required"
        else:
            raise AssertionError("missing actor must be rejected")

        try:
            mcp_server.call_tool(service, "butian_memory_search", {"member_id": "m1", "actor_member_id": "m1", "token": "wrong", "query": "x"})
        except ValueError as exc:
            assert str(exc) == "member_authentication_failed"
        else:
            raise AssertionError("wrong token must be rejected")

        result = mcp_server.call_tool(service, "butian_memory_search", {"member_id": "m1", "actor_member_id": "m1", "token": "secret", "query": "x"})
        assert result == {"memories": []}
    finally:
        service.close()
