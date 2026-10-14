import sqlite3
import pytest
from butian.runtime.exploration_budget import ExplorationBudget


def test_budget_is_persistent_and_duplicate_start_is_free(tmp_path):
    path = tmp_path / 'budget.db'
    db = sqlite3.connect(path, isolation_level=None)
    budget = ExplorationBudget(db)
    budget.configure(max_cost=2, max_actions=2, max_risk=0.2, window_seconds=3600)
    budget.reserve('first', 'worker', cost=1, risk=0.1, now=100)
    budget.reserve('first', 'worker', cost=1, risk=0.1, now=101)
    db.close()
    db = sqlite3.connect(path, isolation_level=None)
    budget = ExplorationBudget(db)
    budget.reserve('second', 'worker', cost=1, risk=0.1, now=102)
    with pytest.raises(ValueError, match='exploration_budget_exceeded'):
        budget.reserve('third', 'worker', cost=1, risk=0.1, now=103)
    assert db.execute('SELECT COUNT(*) FROM exploration_reservations').fetchone()[0] == 2
    budget.reserve('third', 'worker', cost=1, risk=0.1, now=3703)
    db.close()


@pytest.mark.parametrize('change', [{'max_cost': True}, {'max_cost': float('nan')}, {'max_actions': 0}, {'max_risk': 2}, {'window_seconds': -1}])
def test_invalid_policy_cannot_replace_valid_policy(change):
    db = sqlite3.connect(':memory:', isolation_level=None)
    budget = ExplorationBudget(db)
    args = dict(max_cost=2, max_actions=2, max_risk=0.2, window_seconds=3600)
    budget.configure(**args)
    with pytest.raises(ValueError, match='invalid_exploration_budget'):
        budget.configure(**dict(args, **change))
    assert budget.policy()['max_cost'] == 2
    db.close()


def test_risk_limit_and_outer_rollback():
    db = sqlite3.connect(':memory:', isolation_level=None)
    budget = ExplorationBudget(db)
    budget.configure(max_cost=2, max_actions=2, max_risk=0.2, window_seconds=3600)
    with pytest.raises(ValueError, match='exploration_risk_exceeded'):
        budget.reserve('risky', 'worker', cost=1, risk=0.3)
    db.execute('BEGIN IMMEDIATE')
    budget.reserve('rolled-back', 'worker', cost=1, risk=0.1)
    db.execute('ROLLBACK')
    assert db.execute('SELECT COUNT(*) FROM exploration_reservations').fetchone()[0] == 0
    db.close()


def test_hypothesis_start_enforces_persisted_budget_and_keeps_queue(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        h = service.hypotheses.propose('practice_hypothesis', 'bounded experiment', rationale='check', assessment=service.hypotheses.assess(['deduction']))
        budget = ExplorationBudget(service.store.db)
        budget.configure(max_cost=1, max_actions=1, max_risk=0.2, window_seconds=3600)
        actions = []
        for _ in range(2):
            action = service.authorize_hypothesis(h, 'measure', cost=1, risk=0.1, information_gain=1)
            service.grant_hypothesis_authorization(action, actor_member_id='worker', reason='test authorization', authorized=True)
            actions.append(action)
        service.start_hypothesis_action(actions[0])
        with pytest.raises(ValueError, match='exploration_budget_exceeded'):
            service.start_hypothesis_action(actions[1])
        assert service.store.db.execute('SELECT status FROM hypothesis_actions WHERE action_id=?', (actions[1],)).fetchone()[0] == 'queued'
    finally:
        service.close()


def test_independent_connections_cannot_overspend(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'concurrent.db'
    db = sqlite3.connect(path, isolation_level=None)
    ExplorationBudget(db).configure(max_cost=1, max_actions=1, max_risk=0.2, window_seconds=3600)
    db.close()
    def attempt(key):
        connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        try:
            ExplorationBudget(connection).reserve(key, 'worker', cost=1, risk=0.1)
            return 'started'
        except ValueError as exc:
            return str(exc)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['first', 'second']))
    assert sorted(results) == ['exploration_budget_exceeded', 'started']


