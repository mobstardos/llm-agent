"""Разбиение файлов на чанки."""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path

from src.memory.config import ChunkingSettings
from src.memory.tokens import count_tokens

logger = logging.getLogger(__name__)


@dataclass
class RawChunk:
    text: str
    start_line: int
    end_line: int
    symbols: list[str]


class Chunker:
    def __init__(self, cfg: ChunkingSettings):
        self.cfg = cfg

    def chunk_file(self, path: Path, content: str) -> list[RawChunk]:
        ext = path.suffix.lower()
        if ext == ".py" and self.cfg.strategy == "ast":
            try:
                chunks = self._chunk_python_ast(content)
                if chunks:
                    return chunks
            except Exception as e:
                logger.debug("AST chunker failed for %s: %s", path, e)
        return self._chunk_by_lines(content)

    def _chunk_python_ast(self, source: str) -> list[RawChunk]:
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        chunks: list[RawChunk] = []

        for node in tree.body:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            text = "".join(lines[start:end])
            symbols: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(node.name)
            elif isinstance(node, ast.ClassDef):
                symbols.append(node.name)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(f"{node.name}.{child.name}")

            tokens = count_tokens(text)
            if tokens < self.cfg.min_tokens and chunks:
                chunks[-1].text += "\n" + text
                chunks[-1].end_line = end
                chunks[-1].symbols.extend(symbols)
                continue
            if tokens > self.cfg.max_tokens:
                chunks.extend(self._split_long(text, start + 1, symbols))
            else:
                chunks.append(RawChunk(
                    text=text, start_line=start + 1,
                    end_line=end, symbols=symbols,
                ))
        return chunks

    def _split_long(
        self, text: str, start_line: int, symbols: list[str],
    ) -> list[RawChunk]:
        lines = text.splitlines(keepends=True)
        chunks: list[RawChunk] = []
        current: list[str] = []
        current_start = start_line
        current_tokens = 0

        for i, line in enumerate(lines):
            line_tokens = count_tokens(line)
            if current_tokens + line_tokens > self.cfg.max_tokens and current:
                chunks.append(RawChunk(
                    text="".join(current),
                    start_line=current_start,
                    end_line=start_line + i - 1,
                    symbols=symbols,
                ))
                overlap_lines = current[-max(1, self.cfg.overlap_tokens // 10):]
                current = list(overlap_lines)
                current_tokens = sum(count_tokens(l) for l in current)
                current_start = start_line + i - len(overlap_lines)
            current.append(line)
            current_tokens += line_tokens

        if current:
            chunks.append(RawChunk(
                text="".join(current),
                start_line=current_start,
                end_line=start_line + len(lines) - 1,
                symbols=symbols,
            ))
        return chunks

    def _chunk_by_lines(self, content: str) -> list[RawChunk]:
        lines = content.splitlines(keepends=True)
        chunks: list[RawChunk] = []
        current: list[str] = []
        current_tokens = 0
        current_start = 1

        for i, line in enumerate(lines):
            t = count_tokens(line)
            if current_tokens + t > self.cfg.max_tokens and current:
                chunks.append(RawChunk(
                    text="".join(current),
                    start_line=current_start,
                    end_line=i,
                    symbols=[],
                ))
                overlap = max(1, self.cfg.overlap_tokens // 8)
                current = current[-overlap:]
                current_tokens = sum(count_tokens(l) for l in current)
                current_start = i - len(current) + 1
            current.append(line)
            current_tokens += t

        if current:
            chunks.append(RawChunk(
                text="".join(current),
                start_line=current_start,
                end_line=len(lines),
                symbols=[],
            ))
        return chunks
