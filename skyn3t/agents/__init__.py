"""Agent implementations for SkyN3t."""

from skyn3t.adapters import ClaudeCLIAgent, CLIAgent, CopilotCLIAgent, KimiCLIAgent
from skyn3t.agents.architect import ArchitectAgent
from skyn3t.agents.brainstorm import BrainstormAgent
from skyn3t.agents.build_verifier import BuildVerifierAgent
from skyn3t.agents.business_analyst import BusinessAnalystAgent
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.consistency_reviewer import ConsistencyReviewerAgent
from skyn3t.agents.contract_verifier import ContractVerifierAgent
from skyn3t.agents.designer import DesignerAgent
from skyn3t.agents.docs_ingestor import DocsIngestorAgent
from skyn3t.agents.explorer import ExplorerAgent
from skyn3t.agents.file_ops_agent import FileOpsAgent
from skyn3t.agents.github_explorer import GitHubExplorerAgent
from skyn3t.agents.github_ingestor import GitHubIngestorAgent
from skyn3t.agents.integration_verifier import IntegrationContractVerifierAgent
from skyn3t.agents.marketer import MarketerAgent
from skyn3t.agents.packaging_agent import PackagingAgent
from skyn3t.agents.project_memory import ProjectMemoryAgent
from skyn3t.agents.research_agent import ResearchAgent
from skyn3t.agents.reviewer import ReviewerAgent
from skyn3t.agents.scheduler_agent import SchedulerAgent
from skyn3t.agents.verifier import VerifierAgent
from skyn3t.agents.writer import WriterAgent

__all__ = [
    "ArchitectAgent",
    "BrainstormAgent",
    "BuildVerifierAgent",
    "BusinessAnalystAgent",
    "CLIAgent",
    "ClaudeCLIAgent",
    "CodeAgent",
    "CodeImproverAgent",
    "ConsistencyReviewerAgent",
    "ContractVerifierAgent",
    "CopilotCLIAgent",
    "IntegrationContractVerifierAgent",
    "DesignerAgent",
    "DocsIngestorAgent",
    "ExplorerAgent",
    "FileOpsAgent",
    "GitHubExplorerAgent",
    "GitHubIngestorAgent",
    "KimiCLIAgent",
    "MarketerAgent",
    "ProjectMemoryAgent",
    "ResearchAgent",
    "ReviewerAgent",
    "SchedulerAgent",
    "VerifierAgent",
    "WriterAgent",
]
