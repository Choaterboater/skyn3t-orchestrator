"""Capability-based permission system for SkyN3t agents.

Defines roles, policies, and a permission engine that evaluates
whether an agent is allowed to perform a given action.
"""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


class Permission(Enum):
    """Core permissions that can be granted to agents."""

    EXECUTE = auto()      # Run arbitrary commands
    READ = auto()         # Read files
    WRITE = auto()        # Write/modify files
    NETWORK = auto()      # Make network requests
    SHELL = auto()        # Execute shell commands (implies EXECUTE)
    FILESYSTEM = auto()   # Broad filesystem access (implies READ+WRITE)

    def __str__(self) -> str:
        return self.name

    @classmethod
    def from_string(cls, name: str) -> "Permission":
        try:
            return cls[name.upper()]
        except KeyError as exc:
            raise ValueError(f"Unknown permission: {name}") from exc


@dataclass
class Role:
    """A named collection of permissions."""

    name: str
    permissions: Set[Permission] = field(default_factory=set)
    description: str = ""
    parent_role: Optional[str] = None

    def has_permission(self, permission: Permission) -> bool:
        return permission in self.permissions

    def grant(self, permission: Permission) -> None:
        self.permissions.add(permission)

    def revoke(self, permission: Permission) -> None:
        self.permissions.discard(permission)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "permissions": sorted([p.name for p in self.permissions]),
            "description": self.description,
            "parent_role": self.parent_role,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Role":
        perms = {Permission.from_string(p) for p in data.get("permissions", [])}
        return cls(
            name=data["name"],
            permissions=perms,
            description=data.get("description", ""),
            parent_role=data.get("parent_role"),
        )


@dataclass
class Policy:
    """A policy defines what an agent can and cannot do.

    Policies are evaluated in order: explicit DENY overrides explicit ALLOW.
    """

    name: str
    role: str
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None
    allowed_dirs: List[str] = field(default_factory=list)
    denied_dirs: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    denied_commands: List[str] = field(default_factory=list)
    allowed_hosts: List[str] = field(default_factory=list)
    denied_hosts: List[str] = field(default_factory=list)
    max_cpu_time: Optional[float] = None
    max_memory_mb: Optional[int] = None
    max_file_size_mb: Optional[int] = None
    timeout_seconds: Optional[float] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "allowed_dirs": self.allowed_dirs,
            "denied_dirs": self.denied_dirs,
            "allowed_commands": self.allowed_commands,
            "denied_commands": self.denied_commands,
            "allowed_hosts": self.allowed_hosts,
            "denied_hosts": self.denied_hosts,
            "max_cpu_time": self.max_cpu_time,
            "max_memory_mb": self.max_memory_mb,
            "max_file_size_mb": self.max_file_size_mb,
            "timeout_seconds": self.timeout_seconds,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Policy":
        return cls(
            name=data["name"],
            role=data["role"],
            agent_name=data.get("agent_name"),
            agent_type=data.get("agent_type"),
            allowed_dirs=data.get("allowed_dirs", []),
            denied_dirs=data.get("denied_dirs", []),
            allowed_commands=data.get("allowed_commands", []),
            denied_commands=data.get("denied_commands", []),
            allowed_hosts=data.get("allowed_hosts", []),
            denied_hosts=data.get("denied_hosts", []),
            max_cpu_time=data.get("max_cpu_time"),
            max_memory_mb=data.get("max_memory_mb"),
            max_file_size_mb=data.get("max_file_size_mb"),
            timeout_seconds=data.get("timeout_seconds"),
            description=data.get("description", ""),
        )

    def matches(self, agent_name: str, agent_type: str) -> bool:
        """Check if this policy applies to a given agent."""
        if self.agent_name and self.agent_name != agent_name:
            return False
        if self.agent_type and self.agent_type != agent_type:
            return False
        return True


