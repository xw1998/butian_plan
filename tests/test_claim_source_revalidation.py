import hashlib
import pytest
from butian.runtime.service import ButianService

@pytest.mark.parametrize('prepared', [False, True])
def test_changed_source_blocks_claim_practice_even_when_idempotent(tmp_path, prepared):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        content = 'original measured method'
        source = service.learning.add_source('web', 'https://example.org/method', 'method', hashlib.sha256(content.encode()).hexdigest(), owner_member_id='m')
        service.learning.verify_source_content(source, content, owner_member_id='m')
        claim = service.learning.add_claim(source, 'method requires local validation', owner_member_id='m')
        if prepared:
            service.prepare_claim_practice(claim, owner_member_id='m', description='validate method')
        service.learning.verify_source_content(source, 'changed source', owner_member_id='m')
        with pytest.raises(ValueError, match='knowledge_source_hash_mismatch'):
            service.prepare_claim_practice(claim, owner_member_id='m', description='validate method')
        with pytest.raises(ValueError, match='knowledge_source_hash_mismatch'):
            service.promote_claim_to_prior(claim, owner_member_id='m')
    finally:
        service.close()


def test_changed_source_blocks_previously_authorized_action_start(tmp_path):
    service=ButianService(tmp_path)
    try:
        service.add_member('m','Member')
        content='original measured method'
        source=service.learning.add_source('web','https://example.org/method','method',hashlib.sha256(content.encode()).hexdigest(),owner_member_id='m')
        service.learning.verify_source_content(source,content,owner_member_id='m')
        claim=service.learning.add_claim(source,'requires local validation',owner_member_id='m')
        action=service.prepare_claim_practice(claim,owner_member_id='m',description='validate method')['action_id']
        service.grant_hypothesis_authorization(action,actor_member_id='m',reason='authorized local practice',authorized=True,owner_member_id='m')
        service.learning.verify_source_content(source,'changed',owner_member_id='m')
        with pytest.raises(ValueError,match='knowledge_source_hash_mismatch'):
            service.start_hypothesis_action(action,owner_member_id='m')
    finally:service.close()
