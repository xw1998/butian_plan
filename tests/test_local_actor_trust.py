"""本机免令牌模式下的本地信任必须是逐成员记账的。

历史缺陷：service.local_actor 是单值全局状态，HTTP 服务是多线程的，两名成员同时
发请求时会互相把对方的本地信任清成 None，导致同一台机器上的正常调用被误判成
member_authentication_failed。这里用集合语义锁死“登记/撤销只影响被点名的成员”。
"""

import pytest

from butian.runtime.service import ButianService


@pytest.fixture()
def service(tmp_path):
    svc = ButianService(tmp_path)
    svc.add_member("m1", "Member One")
    svc.add_member("m2", "Member Two")
    try:
        yield svc
    finally:
        svc.close()


def test_two_members_keep_their_local_trust_independently(service):
    service.enable_local_actor("m1")
    service.enable_local_actor("m2")
    # 双方都不带口令（本机免令牌），谁都不该因为另一个人登记过就失去信任。
    service.require_member_access("m1", actor_member_id="m1")
    service.require_member_access("m2", actor_member_id="m2")
    service.require_member_access("m1", actor_member_id="m1")


def test_revoking_one_member_does_not_revoke_another(service):
    service.enable_local_actor("m1")
    service.enable_local_actor("m2")
    service.disable_local_actor("m2")
    service.require_member_access("m1", actor_member_id="m1")
    with pytest.raises(ValueError, match="member_authentication_failed"):
        service.require_member_access("m2", actor_member_id="m2")


def test_revoking_everything_requires_credentials(service):
    service.enable_local_actor("m1")
    service.disable_local_actor()
    with pytest.raises(ValueError, match="member_authentication_failed"):
        service.require_member_access("m1", actor_member_id="m1")


def test_local_actor_single_value_view_stays_compatible(service):
    assert service.local_actor is None
    service.local_actor = "m1"
    assert service.local_actor == "m1"
    service.enable_local_actor("m2")
    # 多名成员同时受信时，单值视图给不出“唯一代表”，返回 None 而不是随便挑一个。
    assert service.local_actor is None
    service.local_actor = None
    assert service.local_actor is None
    with pytest.raises(ValueError, match="member_authentication_failed"):
        service.require_member_access("m1", actor_member_id="m1")


def test_local_trust_cannot_cross_member_boundary(service):
    service.enable_local_actor("m1")
    # 本地信任只放行“本人代表本人”，不能拿 m1 的信任去操作 m2。
    with pytest.raises(ValueError, match="member_memory_access_denied"):
        service.require_member_access("m2", actor_member_id="m1")


def test_concurrent_members_do_not_knock_each_other_out(service):
    import threading

    errors = []

    def worker(member):
        for _ in range(200):
            # 登记与校验都要收进 try：旧写法只包住后半段，线程在 enable_local_actor
            # 上抛异常会静默死掉，用例照样“通过”——等于没测。
            try:
                service.enable_local_actor(member)
                service.require_member_access(member, actor_member_id=member)
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append((member, exc))
                return

    threads = [threading.Thread(target=worker, args=(member,)) for member in ("m1", "m2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [], errors[:3]


def test_switching_on_token_requirement_at_runtime_drops_local_trust(service, monkeypatch):
    service.enable_local_actor("m1")
    service.require_member_access("m1", actor_member_id="m1")
    monkeypatch.setenv("BUTIAN_MCP_TOKEN_REQUIRED", "1")
    # 运行期收紧口令后，先前登记的本地信任必须立刻失效。
    with pytest.raises(ValueError, match="member_authentication_failed"):
        service.require_member_access("m1", actor_member_id="m1")


def test_enable_local_actor_rejects_unknown_member(service):
    with pytest.raises(ValueError, match="member_not_found"):
        service.enable_local_actor("nobody")
    assert service.local_actor is None