def test_hypothesis_start_failure_rolls_back_budget(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        h = service.hypotheses.propose('practice_hypothesis', 'bounded', rationale='check', assessment=service.hypotheses.assess(['deduction']))
        action = service.authorize_hypothesis(h, 'measure', cost=1, risk=0.1, information_gain=1)
        service.grant_hypothesis_authorization(action, actor_member_id='worker', reason='authorized', authorized=True)
        ExplorationBudget(service.store.db)
        service.store.db.execute("CREATE TRIGGER reject_start BEFORE UPDATE OF status ON hypothesis_actions WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'injected start failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected start failure'):
            service.hypotheses.start(action)
        assert service.store.db.execute('SELECT COUNT(*) FROM exploration_reservations').fetchone()[0] == 0
        assert service.store.db.execute('SELECT status FROM hypothesis_actions WHERE action_id=?', (action,)).fetchone()[0] == 'queued'
    finally:
        service.close()


def test_dream_and_hypothesis_share_member_budget(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        try:
            memory.put('a', 'lesson', 'check centimeters')
            memory.put('b', 'lesson', 'check meters')
        finally:
            memory.close()
        distilled = service.dream_distill('worker', ['a', 'b'], summary='check units')
        dream_h = service.dream_propose_hypothesis('worker', distilled['distillation_id'], '125 cm is 1.25 m', prior_reliability=.5, argument='measure independently')['hypothesis_id']
        dream_a = service.dream_schedule_verification('worker', dream_h, 'measure', cost=1, risk=.1, expected_information_gain=1, authorized=True)['action_id']
        h = service.hypotheses.propose('practice_hypothesis', 'another measurement', rationale='check', assessment=service.hypotheses.assess(['deduction']), owner_member_id='worker')
        action = service.authorize_hypothesis(h, 'measure', cost=1, risk=.1, information_gain=1, owner_member_id='worker')
        service.grant_hypothesis_authorization(action, actor_member_id='worker', reason='approved', authorized=True, owner_member_id='worker')
        ExplorationBudget(service.store.db).configure(max_cost=1, max_actions=1, max_risk=.2, window_seconds=3600)
        service.start_hypothesis_action(action, owner_member_id='worker')
        with pytest.raises(ValueError, match='exploration_budget_exceeded'):
            service.dream_start_verification('worker', dream_a)
        memory = service.memory('worker')
        try:
            assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (dream_a,)).fetchone()[0] == 'queued'
        finally:
            memory.close()
    finally:
        service.close()


def test_member_policy_is_shared_and_audit_failure_rolls_back(tmp_path, monkeypatch):
    from butian.cli import main
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('worker', 'Worker')
    service.close()
    assert main(['exploration-budget', 'set', '--member-id', 'worker', '--max-cost', '1', '--max-actions', '1', '--max-risk', '.2', '--window-seconds', '3600'], home_dir=tmp_path) == 0
    service = ButianService(tmp_path)
    try:
        ledger = ExplorationBudget(service.store.db)
        assert ledger.policy('worker')['max_cost'] == 1
        ledger.reserve('one', 'worker', cost=1, risk=.1)
        with pytest.raises(ValueError, match='exploration_budget_exceeded'):
            ledger.reserve('two', 'worker', cost=1, risk=.1)
        assert ledger.policy()['max_cost'] == 50
        service.store.db.execute("CREATE TRIGGER reject_budget_event BEFORE INSERT ON events WHEN NEW.event_type='exploration_budget_configured' BEGIN SELECT RAISE(ABORT,'budget audit rejected'); END")
    finally:
        service.close()
    with pytest.raises(sqlite3.IntegrityError, match='budget audit rejected'):
        main(['exploration-budget', 'set', '--member-id', 'worker', '--max-cost', '2', '--max-actions', '2', '--max-risk', '.2', '--window-seconds', '3600'], home_dir=tmp_path)
    service = ButianService(tmp_path)
    try:
        assert ExplorationBudget(service.store.db).policy('worker')['max_cost'] == 1
    finally:
        service.close()


def test_dream_failed_member_commit_retains_single_charge(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        memory = service.memory('worker')
        memory.put('a', 'lesson', 'check centimeters')
        memory.put('b', 'lesson', 'check meters')
        memory.close()
        distilled = service.dream_distill('worker', ['a', 'b'], summary='check units')
        hypothesis = service.dream_propose_hypothesis('worker', distilled['distillation_id'], '125 cm is 1.25 m', prior_reliability=.5, argument='measure')['hypothesis_id']
        action = service.dream_schedule_verification('worker', hypothesis, 'measure', cost=1, risk=.1, expected_information_gain=1, authorized=True)['action_id']
        memory = service.memory('worker')
        memory.db.execute("CREATE TRIGGER reject_dream_start BEFORE UPDATE OF status ON verification_actions WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'member commit rejected'); END")
        memory.close()
        with pytest.raises(sqlite3.IntegrityError, match='member commit rejected'):
            service.dream_start_verification('worker', action)
        assert service.store.db.execute('SELECT COUNT(*) FROM exploration_reservations WHERE action_id=?', (action,)).fetchone()[0] == 1
        memory = service.memory('worker')
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (action,)).fetchone()[0] == 'queued'
        memory.db.execute('DROP TRIGGER reject_dream_start')
        memory.close()
        assert service.dream_start_verification('worker', action)['status'] == 'running'
        assert service.store.db.execute('SELECT COUNT(*) FROM exploration_reservations WHERE action_id=?', (action,)).fetchone()[0] == 1
    finally:
        service.close()


def test_legacy_member_budget_import_preserves_policy_charge_and_idempotency(tmp_path):
    legacy = sqlite3.connect(tmp_path / 'legacy.db', isolation_level=None)
    team = sqlite3.connect(tmp_path / 'team.db', isolation_level=None)
    try:
        old = ExplorationBudget(legacy)
        old.configure(max_cost=1, max_actions=1, max_risk=0.1, window_seconds=3600)
        old.reserve('old-attempt', None, cost=1, risk=0.1, now=100)
        current = ExplorationBudget(team)
        current.import_legacy_member(legacy, 'worker')
        current.import_legacy_member(legacy, 'worker')
        assert current.policy('worker')['max_cost'] == 1
        assert team.execute('SELECT COUNT(*) FROM exploration_reservations').fetchone()[0] == 1
        with pytest.raises(ValueError, match='exploration_budget_exceeded'):
            current.reserve('new-attempt', 'worker', cost=1, risk=0.1, now=101)
        assert legacy.execute('SELECT COUNT(*) FROM exploration_reservations').fetchone()[0] == 1
    finally:
        legacy.close()
        team.close()


def test_service_restart_imports_legacy_member_budget_once(tmp_path, monkeypatch):
    from butian.runtime.service import ButianService
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    service.add_member('worker', 'Worker')
    memory = service.memory('worker')
    try:
        budget = ExplorationBudget(memory.db)
        budget.configure(max_cost=1, max_actions=1, max_risk=0.1, window_seconds=3600)
        budget.reserve('legacy-start', None, cost=1, risk=0.1)
    finally:
        memory.close()
        service.close()
    for index in range(2):
        service = ButianService(tmp_path)
        try:
            current = ExplorationBudget(service.store.db)
            assert current.policy('worker')['max_cost'] == (1 if index == 0 else 2)
            assert service.store.db.execute("SELECT COUNT(*) FROM exploration_reservations WHERE owner='worker'").fetchone()[0] == 1
            if index == 0:
                with pytest.raises(ValueError, match='exploration_budget_exceeded'):
                    current.reserve('new-start', 'worker', cost=1, risk=0.1)
                current.configure(max_cost=2, max_actions=2, max_risk=0.1, window_seconds=3600, owner='worker')
        finally:
            service.close()


def test_legacy_conflict_rolls_back_policy_and_import_marker():
    legacy = sqlite3.connect(':memory:', isolation_level=None)
    team = sqlite3.connect(':memory:', isolation_level=None)
    try:
        old = ExplorationBudget(legacy)
        old.configure(max_cost=1, max_actions=1, max_risk=.1, window_seconds=3600)
        old.reserve('collision', None, cost=1, risk=.1, now=100)
        current = ExplorationBudget(team)
        current.reserve('collision', 'other', cost=1, risk=.1, now=100)
        with pytest.raises(ValueError, match='legacy_budget_reservation_conflict'):
            current.import_legacy_member(legacy, 'worker')
        assert team.execute('SELECT COUNT(*) FROM exploration_member_policy').fetchone()[0] == 0
        assert team.execute('SELECT COUNT(*) FROM exploration_legacy_imports').fetchone()[0] == 0
        assert team.execute('SELECT owner FROM exploration_reservations').fetchone()[0] == 'other'
    finally:
        legacy.close()
        team.close()
