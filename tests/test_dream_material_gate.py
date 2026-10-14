import pytest
from butian.runtime.memory import MemoryStore
from butian.runtime.dream import DreamRunner


def test_archived_or_cold_material_cannot_create_empty_completed_dream(tmp_path):
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('cold', 'experience', '历史材料', status='cold')
        result = DreamRunner(memory).run(min_fragments=1, min_interval=0)
        assert result['status'] == 'skipped'
        assert result['reason'] == 'insufficient_valid_material'
        assert memory.db.execute('SELECT COUNT(*) FROM dream_candidates').fetchone()[0] == 0
    finally:
        memory.close()


def test_unchanged_material_does_not_repeat_dream_after_interval(tmp_path):
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('first', 'experience', '已实践的材料', status='warm', source_event_ids=['source'])
        runner = DreamRunner(memory)
        first = runner.run(min_fragments=1, min_interval=0)
        assert first['status'] == 'completed'
        again = runner.run(min_fragments=1, min_interval=0)
        assert again['status'] == 'skipped'
        assert again['reason'] == 'insufficient_new_fragments'
        memory.put('second', 'lesson', '新增失败观察', status='candidate', source_event_ids=['second-source'])
        assert runner.run(min_fragments=1, min_interval=0)['status'] == 'completed'
    finally:
        memory.close()


def test_processed_material_survives_restart_and_budget_preserves_pending(tmp_path):
    path = tmp_path / 'memory.db'
    memory = MemoryStore(path)
    try:
        for index in range(3):
            memory.put(str(index), 'experience', f'独立材料{index}', status='candidate')
        assert DreamRunner(memory).run(min_fragments=1, min_interval=0, budget=1)['status'] == 'completed'
    finally:
        memory.close()
    memory = MemoryStore(path)
    try:
        runner = DreamRunner(memory)
        assert runner.run(min_fragments=1, min_interval=0, budget=1)['status'] == 'completed'
        assert runner.run(min_fragments=1, min_interval=0, budget=1)['status'] == 'completed'
        assert runner.run(min_fragments=1, min_interval=0)['reason'] == 'insufficient_new_fragments'
        memory.put('0', 'experience', '实质修订后的材料', status='candidate')
        assert runner.run(min_fragments=1, min_interval=0)['status'] == 'completed'
    finally:
        memory.close()

def test_candidate_write_failure_rolls_back_material_run_and_lock(tmp_path):
    import pytest
    import sqlite3
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('one', 'experience', '真实观察材料', status='candidate')
        runner = DreamRunner(memory)
        memory.db.execute("CREATE TRIGGER fail_dream BEFORE INSERT ON dream_candidates BEGIN SELECT RAISE(ABORT, 'injected_candidate_failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected_candidate_failure'):
            runner.run(min_fragments=1, min_interval=0)
        for table in ('dream_runs', 'dream_candidates', 'dream_materials', 'dream_lock'):
            assert memory.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        memory.db.execute('DROP TRIGGER fail_dream')
        assert runner.run(min_fragments=1, min_interval=0)['status'] == 'completed'
    finally:
        memory.close()

def test_two_runtimes_consume_new_material_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = tmp_path / 'memory.db'
    first = MemoryStore(path)
    second = MemoryStore(path)
    try:
        first.put('one', 'experience', '一次独立观察', status='candidate')
        runners = [DreamRunner(first), DreamRunner(second)]
        barrier = Barrier(2)
        def run(runner):
            barrier.wait()
            return runner.run(min_fragments=1, min_interval=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, runners))
        assert sorted(r['status'] for r in results) == ['completed', 'skipped']
        assert first.db.execute('SELECT COUNT(*) FROM dream_candidates').fetchone()[0] == 1
        assert first.db.execute('SELECT COUNT(*) FROM dream_materials').fetchone()[0] == 1
    finally:
        first.close()
        second.close()

