import pytest

from butian.runtime.execution_contract import (
    CapabilityEvidence, ExecutionCapabilities, ExecutionObservation, REQUIRED_CONTROL,
)


def test_implementation_claim_cannot_authorize_execution():
    capabilities = ExecutionCapabilities("local", "generation-1", {
        name: CapabilityEvidence("supported", "implementation", 1, "generation-1")
        for name in REQUIRED_CONTROL
    })
    with pytest.raises(ValueError, match="execution_capabilities_missing"):
        capabilities.require()


def test_previous_instance_evidence_cannot_authorize_restart():
    capabilities = ExecutionCapabilities("local", "generation-2", {
        name: CapabilityEvidence("supported", "explicit_self_test", 1, "generation-1")
        for name in REQUIRED_CONTROL
    })
    assert capabilities.missing() == list(REQUIRED_CONTROL)


def test_verified_control_is_required_as_a_complete_set():
    features = {name: CapabilityEvidence("supported", "explicit_self_test", 1, "generation-1")
                for name in REQUIRED_CONTROL}
    ExecutionCapabilities("local", "generation-1", features).require()
    del features["process_tree_cleanup"]
    assert ExecutionCapabilities("local", "generation-1", features).missing() == ["process_tree_cleanup"]


@pytest.mark.parametrize("state", ["success", "failed", "cancelled"])
def test_terminal_claim_requires_tree_exit_confirmation(state):
    with pytest.raises(ValueError, match="execution_exit_unconfirmed"):
        ExecutionObservation("exec", "local", "generation-1", 2, state, exit_code=0)


def test_cancel_request_and_unknown_do_not_claim_exit():
    for state in ("cancelling", "unknown"):
        observation = ExecutionObservation("exec", "local", "generation-1", 2, state)
        assert not observation.tree_exit_confirmed


def test_zero_exit_required_for_success():
    with pytest.raises(ValueError, match="execution_success_exit_invalid"):
        ExecutionObservation("exec", "local", "generation-1", 2, "success",
                             exit_code=1, tree_exit_confirmed=True)