class PermissionEngine:
    """Evaluates permissions and policies before task execution."""

    DEFAULT_ROLES: Dict[str, Role] = {
        "admin": Role(
            name="admin",
            permissions={
                Permission.EXECUTE, Permission.READ, Permission.WRITE,
                Permission.NETWORK, Permission.SHELL, Permission.FILESYSTEM,
            },
            description="Full system access",
        ),
        "developer": Role(
            name="developer",
            permissions={
                Permission.EXECUTE, Permission.READ, Permission.WRITE,
                Permission.NETWORK, Permission.SHELL,
            },
            description="Can execute CLI commands, read/write code, and use network",
        ),
        "readonly": Role(
            name="readonly",
            permissions={Permission.READ},
            description="Read-only access to files",
        ),
        "sandboxed": Role(
            name="sandboxed",
            permissions={Permission.READ, Permission.EXECUTE},
            description="Restricted execution with no network or shell access",
        ),
    }

    def __init__(self):
        self.roles: Dict[str, Role] = deepcopy(self.DEFAULT_ROLES)
        self.policies: List[Policy] = []
        self._policy_cache: Dict[str, Optional[Policy]] = {}

    def register_role(self, role: Role) -> None:
        """Register or update a role."""
        self.roles[role.name] = role

    def get_role(self, name: str) -> Optional[Role]:
        """Get a role by name with merged permissions from parent chain."""
        role = self.roles.get(name)
        if not role:
            return None
        merged_perms: Set[Permission] = set(role.permissions)
        visited: Set[str] = {role.name}
        current = role
        while current.parent_role and current.parent_role in self.roles:
            if current.parent_role in visited:
                raise ValueError(
                    f"Circular role inheritance detected at '{current.parent_role}'"
                )
            visited.add(current.parent_role)
            parent = self.roles[current.parent_role]
            merged_perms |= parent.permissions
            current = parent
        if merged_perms == role.permissions:
            return role
        return Role(
            name=role.name,
            permissions=merged_perms,
            description=role.description,
            parent_role=role.parent_role,
        )

    def add_policy(self, policy: Policy) -> None:
        """Add a policy to the engine."""
        self.policies.append(policy)
        self._policy_cache.clear()

    def remove_policy(self, policy_name: str) -> bool:
        """Remove a policy by name."""
        for i, p in enumerate(self.policies):
            if p.name == policy_name:
                self.policies.pop(i)
                self._policy_cache.clear()
                return True
        return False

    def get_policy(self, agent_name: str, agent_type: str) -> Optional[Policy]:
        """Find the most specific policy for an agent."""
        cache_key = f"{agent_name}:{agent_type}"
        if cache_key in self._policy_cache:
            return self._policy_cache[cache_key]

        matches = [p for p in self.policies if p.matches(agent_name, agent_type)]
        if not matches:
            self._policy_cache[cache_key] = None
            return None

        # Most specific match wins (agent_name + agent_type is most specific)
        def specificity(p: Policy) -> int:
            score = 0
            if p.agent_name:
                score += 2
            if p.agent_type:
                score += 1
            return score

        matches.sort(key=specificity, reverse=True)
        best = matches[0]
        self._policy_cache[cache_key] = best
        return best

    def check_permission(
        self,
        agent_name: str,
        agent_type: str,
        permission: Permission,
    ) -> bool:
        """Check if an agent has a specific permission."""
        policy = self.get_policy(agent_name, agent_type)
        if policy is None:
            return False
        role = self.get_role(policy.role)
        if role is None:
            return False
        return role.has_permission(permission)

    def check_command(
        self,
        agent_name: str,
        agent_type: str,
        command: str,
    ) -> bool:
        """Check if an agent is allowed to run a specific command."""
        if not self.check_permission(agent_name, agent_type, Permission.EXECUTE):
            return False
        policy = self.get_policy(agent_name, agent_type)
        if policy is None:
            return False
        if policy.denied_commands:
            for pattern in policy.denied_commands:
                if command == pattern or _match_glob(command, pattern):
                    return False
        if policy.allowed_commands:
            for pattern in policy.allowed_commands:
                if command == pattern or _match_glob(command, pattern):
                    return True
            return False
        return True

    def check_directory(
        self,
        agent_name: str,
        agent_type: str,
        path: str,
    ) -> bool:
        """Check if an agent is allowed to access a directory."""
        policy = self.get_policy(agent_name, agent_type)
        if policy is None:
            return False
        if policy.denied_dirs:
            for pattern in policy.denied_dirs:
                if _match_glob(path, pattern):
                    return False
        if policy.allowed_dirs:
            for pattern in policy.allowed_dirs:
                if _match_glob(path, pattern):
                    return True
            return False
        return True

    def check_network(
        self,
        agent_name: str,
        agent_type: str,
        host: str,
    ) -> bool:
        """Check if an agent is allowed to connect to a host."""
        if not self.check_permission(agent_name, agent_type, Permission.NETWORK):
            return False
        policy = self.get_policy(agent_name, agent_type)
        if policy is None:
            return False
        if policy.denied_hosts:
            for pattern in policy.denied_hosts:
                if _match_glob(host, pattern):
                    return False
        if policy.allowed_hosts:
            for pattern in policy.allowed_hosts:
                if _match_glob(host, pattern):
                    return True
            return False
        return True

    def get_sandbox_limits(
        self,
        agent_name: str,
        agent_type: str,
    ) -> Dict[str, Any]:
        """Get resource limits for an agent from its policy."""
        policy = self.get_policy(agent_name, agent_type)
        limits: Dict[str, Any] = {
            "max_cpu_time": 60.0,
            "max_memory_mb": 512,
            "max_file_size_mb": 128,
            "timeout_seconds": 300.0,
        }
        if policy:
            if policy.max_cpu_time is not None:
                limits["max_cpu_time"] = policy.max_cpu_time
            if policy.max_memory_mb is not None:
                limits["max_memory_mb"] = policy.max_memory_mb
            if policy.max_file_size_mb is not None:
                limits["max_file_size_mb"] = policy.max_file_size_mb
            if policy.timeout_seconds is not None:
                limits["timeout_seconds"] = policy.timeout_seconds
        return limits

    def can_execute_task(
        self,
        agent_name: str,
        agent_type: str,
        task_title: str,
        task_input: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        """Full task execution check. Returns (allowed, reason)."""
        policy = self.get_policy(agent_name, agent_type)
        if policy is None:
            return False, f"No policy found for agent '{agent_name}'"

        role = self.get_role(policy.role)
        if role is None:
            return False, f"Role '{policy.role}' not found"

        # Check if task requires SHELL
        requires_shell = any(
            kw in task_title.lower() or kw in str(task_input).lower()
            for kw in ("shell", "bash", "sh ", "zsh", "exec ")
        )
        if requires_shell and not role.has_permission(Permission.SHELL):
            return False, "Agent lacks SHELL permission for this task"

        # Check if task requires NETWORK
        requires_network = any(
            kw in task_title.lower() or kw in str(task_input).lower()
            for kw in ("http", "curl", "wget", "fetch", "download", "api")
        )
        if requires_network and not role.has_permission(Permission.NETWORK):
            return False, "Agent lacks NETWORK permission for this task"

        if not role.has_permission(Permission.EXECUTE):
            return False, "Agent lacks EXECUTE permission"

        return True, None

    def load_policies(self, path: Path) -> None:
        """Load policies from a YAML or JSON file."""
        data = path.read_text()
        if path.suffix in (".yaml", ".yml"):
            if not _YAML_AVAILABLE:
                raise RuntimeError("PyYAML is required for YAML policy files")
            parsed = yaml.safe_load(data)
        else:
            parsed = json.loads(data)

        for role_data in parsed.get("roles", []):
            self.register_role(Role.from_dict(role_data))

        for policy_data in parsed.get("policies", []):
            self.add_policy(Policy.from_dict(policy_data))

    def save_policies(self, path: Path) -> None:
        """Save policies to a YAML or JSON file."""
        data = {
            "roles": [r.to_dict() for r in self.roles.values()],
            "policies": [p.to_dict() for p in self.policies],
        }
        if path.suffix in (".yaml", ".yml"):
            if not _YAML_AVAILABLE:
                raise RuntimeError("PyYAML is required for YAML policy files")
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        else:
            path.write_text(json.dumps(data, indent=2))


def _match_glob(value: str, pattern: str) -> bool:
    """Simple glob matching."""
    import fnmatch
    return fnmatch.fnmatch(value, pattern)
