"""GitHub Explorer Agent - explores and analyzes GitHub repositories."""

import base64
import os
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class GitHubExplorerAgent(BaseAgent):
    """Agent for exploring GitHub repositories and projects."""

    def __init__(
        self,
        name: str = "github_explorer",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="github_explorer",
            provider="github",
            event_bus=event_bus,
            config=config,
        )
        self.github_client = None
        self.add_capability(
            AgentCapability(
                name="repo_analysis",
                description="Analyze GitHub repositories for structure, dependencies, and quality",
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_search",
                description="Search code across GitHub repositories",
            )
        )
        self.add_capability(
            AgentCapability(
                name="trending_repos",
                description="Find trending repositories by language or topic",
            )
        )
        self.add_capability(
            AgentCapability(
                name="readme_generation",
                description="Generate README files for repositories",
            )
        )
        self.add_capability(
            AgentCapability(
                name="issue_analysis",
                description="Analyze issues and PRs in repositories",
            )
        )

    async def initialize(self) -> None:
        """Initialize GitHub client."""
        try:
            from github import Github

            settings = get_settings()
            token = settings.github_token or os.getenv("GITHUB_TOKEN")
            if token:
                self.github_client = Github(token)
            else:
                self.github_client = Github()

            # Test connection
            self.github_client.get_user()
            self.metadata["authenticated"] = token is not None
            self.metadata["initialized"] = True

        except ImportError:
            raise ImportError(
                "PyGithub not installed. Run: pip install PyGithub"
            )

    async def health_check(self) -> bool:
        """Check GitHub API health."""
        if not self.github_client:
            return False
        try:
            self.github_client.get_rate_limit()
            return True
        except Exception:
            return False

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a GitHub exploration task."""
        task_type = task.input_data.get("task_type", "repo_analysis")

        handlers = {
            "repo_analysis": self._analyze_repo,
            "code_search": self._search_code,
            "trending_repos": self._find_trending,
            "readme_generation": self._generate_readme,
            "issue_analysis": self._analyze_issues,
            "user_repos": self._get_user_repos,
            "compare_repos": self._compare_repos,
            "file_explorer": self._explore_files,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result = await handler(task)
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

    async def _analyze_repo(self, task: TaskRequest) -> Dict[str, Any]:
        """Analyze a repository."""
        owner = task.input_data.get("owner")
        repo_name = task.input_data.get("repo")
        url = task.input_data.get("url")

        if url:
            parts = url.replace("https://github.com/", "").split("/")
            owner, repo_name = parts[0], parts[1]

        if not owner or not repo_name:
            raise ValueError("Owner and repo name required")

        repo = self.github_client.get_repo(f"{owner}/{repo_name}")

        # Get languages
        languages = repo.get_languages()

        # Get directory structure (first 3 levels)
        structure = await self._get_repo_structure(repo)

        # Get recent commits
        commits = [
            {
                "sha": c.sha[:7],
                "message": c.commit.message.split("\n")[0],
                "author": c.commit.author.name,
                "date": c.commit.author.date.isoformat(),
            }
            for c in repo.get_commits()[:10]
        ]

        # Get README
        readme_content = ""
        try:
            readme = repo.get_readme()
            readme_content = base64.b64decode(readme.content).decode("utf-8")
        except Exception:
            pass

        return {
            "name": repo.name,
            "full_name": repo.full_name,
            "description": repo.description,
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "open_issues": repo.open_issues_count,
            "language": repo.language,
            "languages": languages,
            "license": repo.license.name if repo.license else None,
            "created_at": repo.created_at.isoformat(),
            "updated_at": repo.updated_at.isoformat(),
            "structure": structure,
            "recent_commits": commits,
            "readme_preview": readme_content[:1000] if readme_content else "",
            "topics": repo.get_topics(),
            "url": repo.html_url,
        }

    async def _get_repo_structure(
        self, repo, path: str = "", depth: int = 0, max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """Get repository file structure."""
        if depth >= max_depth:
            return []

        structure = []
        try:
            contents = repo.get_contents(path)
            for content in contents[:50]:  # Limit to prevent timeouts
                item = {
                    "name": content.name,
                    "type": "file" if content.type == "file" else "dir",
                    "path": content.path,
                }
                if content.type == "dir":
                    item["children"] = await self._get_repo_structure(
                        repo, content.path, depth + 1, max_depth
                    )
                else:
                    item["size"] = content.size
                structure.append(item)
        except Exception:
            pass

        return structure

    async def _search_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Search code on GitHub."""
        query = task.input_data.get("query", "")
        language = task.input_data.get("language")
        sort = task.input_data.get("sort", "stars")

        if language:
            query += f" language:{language}"

        results = self.github_client.search_repositories(query, sort=sort)

        repos = []
        for repo in results[:20]:
            repos.append({
                "name": repo.name,
                "full_name": repo.full_name,
                "description": repo.description,
                "stars": repo.stargazers_count,
                "language": repo.language,
                "url": repo.html_url,
            })

        return {
            "query": query,
            "total_count": results.totalCount,
            "repositories": repos,
        }

    async def _find_trending(self, task: TaskRequest) -> Dict[str, Any]:
        """Find trending repositories."""
        language = task.input_data.get("language")
        since = task.input_data.get("since", "daily")

        # GitHub trending is not directly available via API, so we search for recently starred repos
        query = "stars:>100"
        if language:
            query += f" language:{language}"
        query += " sort:stars"

        results = self.github_client.search_repositories(query)

        repos = []
        for repo in results[:15]:
            repos.append({
                "name": repo.name,
                "full_name": repo.full_name,
                "description": repo.description,
                "stars": repo.stargazers_count,
                "language": repo.language,
                "url": repo.html_url,
            })

        return {
            "language": language,
            "since": since,
            "repositories": repos,
        }

    async def _generate_readme(self, task: TaskRequest) -> Dict[str, Any]:
        """Generate a README for a project."""
        project_name = task.input_data.get("project_name", "My Project")
        description = task.input_data.get("description", "")
        features = task.input_data.get("features", [])
        tech_stack = task.input_data.get("tech_stack", [])
        installation = task.input_data.get("installation", "")
        usage = task.input_data.get("usage", "")

        readme = f"""# {project_name}

{description}

## Features

"""
        for feature in features:
            readme += f"- {feature}\n"

        if tech_stack:
            readme += "\n## Tech Stack\n\n"
            for tech in tech_stack:
                readme += f"- {tech}\n"

        if installation:
            readme += f"\n## Installation\n\n```bash\n{installation}\n```\n"

        if usage:
            readme += f"\n## Usage\n\n```bash\n{usage}\n```\n"

        readme += """
## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License.
"""

        return {"readme": readme, "project_name": project_name}

    async def _analyze_issues(self, task: TaskRequest) -> Dict[str, Any]:
        """Analyze issues in a repository."""
        owner = task.input_data.get("owner")
        repo_name = task.input_data.get("repo")

        repo = self.github_client.get_repo(f"{owner}/{repo_name}")
        issues = repo.get_issues(state="open")

        issue_list = []
        for issue in issues[:30]:
            issue_list.append({
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "labels": [l.name for l in issue.labels],
                "created_at": issue.created_at.isoformat(),
                "user": issue.user.login,
                "comments": issue.comments,
            })

        # Group by labels
        label_counts = {}
        for issue in issue_list:
            for label in issue["labels"]:
                label_counts[label] = label_counts.get(label, 0) + 1

        return {
            "repo": repo.full_name,
            "open_issues": repo.open_issues_count,
            "issues": issue_list,
            "label_distribution": label_counts,
        }

    async def _get_user_repos(self, task: TaskRequest) -> Dict[str, Any]:
        """Get repositories for a user."""
        username = task.input_data.get("username")
        user = self.github_client.get_user(username)

        repos = []
        for repo in user.get_repos():
            repos.append({
                "name": repo.name,
                "description": repo.description,
                "stars": repo.stargazers_count,
                "language": repo.language,
                "fork": repo.fork,
                "url": repo.html_url,
            })

        return {
            "username": username,
            "public_repos": user.public_repos,
            "followers": user.followers,
            "repositories": repos,
        }

    async def _compare_repos(self, task: TaskRequest) -> Dict[str, Any]:
        """Compare two repositories."""
        repos_data = task.input_data.get("repos", [])
        if len(repos_data) < 2:
            raise ValueError("At least 2 repositories required for comparison")

        comparisons = []
        for repo_info in repos_data:
            owner = repo_info.get("owner")
            name = repo_info.get("repo")
            repo = self.github_client.get_repo(f"{owner}/{name}")

            comparisons.append({
                "name": repo.name,
                "stars": repo.stargazers_count,
                "forks": repo.forks_count,
                "open_issues": repo.open_issues_count,
                "language": repo.language,
                "created_at": repo.created_at.isoformat(),
                "updated_at": repo.updated_at.isoformat(),
                "size": repo.size,
            })

        return {"comparisons": comparisons}

    async def _explore_files(self, task: TaskRequest) -> Dict[str, Any]:
        """Explore specific files in a repository."""
        owner = task.input_data.get("owner")
        repo_name = task.input_data.get("repo")
        file_path = task.input_data.get("file_path")

        repo = self.github_client.get_repo(f"{owner}/{repo_name}")
        content = repo.get_contents(file_path)

        file_content = base64.b64decode(content.content).decode("utf-8")

        return {
            "repo": repo.full_name,
            "file_path": file_path,
            "size": content.size,
            "content": file_content,
            "sha": content.sha,
        }
