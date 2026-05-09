"""Document processing for RAG."""

import re
from typing import Any, Dict, List, Optional

from skyn3t.config.settings import get_settings


class DocumentProcessor:
    """Process documents for RAG ingestion."""

    def __init__(self):
        settings = get_settings()
        self.chunk_size = settings.chunk_size
        self.chunk_overlap = settings.chunk_overlap

    def process_text(
        self, text: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Process raw text into chunks."""
        chunks = self._chunk_text(text)
        return [
            {
                "content": chunk,
                "metadata": {
                    **(metadata or {}),
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                },
            }
            for i, chunk in enumerate(chunks)
        ]

    def process_markdown(
        self, text: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Process markdown text, preserving headers as context."""
        chunks = []
        current_section = ""
        current_header = ""

        for line in text.split("\n"):
            if line.startswith("#"):
                if current_section:
                    section_chunks = self._chunk_text(current_section)
                    for i, chunk in enumerate(section_chunks):
                        chunks.append({
                            "content": f"Section: {current_header}\n\n{chunk}",
                            "metadata": {
                                **(metadata or {}),
                                "header": current_header,
                                "chunk_index": i,
                            },
                        })
                current_header = line.lstrip("# ").strip()
                current_section = ""
            else:
                current_section += line + "\n"

        # Process remaining section
        if current_section:
            section_chunks = self._chunk_text(current_section)
            for i, chunk in enumerate(section_chunks):
                chunks.append({
                    "content": f"Section: {current_header}\n\n{chunk}",
                    "metadata": {
                        **(metadata or {}),
                        "header": current_header,
                        "chunk_index": i,
                    },
                })

        return chunks

    def process_code(
        self, code: str, language: str, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Process code with syntax-aware chunking."""
        # Try to split by functions/classes
        if language in ("python", "py"):
            chunks = self._chunk_python_code(code)
        else:
            chunks = self._chunk_text(code)

        return [
            {
                "content": chunk,
                "metadata": {
                    **(metadata or {}),
                    "language": language,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                },
            }
            for i, chunk in enumerate(chunks)
        ]

    def _chunk_text(self, text: str) -> List[str]:
        """Split text into overlapping chunks."""
        words = text.split()
        chunks = []
        start = 0

        while start < len(words):
            end = min(start + self.chunk_size, len(words))
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            start += self.chunk_size - self.chunk_overlap

        return chunks

    def _chunk_python_code(self, code: str) -> List[str]:
        """Chunk Python code by functions and classes."""
        chunks = []
        current_chunk = []
        indent_level = 0

        for line in code.split("\n"):
            stripped = line.lstrip()
            if not stripped:
                current_chunk.append(line)
                continue

            current_indent = len(line) - len(stripped)

            # Detect function/class definitions
            if stripped.startswith(("def ", "class ", "async def ")):
                if current_chunk:
                    chunk_text = "\n".join(current_chunk)
                    if len(chunk_text.split()) > self.chunk_size:
                        # Split large chunks
                        subchunks = self._chunk_text(chunk_text)
                        chunks.extend(subchunks)
                    else:
                        chunks.append(chunk_text)
                current_chunk = [line]
                indent_level = current_indent
            else:
                current_chunk.append(line)

        # Add remaining chunk
        if current_chunk:
            chunk_text = "\n".join(current_chunk)
            if len(chunk_text.split()) > self.chunk_size:
                subchunks = self._chunk_text(chunk_text)
                chunks.extend(subchunks)
            else:
                chunks.append(chunk_text)

        return chunks

    def extract_entities(self, text: str) -> Dict[str, List[str]]:
        """Extract named entities from text."""
        # Simple regex-based extraction
        entities = {
            "urls": re.findall(r'https?://[^\s<>"{}|\\^`[\]]+', text),
            "emails": re.findall(r'[\w.-]+@[\w.-]+\.\w+', text),
            "code_blocks": re.findall(r'```[\w]*\n(.*?)```', text, re.DOTALL),
            "mentions": re.findall(r'@(\w+)', text),
        }
        return entities
