import pytest
from butian.runtime.service import ButianService


def test_repeated_secondary_hypothesis_evidence_is_rejected_before_team_commit(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'M')
        h = service.hypotheses.propose('practice_hypothesis', 'verify all evidence', rationale='independence', assessment=service.hypotheses.assess(['skill']))
        def action():
            a = service.hypotheses.authorize(h, 'experiment', cost=1, risk=0, information_gain=1)
            service.hypotheses.grant_authorization(a, actor_member_id='m', reason='approved', authorized=True)
            service.hypotheses.start(a)
            return a
        def evidence(name, text):
            p = tmp_path/name
            p.write_text(text, encoding='utf-8')
            return service.store.record_evidence(p, owner_member_id='m', actor_member_id='m')
        first, shared, second = evidence('a', 'first run'), evidence('shared', 'same measurement'), evidence('b', 'second run')
        result = service.observe_hypothesis(action(), 'supported', [first, shared], independent_key='first', actor_member_id='m', member_id='m')
        a = action()
        with pytest.raises(ValueError, match='independent_evidence_required'):
            service.observe_hypothesis(a, 'supported', [second, shared], independent_key='second', actor_member_id='m', member_id='m')
        assert service.store.db.execute('SELECT status FROM hypothesis_actions WHERE action_id=?', (a,)).fetchone()[0] == 'running'
        assert service.store.db.execute('SELECT COUNT(*) FROM hypothesis_observations').fetchone()[0] == 1
        memory = service.memory('m')
        try:
            assert memory.db.execute('SELECT COUNT(*) FROM practice_evidence WHERE memory_id=?', (result['candidate_memory_id'],)).fetchone()[0] == 2
            refs = memory.get(result['candidate_memory_id'])['source_event_ids']
            assert first in refs and shared in refs
        finally:
            memory.close()
    finally:
        service.close()
