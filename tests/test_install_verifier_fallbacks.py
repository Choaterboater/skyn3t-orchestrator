from skyn3t.agents.boot_verifier import BootVerifierAgent
from skyn3t.agents.integration_verifier import IntegrationContractVerifierAgent


def test_boot_verifier_relaxed_install_cmd_strips_problematic_flags() -> None:
    cmd = ["npm", "install", "--silent", "--no-audit", "--prefer-offline"]
    relaxed = BootVerifierAgent._relaxed_install_cmd(cmd)
    assert relaxed == ["npm", "install", "--no-audit"]


def test_integration_verifier_relaxed_install_cmd_strips_problematic_flags() -> None:
    cmd = ["npm", "install", "--silent", "--prefer-offline", "--no-fund"]
    relaxed = IntegrationContractVerifierAgent._relaxed_install_cmd(cmd)
    assert relaxed == ["npm", "install", "--no-fund"]


def test_install_failure_diagnosis_handles_timeout_and_empty_logs() -> None:
    boot = BootVerifierAgent()
    timeout_hint = boot._diagnose_install_failure("npm exceeded 240s timeout")
    assert "timed out" in timeout_hint.lower()

    empty_hint = boot._diagnose_install_failure("")
    assert "no diagnostics" in empty_hint.lower()
