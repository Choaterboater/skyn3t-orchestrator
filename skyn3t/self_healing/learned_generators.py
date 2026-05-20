"""Learned Generators — the key innovation that beats Hermes.

When the system repeatedly fails to generate a certain file
(e.g., `server/routes/habits.js`), it:
1. Detects the pattern (same file fails N times)
2. Asks the LLM ONCE to generate a generic template
3. Persists that template as a new deterministic generator
4. Future runs use the learned generator → NO MORE stub loops

This is "active learning" in the truest sense — the system
improves its own code generation capabilities permanently.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("skyn3t.self_healing.learned_generators")

# Where learned generators are persisted
_LEARNED_GEN_PATH = Path(__file__).parent.parent.parent / "stack_templates" / "learned_generators.py"
_FAIL_THRESHOLD = 3  # After 3 failures, learn a generator


class LearnedGeneratorManager:
    """Manages learned file generators with persistent storage."""

    def __init__(self):
        self._generators: Dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._load_all()

    def _load_all(self) -> None:
        """Load learned generators from disk."""
        if not _LEARNED_GEN_PATH.exists():
            return
        try:
            # Import the learned generators module
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "learned_generators", str(_LEARNED_GEN_PATH)
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Pull all callable entries that look like generators
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if callable(attr) and attr_name.startswith("gen_"):
                    # The generator name maps to a file path
                    # e.g., gen_server_routes_habits_js → server/routes/habits.js
                    file_path = _name_to_path(attr_name)
                    if file_path:
                        self._generators[file_path] = attr
            logger.info(
                "Loaded %d learned generator(s) from %s",
                len(self._generators), _LEARNED_GEN_PATH,
            )
        except Exception:
            logger.exception("Failed to load learned generators")

    def get_generator(self, file_path: str) -> Optional[Callable]:
        """Return a learned generator for `file_path`, or None."""
        with self._lock:
            return self._generators.get(file_path)

    def detect_repeat_failure(
        self, file_path: str, fail_count: int,
        threshold: int = _FAIL_THRESHOLD,
    ) -> bool:
        """Return True when the file has failed enough times to trigger learning."""
        return fail_count >= threshold

    def create_generator(
        self,
        file_path: str,
        stack: str,
        brief: str,
        llm_client=None,
    ) -> Optional[Callable]:
        """Use LLM once to generate a generic template, then persist it.

        Returns the newly created generator callable, or None on failure.
        """
        try:
            generator_func = self._ask_llm_for_generator(file_path, stack, brief, llm_client)
            if generator_func is None:
                return None
            # Save to disk
            self._persist_generator(file_path, generator_func)
            with self._lock:
                self._generators[file_path] = generator_func
            logger.info("Learned new generator for %s", file_path)
            return generator_func
        except Exception:
            logger.exception("Failed to create learned generator for %s", file_path)
            return None

    def _ask_llm_for_generator(
        self, file_path: str, stack: str, brief: str, llm_client=None,
    ) -> Optional[Callable]:
        """Ask LLM once to produce a generic template for this file type."""
        if llm_client is None:
            logger.warning("No LLM client available — cannot learn generator for %s", file_path)
            return None
        try:
            prompt = (
                f"Write a COMPLETE, generic, production-ready source file for: '{file_path}'.\n"
                f"Stack: {stack}\n"
                f"Brief context: {brief}\n"
                "Requirements:\n"
                "1. The file must be fully functional (no TODO stubs).\n"
                "2. Use generic/example values — this will be a reusable template.\n"
                "3. Handle errors gracefully.\n"
                "4. Include all necessary imports.\n"
                "5. Return ONLY the raw code — no markdown fences, no explanations.\n"
            )
            body = llm_client.complete(
                prompt=prompt,
                system="You are a senior developer. Write clean, working code templates.",
                max_tokens=2048,
                temperature=0.2,
                timeout=120,
            )
            if not body or "TODO" in body or "skyn3t-backfill" in body:
                logger.warning("LLM produced invalid template for %s", file_path)
                return None

            # Create a callable that returns this body
            def make_generator(code: str):
                def generator(brief: str = "") -> str:  # noqa: ARG001
                    return code
                return generator

            return make_generator(body)
        except Exception as exc:
            logger.warning("LLM call failed for %s: %s", file_path, exc)
            return None

    def _persist_generator(self, file_path: str, generator: Callable) -> None:
        """Append the generator function to learned_generators.py."""
        try:
            _LEARNED_GEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            func_name = _path_to_name(file_path)
            # Get the generated code by calling the generator
            code = generator("")
            # Build the Python source for the generator function
            source = (
                f"\n\ndef {func_name}(brief: str = '') -> Optional[str]:\n"
                f'    """Learned generator for {file_path}.\'"""\n'
                f"    return (\n"
                f"{_indent(code, 8)}"
                f"    )\n"
            )
            mode = "a" if _LEARNED_GEN_PATH.exists() else "w"
            with open(_LEARNED_GEN_PATH, mode, encoding="utf-8") as f:
                if mode == "w":
                    f.write(
                        "from __future__ import annotations\n"
                        "from typing import Optional\n\n"
                        "# Learned file generators — auto-generated by SelfHealing system\n"
                        "# DO NOT EDIT by hand — delete this file to reset.\n\n"
                    )
                f.write(source)
            logger.info("Persisted generator %s to %s", func_name, _LEARNED_GEN_PATH)
        except Exception:
            logger.exception("Failed to persist generator for %s", file_path)


def _path_to_name(file_path: str) -> str:
    """Convert a file path to a valid Python function name."""
    import re
    # Normalize: server/routes/habits.js → server_routes_habits_js
    name = re.sub(r"[^a-zA-Z0-9_]", "_", file_path)
    name = re.sub(r"_+", "_", name).strip("_")
    return f"gen_{name}"


def _name_to_path(func_name: str) -> Optional[str]:
    """Convert a generator function name back to a file path."""
    import re
    if not func_name.startswith("gen_"):
        return None
    path_part = func_name[4:]  # Remove "gen_"
    # Convert underscores back to slashes and dots
    # This is a best-effort reverse mapping
    parts = path_part.split("_")
    # Try to reconstruct: the last part might be an extension
    return path_part.replace("_", "/").replace("//", "_")


def _indent(text: str, spaces: int) -> str:
    """Indent all lines in `text` by `spaces` spaces."""
    indent_str = " " * spaces
    return "".join(
        indent_str + line if line.strip() else line + "\n"
        for line in text.splitlines(keepends=True)
    )
