"""GitHub webhook handler for SkyN3t."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Header, HTTPException, Request

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import Event, EventBus, EventType


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def get_webhook_secret() -> str:
    return os.getenv("GITHUB_WEBHOOK_SECRET", "")


_unsigned_warning_emitted = False


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    global _unsigned_warning_emitted
    if not secret:
        if not _unsigned_warning_emitted:
            logger.warning(
                "GITHUB_WEBHOOK_SECRET is not configured; webhook signature verification is DISABLED."
            )
            _unsigned_warning_emitted = True
        return True  # Skip verification if no secret configured
    if not signature:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubWebhookAgent(BaseAgent):
    """Agent that handles GitHub webhooks and auto-triggers other agents.

    Handles: push, pull_request, issue, release events.
    Auto-triggers code review agent on PR creation.
    Auto-triggers analysis agent on issues.
    Posts results as PR comments or issue comments.
    """

    def __init__(
        self,
        name: str = "github_webhook",
        event_bus: Optional[EventBus] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="integration",
            provider="github",
            event_bus=event_bus,
            config=config or {},
        )
        self.add_capability(
            AgentCapability(
                name="github_webhook_handling",
                description="Handle GitHub webhook events and trigger agents",
                parameters={"event_type": "str", "payload": "dict"},
                required_config=["GITHUB_WEBHOOK_SECRET", "GITHUB_TOKEN"],
            )
        )
        self.add_capability(
            AgentCapability(
                name="pr_review",
                description="Perform automated PR code review",
                parameters={"repo": "str", "pr_number": "int", "diff_url": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="issue_analysis",
                description="Analyze and triage GitHub issues",
                parameters={"repo": "str", "issue_number": "int", "body": "str"},
            )
        )

        self.webhook_secret = self.config.get("webhook_secret") or os.getenv("GITHUB_WEBHOOK_SECRET", "")
        self.github_token = self.config.get("github_token") or os.getenv("GITHUB_TOKEN")
        self._orchestrator: Any = None
        self._github_client: Any = None
        self._bg_tasks: Set[asyncio.Task] = set()

    async def initialize(self) -> None:
        """Initialize the GitHub client."""
        if self.github_token:
            try:
                from github import Github
                self._github_client = Github(self.github_token)
                user = self._github_client.get_user()
                self.metadata["github_user"] = user.login
            except Exception as e:
                print(f"GitHub client initialization warning: {e}")

    async def health_check(self) -> bool:
        """Check webhook handler health."""
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a GitHub webhook task."""
        task_type = task.input_data.get("task_type", "process_webhook")

        if task_type == "process_webhook":
            return await self._process_webhook_task(task)
        elif task_type == "pr_review":
            return await self._pr_review_task(task)
        elif task_type == "issue_analysis":
            return await self._issue_analysis_task(task)
        elif task_type == "post_comment":
            return await self._post_comment_task(task)
        else:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown GitHub webhook task type: {task_type}",
            )

    def set_orchestrator(self, orchestrator: Any) -> None:
        """Attach the main orchestrator for agent triggering."""
        self._orchestrator = orchestrator

    # ------------------------------------------------------------------
    # Webhook processing
    # ------------------------------------------------------------------

    async def _process_webhook_task(self, task: TaskRequest) -> TaskResult:
        event_type = task.input_data.get("event_type", "")
        payload = task.input_data.get("payload", {})

        handler_map = {
            "push": self._handle_push,
            "pull_request": self._handle_pull_request,
            "issues": self._handle_issue,
            "release": self._handle_release,
            "pull_request_review": self._handle_pr_review_event,
        }

        handler = handler_map.get(event_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output={"handled": False, "reason": f"Unhandled event type: {event_type}"},
            )

        try:
            result = await handler(payload)
            return TaskResult(
                task_id=task.task_id,
                success=True,
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    async def _handle_push(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle push events."""
        repo = payload.get("repository", {}).get("full_name")
        ref = payload.get("ref", "")
        commits = payload.get("commits", [])

        self.event_bus.publish(
            Event(
                event_type=EventType.GITHUB_EVENT,
                source=self.name,
                payload={
                    "event": "push",
                    "repo": repo,
                    "ref": ref,
                    "commit_count": len(commits),
                    "commits": [{"id": c.get("id"), "message": c.get("message")} for c in commits],
                },
            )
        )
        return {"handled": True, "event": "push", "repo": repo, "commits": len(commits)}

    async def _handle_pull_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle pull_request events. Auto-trigger review on creation."""
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name")
        pr_number = pr.get("number")

        self.event_bus.publish(
            Event(
                event_type=EventType.GITHUB_EVENT,
                source=self.name,
                payload={
                    "event": "pull_request",
                    "action": action,
                    "repo": repo,
                    "pr_number": pr_number,
                    "title": pr.get("title"),
                    "user": pr.get("user", {}).get("login"),
                },
            )
        )

        if action in ("opened", "synchronize", "reopened"):
            # Auto-trigger code review
            bg_task = asyncio.create_task(self._trigger_pr_review(repo, pr_number, pr))
            self._bg_tasks.add(bg_task)
            bg_task.add_done_callback(self._bg_tasks.discard)

        return {"handled": True, "event": "pull_request", "action": action, "repo": repo, "pr_number": pr_number}

    async def _handle_issue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle issue events. Auto-trigger analysis on creation."""
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {}).get("full_name")
        issue_number = issue.get("number")

        self.event_bus.publish(
            Event(
                event_type=EventType.GITHUB_EVENT,
                source=self.name,
                payload={
                    "event": "issue",
                    "action": action,
                    "repo": repo,
                    "issue_number": issue_number,
                    "title": issue.get("title"),
                    "user": issue.get("user", {}).get("login"),
                },
            )
        )

        if action in ("opened", "reopened"):
            bg_task = asyncio.create_task(self._trigger_issue_analysis(repo, issue_number, issue))
            self._bg_tasks.add(bg_task)
            bg_task.add_done_callback(self._bg_tasks.discard)

        return {"handled": True, "event": "issue", "action": action, "repo": repo, "issue_number": issue_number}

    async def _handle_release(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle release events."""
        action = payload.get("action", "")
        release = payload.get("release", {})
        repo = payload.get("repository", {}).get("full_name")

        self.event_bus.publish(
            Event(
                event_type=EventType.GITHUB_EVENT,
                source=self.name,
                payload={
                    "event": "release",
                    "action": action,
                    "repo": repo,
                    "tag": release.get("tag_name"),
                    "name": release.get("name"),
                },
            )
        )
        return {"handled": True, "event": "release", "action": action, "repo": repo}

    async def _handle_pr_review_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle pull_request_review events."""
        action = payload.get("action", "")
        review = payload.get("review", {})
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name")

        return {
            "handled": True,
            "event": "pull_request_review",
            "action": action,
            "repo": repo,
            "pr_number": pr.get("number"),
            "state": review.get("state"),
        }

    # ------------------------------------------------------------------
    # Auto-triggered agent tasks
    # ------------------------------------------------------------------

    async def _trigger_pr_review(self, repo: str, pr_number: int, pr: Dict[str, Any]):
        """Trigger a PR review via the code agent."""
        if not self._orchestrator:
            print("No orchestrator connected; skipping auto PR review")
            return

        task = TaskRequest(
            title=f"PR Review: {repo}#{pr_number}",
            description=f"Review pull request #{pr_number} in {repo}",
            input_data={
                "task_type": "pr_review",
                "repo": repo,
                "pr_number": pr_number,
                "title": pr.get("title"),
                "body": pr.get("body"),
                "diff_url": pr.get("diff_url"),
                "html_url": pr.get("html_url"),
                "head_sha": pr.get("head", {}).get("sha"),
            },
            priority=2,
        )

        try:
            task_id = await self._orchestrator.submit_task(task, capability="pr_review")
            result = await self._orchestrator.wait_for_task(task_id, timeout=300.0)
            if result and result.success:
                await self._post_pr_comment(repo, pr_number, result.output)
        except Exception as e:
            print(f"Auto PR review failed: {e}")

    async def _trigger_issue_analysis(self, repo: str, issue_number: int, issue: Dict[str, Any]):
        """Trigger issue analysis via the analysis agent."""
        if not self._orchestrator:
            print("No orchestrator connected; skipping auto issue analysis")
            return

        task = TaskRequest(
            title=f"Issue Analysis: {repo}#{issue_number}",
            description=f"Analyze issue #{issue_number} in {repo}",
            input_data={
                "task_type": "issue_analysis",
                "repo": repo,
                "issue_number": issue_number,
                "title": issue.get("title"),
                "body": issue.get("body"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
            },
            priority=2,
        )

        try:
            task_id = await self._orchestrator.submit_task(task, capability="issue_analysis")
            result = await self._orchestrator.wait_for_task(task_id, timeout=300.0)
            if result and result.success:
                await self._post_issue_comment(repo, issue_number, result.output)
        except Exception as e:
            print(f"Auto issue analysis failed: {e}")

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    async def _post_pr_comment(self, repo: str, pr_number: int, output: Dict[str, Any]) -> None:
        """Post a comment on a PR."""
        if not self._github_client:
            return
        try:
            body = self._format_review_comment(output)

            def _post():
                gh_repo = self._github_client.get_repo(repo)
                pr_obj = gh_repo.get_pull(pr_number)
                pr_obj.create_issue_comment(body)

            await asyncio.to_thread(_post)
        except Exception as e:
            print(f"Failed to post PR comment: {e}")

    async def _post_issue_comment(self, repo: str, issue_number: int, output: Dict[str, Any]) -> None:
        """Post a comment on an issue."""
        if not self._github_client:
            return
        try:
            body = self._format_issue_comment(output)

            def _post():
                gh_repo = self._github_client.get_repo(repo)
                issue_obj = gh_repo.get_issue(issue_number)
                issue_obj.create_comment(body)

            await asyncio.to_thread(_post)
        except Exception as e:
            print(f"Failed to post issue comment: {e}")

    def _format_review_comment(self, output: Dict[str, Any]) -> str:
        """Format PR review output into a markdown comment."""
        lines = ["## 🤖 SkyN3t Automated PR Review", ""]
        summary = output.get("summary", output.get("response", str(output)))
        lines.append(summary)
        lines.append("")
        lines.append("---")
        lines.append("*This review was generated automatically by SkyN3t.*")
        return "\n".join(lines)

    def _format_issue_comment(self, output: Dict[str, Any]) -> str:
        """Format issue analysis output into a markdown comment."""
        lines = ["## 🤖 SkyN3t Issue Analysis", ""]
        summary = output.get("summary", output.get("response", str(output)))
        lines.append(summary)
        lines.append("")
        lines.append("---")
        lines.append("*This analysis was generated automatically by SkyN3t.*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Direct task handlers
    # ------------------------------------------------------------------

    async def _pr_review_task(self, task: TaskRequest) -> TaskResult:
        """Direct PR review task."""
        repo = task.input_data.get("repo")
        pr_number = task.input_data.get("pr_number")
        if not repo or not pr_number:
            return TaskResult(task_id=task.task_id, success=False, error="repo and pr_number required")

        if not self._github_client:
            return TaskResult(task_id=task.task_id, success=False, error="GitHub client not initialized")

        try:
            gh_repo = self._github_client.get_repo(repo)
            pr_obj = gh_repo.get_pull(pr_number)
            files = list(pr_obj.get_files())
            file_summaries = []
            for f in files[:20]:
                file_summaries.append({
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "patch": f.patch[:2000] if f.patch else None,
                })

            output = {
                "repo": repo,
                "pr_number": pr_number,
                "title": pr_obj.title,
                "files": file_summaries,
                "total_files": len(files),
                "summary": f"Analyzed {len(files)} files in PR #{pr_number}.",
            }
            return TaskResult(task_id=task.task_id, success=True, output=output)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    async def _issue_analysis_task(self, task: TaskRequest) -> TaskResult:
        """Direct issue analysis task."""
        repo = task.input_data.get("repo")
        issue_number = task.input_data.get("issue_number")
        if not repo or not issue_number:
            return TaskResult(task_id=task.task_id, success=False, error="repo and issue_number required")

        if not self._github_client:
            return TaskResult(task_id=task.task_id, success=False, error="GitHub client not initialized")

        try:
            gh_repo = self._github_client.get_repo(repo)
            issue_obj = gh_repo.get_issue(issue_number)
            output = {
                "repo": repo,
                "issue_number": issue_number,
                "title": issue_obj.title,
                "body": issue_obj.body,
                "labels": [l.name for l in issue_obj.labels],
                "state": issue_obj.state,
                "summary": f"Issue #{issue_number}: {issue_obj.title}",
            }
            return TaskResult(task_id=task.task_id, success=True, output=output)
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))

    async def _post_comment_task(self, task: TaskRequest) -> TaskResult:
        repo = task.input_data.get("repo")
        number = task.input_data.get("number")
        body = task.input_data.get("body", "")
        is_pr = task.input_data.get("is_pr", False)

        if not self._github_client:
            return TaskResult(task_id=task.task_id, success=False, error="GitHub client not initialized")

        try:
            gh_repo = self._github_client.get_repo(repo)
            if is_pr:
                pr_obj = gh_repo.get_pull(number)
                pr_obj.create_issue_comment(body)
            else:
                issue_obj = gh_repo.get_issue(number)
                issue_obj.create_comment(body)
            return TaskResult(task_id=task.task_id, success=True, output={"posted": True})
        except Exception as e:
            return TaskResult(task_id=task.task_id, success=False, error=str(e))


# ------------------------------------------------------------------
# FastAPI endpoint
# ------------------------------------------------------------------

@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    """Receive GitHub webhook events."""
    payload_bytes = await request.body()
    secret = get_webhook_secret()

    if secret and not verify_signature(payload_bytes, x_hub_signature_256, secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Route to the agent if available via app state
    agent = getattr(request.app.state, "github_webhook_agent", None)
    if agent:
        task = TaskRequest(
            title=f"GitHub {x_github_event}",
            description=f"Process {x_github_event} webhook",
            input_data={
                "task_type": "process_webhook",
                "event_type": x_github_event,
                "payload": payload,
            },
            priority=2,
        )
        result = await agent.execute(task)
        return {"received": True, "event": x_github_event, "handled": result.success}

    return {"received": True, "event": x_github_event, "handled": False}