def test_abstraction_keeps_pattern_and_revises_after_sources_change(tmp_path):
    path = tmp_path / 'memory.db'
    memory = MemoryStore(path)
    try:
        memory.put('a', 'lesson', '输入必须核对单位')
        memory.put('b', 'lesson', '输出必须核对单位')
        runner = DreamRunner(memory)
        first = runner.abstract_memories(['a', 'b'], pattern='量纲一致时才能合并', applicability=['相同单位'], failure_conditions=['单位未知'])
        assert first['pattern'] == '量纲一致时才能合并'
        same = runner.abstract_memories(['b', 'a'], pattern='量纲一致时才能合并', applicability=['相同单位'], failure_conditions=['单位未知'])
        assert same['abstraction_id'] == first['abstraction_id']
        changed = runner.abstract_memories(['a', 'b'], pattern='量纲一致时才能合并', applicability=['必须先换算'], failure_conditions=['单位未知'])
        assert changed['abstraction_id'] != first['abstraction_id']
        memory.put('a', 'lesson', '输入必须核对单位且先换算')
        revised = runner.abstract_memories(['a', 'b'], pattern='量纲一致时才能合并', applicability=['相同单位'], failure_conditions=['单位未知'])
        assert revised['abstraction_id'] != first['abstraction_id']
    finally:
        memory.close()
    memory = MemoryStore(path)
    try:
        assert memory.db.execute('SELECT pattern FROM memory_abstractions WHERE abstraction_id=?', (first['abstraction_id'],)).fetchone()[0] == '量纲一致时才能合并'
    finally:
        memory.close()


def test_hypothesis_can_reference_current_abstraction_and_distillation(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'lesson', '厘米到米除以100')
        memory.put('b', 'lesson', '毫米到米除以1000')
        runner = DreamRunner(memory)
        abstract = runner.abstract_memories(['a', 'b'], pattern='换算先核对单位')
        distilled = runner.distill_memories(['a', 'b'], summary='单位换算需要对应分母')
        for reference in (abstract['abstraction_id'], distilled['distillation_id']):
            hypothesis = runner.propose_hypothesis(reference, '125厘米等于1.25米', prior_reliability=.5, argument='验证单位换算')
            assert memory.db.execute('SELECT candidate_id FROM hypotheses WHERE hypothesis_id=?', (hypothesis,)).fetchone()[0] == reference
        memory.put('a', 'lesson', '厘米到米除以100，禁止除以10')
        for reference in (abstract['abstraction_id'], distilled['distillation_id']):
            with pytest.raises(ValueError, match='dream_source_snapshot_stale'):
                runner.propose_hypothesis(reference, '旧来源验证', prior_reliability=.5, argument='来源已改')
    finally:
        memory.close()


def test_dream_source_snapshot_blocks_concurrent_revision(tmp_path):
    import sqlite3
    import pytest
    path = tmp_path / 'memory.db'
    memory = MemoryStore(path)
    other = MemoryStore(path)
    try:
        memory.put('a', 'lesson', '单位核对')
        memory.put('b', 'lesson', '分母核对')
        runner = DreamRunner(memory)
        other.db.execute('PRAGMA busy_timeout=0')
        original_get = memory.get
        attempts = []
        def observed_get(mid):
            if not attempts:
                attempts.append(mid)
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    other.db.execute("UPDATE memories SET content='并发修改' WHERE memory_id='a'")
            return original_get(mid)
        memory.get = observed_get
        runner.abstract_memories(['a', 'b'], pattern='先核对单位再换算')
        attempts.clear()
        runner.distill_memories(['a', 'b'], summary='保留换算条件')
        assert not memory.db.in_transaction
    finally:
        memory.close()
        other.close()


def test_distillation_changed_semantics_cannot_reuse_old_result(tmp_path):
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '厘米除以100', status='candidate')
        memory.put('b', 'experience', '毫米除以1000', status='candidate')
        runner = DreamRunner(memory)
        original = runner.distill_memories(['a', 'b'], summary='换算长度', conflicts=['单位待核对'], information_loss=0.1)
        repeated = runner.distill_memories(['b', 'a'], summary='换算长度', conflicts=['单位待核对'], information_loss=0.1)
        assert repeated['distillation_id'] == original['distillation_id']
        changed = runner.distill_memories(['a', 'b'], summary='换算长度', conflicts=['单位已解决'], information_loss=0.2)
        assert changed['distillation_id'] != original['distillation_id']
    finally:
        memory.close()


def test_hypothesis_source_check_and_insert_are_atomic(tmp_path):
    import sqlite3
    import pytest
    path = tmp_path / 'memory.db'
    memory = MemoryStore(path)
    other = MemoryStore(path)
    try:
        memory.put('a', 'experience', '厘米除100', status='candidate')
        memory.put('b', 'experience', '毫米除1000', status='candidate')
        runner = DreamRunner(memory)
        record = runner.distill_memories(['a', 'b'], summary='长度换算')
        other.db.execute('PRAGMA busy_timeout=0')
        original_get = memory.get
        def read_with_concurrent_revision(mid):
            value = original_get(mid)
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.db.execute("UPDATE memories SET content='并发修订' WHERE memory_id=?", (mid,))
            return value
        memory.get = read_with_concurrent_revision
        runner.propose_hypothesis(record['distillation_id'], '长度可换算', prior_reliability=0.5, argument='两条来源')
        assert not memory.db.in_transaction
    finally:
        other.close()
        memory.close()


def test_dream_action_requires_authorized_start_before_supported_result(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a','experience','厘米除100',status='candidate')
        memory.put('b','experience','毫米除1000',status='candidate')
        runner = DreamRunner(memory)
        record = runner.distill_memories(['a','b'],summary='长度换算')
        hypothesis = runner.propose_hypothesis(record['distillation_id'],'厘米换算',prior_reliability=.5,argument='来源')
        action = runner.schedule_verification(hypothesis,'复测125厘米',cost=1,risk=0,expected_information_gain=1,authorized=True)
        with pytest.raises(ValueError,match='verification_running_action_required'):
            runner.record_verification(hypothesis,outcome='supported',evidence_refs=['measurement'])
        assert runner.start_verification(action['action_id'])['status'] == 'running'
        result = runner.record_verification(hypothesis,outcome='supported',evidence_refs=['measurement'])
        assert result['status'] == 'supported'
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?',(action['action_id'],)).fetchone()[0] == 'completed'
    finally:
        memory.close()

def test_verification_rejects_ambiguous_running_actions(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '厘米除100', status='candidate')
        memory.put('b', 'experience', '毫米除1000', status='candidate')
        runner = DreamRunner(memory)
        record = runner.distill_memories(['a', 'b'], summary='长度换算')
        hypothesis = runner.propose_hypothesis(record['distillation_id'], '厘米换算', prior_reliability=.5, argument='来源')
        for description in ('测量125厘米', '测量230厘米'):
            action = runner.schedule_verification(hypothesis, description, cost=1, risk=0, expected_information_gain=1, authorized=True)
            runner.start_verification(action['action_id'])
        with pytest.raises(ValueError, match='verification_action_ambiguous'):
            runner.record_verification(hypothesis, outcome='supported', evidence_refs=['measurement'])
        assert memory.db.execute("SELECT COUNT(*) FROM verification_actions WHERE status='running'").fetchone()[0] == 2
        assert memory.db.execute('SELECT status FROM hypotheses WHERE hypothesis_id=?', (hypothesis,)).fetchone()[0] == 'candidate'
    finally:
        memory.close()

def test_changed_sources_block_start_and_result(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '厘米除100', status='candidate')
        memory.put('b', 'experience', '毫米除1000', status='candidate')
        runner = DreamRunner(memory)
        record = runner.distill_memories(['a', 'b'], summary='长度换算')
        hypothesis = runner.propose_hypothesis(record['distillation_id'], '厘米换算', prior_reliability=.5, argument='来源')
        action = runner.schedule_verification(hypothesis, '测量125厘米', cost=1, risk=0, expected_information_gain=1, authorized=True)
        memory.put('a', 'experience', '修订来源', status='candidate')
        with pytest.raises(ValueError, match='dream_source_snapshot_stale'):
            runner.start_verification(action['action_id'])
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (action['action_id'],)).fetchone()[0] == 'queued'
        memory.db.execute("UPDATE verification_actions SET status='running' WHERE action_id=?", (action['action_id'],))
        with pytest.raises(ValueError, match='dream_source_snapshot_stale'):
            runner.record_verification(hypothesis, outcome='supported', evidence_refs=['measurement'])
        assert memory.db.execute('SELECT status FROM hypotheses WHERE hypothesis_id=?', (hypothesis,)).fetchone()[0] == 'candidate'
    finally:
        memory.close()

def test_result_completes_only_explicit_running_action(tmp_path):
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '厘米除100', status='candidate')
        memory.put('b', 'experience', '毫米除1000', status='candidate')
        runner = DreamRunner(memory)
        record = runner.distill_memories(['a', 'b'], summary='长度换算')
        hypothesis = runner.propose_hypothesis(record['distillation_id'], '厘米换算', prior_reliability=.5, argument='来源')
        actions = [runner.schedule_verification(hypothesis, description, cost=1, risk=0, expected_information_gain=1, authorized=True)['action_id'] for description in ('测量125厘米', '测量230厘米')]
        for action in actions:
            runner.start_verification(action)
        runner.record_verification(hypothesis, action_id=actions[0], outcome='supported', evidence_refs=['measurement'])
        states = [memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (action,)).fetchone()[0] for action in actions]
        assert states == ['completed', 'running']
    finally:
        memory.close()


def test_run_candidate_rechecks_source_snapshot(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '125厘米为1.25米', status='candidate')
        runner = DreamRunner(memory)
        candidate = runner.run(min_fragments=1, min_interval=0)['candidate_id']
        hypothesis = runner.propose_hypothesis(candidate, '长度换算', prior_reliability=.5, argument='测量来源')
        action = runner.schedule_verification(hypothesis, '复测', cost=1, risk=0, expected_information_gain=1, authorized=True)['action_id']
        memory.put('a', 'experience', '来源已修订', status='candidate')
        with pytest.raises(ValueError, match='dream_source_snapshot_stale'):
            runner.propose_hypothesis(candidate, '旧来源', prior_reliability=.5, argument='旧来源')
        with pytest.raises(ValueError, match='dream_source_snapshot_stale'):
            runner.start_verification(action)
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (action,)).fetchone()[0] == 'queued'
    finally:
        memory.close()


def test_explicit_action_rejects_foreign_and_queued(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('a', 'experience', '长度换算', status='candidate')
        runner = DreamRunner(memory)
        candidate = runner.run(min_fragments=1, min_interval=0)['candidate_id']
        hypotheses = [runner.propose_hypothesis(candidate, '换算', prior_reliability=.5, argument='来源') for _ in range(2)]
        actions = [runner.schedule_verification(hypothesis, '复测', cost=1, risk=0, expected_information_gain=1, authorized=True)['action_id'] for hypothesis in hypotheses]
        runner.start_verification(actions[0])
        with pytest.raises(ValueError, match='verification_action_mismatch'):
            runner.record_verification(hypotheses[0], action_id=actions[1], outcome='supported', evidence_refs=['measurement'])
        with pytest.raises(ValueError, match='verification_running_action_required'):
            runner.record_verification(hypotheses[1], action_id=actions[1], outcome='unknown', evidence_refs=[])
        assert [row[0] for row in memory.db.execute('SELECT status FROM verification_actions ORDER BY created_at')] == ['running', 'queued']
    finally:
        memory.close()


def test_verification_memory_failure_rolls_back_result_and_action(tmp_path):
    import sqlite3
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('source', 'experience', '厘米除100', status='candidate')
        runner = DreamRunner(memory)
        candidate = runner.run(min_fragments=1, min_interval=0)['candidate_id']
        hypothesis = runner.propose_hypothesis(candidate, '125厘米为1.25米', prior_reliability=.5, argument='来源')
        action = runner.schedule_verification(hypothesis, '复测', cost=1, risk=0, expected_information_gain=1, authorized=True)['action_id']
        runner.start_verification(action)
        memory.db.execute("CREATE TRIGGER fail_verified_memory BEFORE INSERT ON memories WHEN NEW.memory_id='verified' BEGIN SELECT RAISE(ABORT, 'injected_verification_memory_failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='injected_verification_memory_failure'):
            runner.record_verification(hypothesis, action_id=action, outcome='supported', evidence_refs=['measurement'], new_memory_id='verified', new_content='换算验证结果')
        assert memory.db.execute('SELECT status FROM hypotheses WHERE hypothesis_id=?', (hypothesis,)).fetchone()[0] == 'candidate'
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (action,)).fetchone()[0] == 'running'
        assert memory.db.execute('SELECT status FROM verification_queue WHERE action_id=?', (action,)).fetchone()[0] == 'running'
        assert memory.db.execute("SELECT COUNT(*) FROM memories WHERE memory_id='verified'").fetchone()[0] == 0
        assert not memory.db.in_transaction
        memory.db.execute('DROP TRIGGER fail_verified_memory')
        assert runner.record_verification(hypothesis, action_id=action, outcome='supported', evidence_refs=['measurement'], new_memory_id='verified', new_content='换算验证结果')['status'] == 'supported'
    finally:
        memory.close()


def test_same_hypothesis_cannot_reuse_evidence_as_independent_action(tmp_path):
    import pytest
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.put('source', 'experience', '厘米除100', status='candidate')
        runner = DreamRunner(memory)
        candidate = runner.run(min_fragments=1, min_interval=0)['candidate_id']
        hypothesis = runner.propose_hypothesis(candidate, '125厘米为1.25米', prior_reliability=.5, argument='来源')
        actions = [runner.schedule_verification(hypothesis, description, cost=1, risk=0, expected_information_gain=1, authorized=True)['action_id'] for description in ('第一次复测', '独立复测')]
        for action in actions:
            runner.start_verification(action)
        runner.record_verification(hypothesis, action_id=actions[0], outcome='supported', evidence_refs=['measurement'])
        with pytest.raises(ValueError, match='verification_evidence_not_independent'):
            runner.record_verification(hypothesis, action_id=actions[1], outcome='supported', evidence_refs=['measurement'])
        assert memory.db.execute('SELECT status FROM verification_actions WHERE action_id=?', (actions[1],)).fetchone()[0] == 'running'
        assert runner.record_verification(hypothesis, action_id=actions[1], outcome='rejected', evidence_refs=['countermeasurement'])['status'] == 'rejected'
    finally:
        memory.close()


@pytest.mark.parametrize('change', [{'budget': True}, {'budget': 1.5}, {'min_fragments': True}, {'min_interval': float('nan')}, {'lock_timeout': float('inf')}, {'now': float('nan')}])
def test_dream_run_rejects_invalid_limits(tmp_path, change):
    from butian.runtime.memory import MemoryStore
    from butian.runtime.dream import DreamRunner
    memory = MemoryStore(tmp_path / 'limits.db')
    try:
        runner = DreamRunner(memory)
        with pytest.raises(ValueError, match='invalid_dream_run_limits'):
            runner.run(**change)
        assert memory.db.execute('SELECT COUNT(*) FROM dream_runs').fetchone()[0] == 0
    finally:
        memory.close()


def test_dream_initialization_preserves_callers_transaction(tmp_path):
    from butian.runtime.memory import MemoryStore
    from butian.runtime.dream import DreamRunner
    memory = MemoryStore(tmp_path / 'memory.db')
    try:
        memory.db.execute('CREATE TABLE caller_marker(value TEXT)')
        memory.db.execute('BEGIN')
        memory.db.execute("INSERT INTO caller_marker VALUES ('uncommitted')")
        DreamRunner(memory)
        assert memory.db.in_transaction
        memory.db.execute('ROLLBACK')
        assert memory.db.execute('SELECT COUNT(*) FROM caller_marker').fetchone()[0] == 0
    finally:
        memory.close()
