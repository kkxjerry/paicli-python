"""Phase 4：轻量代码索引与词法向量检索。

RAG 是 Retrieval-Augmented Generation（检索增强生成）。本期只实现检索部分：

    项目文件
       |
       v
    CodeChunker 切成代码块
       |
       v
    tokenize 转成词频向量
       |
       v
    VectorStore 保存在内存
       |
       v
    用户查询 -> 余弦相似度 -> 排序后的代码块

注意：这里的“向量”是词频 Counter，不是大模型 Embedding，不具备真正语义理解。
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .tools import ToolRegistry, ToolSpec


def tokenize(text: str) -> Counter[str]:
    """将文本转成“词 -> 出现次数”的稀疏向量。"""

    # 先拆分 camelCase，再把 snake_case 的下划线换成空格。
    # calculateInvoice_total 会变成 calculate Invoice total。
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")
    # 英文按单词提取，中文暂时按单个汉字提取，然后 Counter 统计词频。
    parts = re.findall(r"[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]", expanded.lower())
    return Counter(parts)


@dataclass(frozen=True)
class CodeChunk:
    """可被独立检索的代码块，同时保留文件、符号和行号信息。"""

    path: str
    symbol: str
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class SearchResult:
    """一个代码块及其与查询的相似度分数。"""

    chunk: CodeChunk
    score: float


class CodeChunker:
    """将文件切成 CodeChunk。Python 按顶层类/函数切分，其他文件整体作为一块。"""

    def chunk(self, path: Path, root: Path) -> list[CodeChunk]:
        # errors="replace" 会替换非法 UTF-8 字节，避免一个异常文件中断整个索引。
        text = path.read_text(encoding="utf-8", errors="replace")
        # 索引中保存相对路径，返回给 Agent 时不暴露冗长绝对路径。
        relative = str(path.relative_to(root))
        if path.suffix != ".py":
            # Java/JS/TS/Markdown 本期没有对应语法解析器，因此整个文件当作一块。
            return [CodeChunk(relative, path.name, 1, len(text.splitlines()), text)]

        try:
            # AST 能识别 Python 的函数和类边界，比根据空行切分更可靠。
            tree = ast.parse(text)
        except SyntaxError:
            # 正在编辑的文件可能暂时有语法错误，此时退化为整文件索引。
            return [CodeChunk(relative, path.name, 1, len(text.splitlines()), text)]

        lines = text.splitlines()
        chunks: list[CodeChunk] = []
        # 只遍历 tree.body，所以只将顶层类和函数分块；类中方法包含在整个类块内。
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            chunks.append(
                CodeChunk(
                    relative,
                    node.name,
                    node.lineno,
                    end,
                    # AST 行号从 1 开始，list 下标从 0 开始，所以起点要减 1。
                    "\n".join(lines[node.lineno - 1 : end]),
                )
            )
        # 合法 Python 文件也可能没有类/函数，例如只有常量，此时仍保留整个文件。
        return chunks or [
            CodeChunk(relative, path.name, 1, len(lines), text)
        ]


class VectorStore:
    """保存在内存中的稀疏词频向量，使用余弦相似度检索。"""

    def __init__(self) -> None:
        self._items: list[tuple[CodeChunk, Counter[str]]] = []

    def replace(self, chunks: Iterable[CodeChunk]) -> None:
        # rebuild 时整体替换旧索引，本期没有增量更新或磁盘持久化。
        self._items = [(chunk, tokenize(chunk.content)) for chunk in chunks]

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        # 查询和代码块必须经过同一个 tokenize 过程，向量维度才能对齐。
        query_vector = tokenize(query)
        scored = [
            SearchResult(chunk, self._cosine(query_vector, vector))
            for chunk, vector in self._items
        ]
        # 过滤 0 分项，再从高到低排序，最多返回 limit 个。
        return sorted(
            (result for result in scored if result.score > 0),
            key=lambda result: result.score,
            reverse=True,
        )[:limit]

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        # 点积：只有两边共同出现的词才会对分子产生贡献。
        numerator = sum(value * right.get(term, 0) for term, value in left.items())
        # 分母用两个向量的长度归一化，避免长代码块仅因词多就得高分。
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        if not left_norm or not right_norm:
            # 查询或代码块无可用词时，不能做除法，直接认为不相关。
            return 0.0
        return numerator / (left_norm * right_norm)


class CodeIndex:
    """协调“扫描文件 -> 分块 -> 建索引 -> 检索/注册工具”的总入口。"""

    SUPPORTED_SUFFIXES = {".py", ".java", ".js", ".ts", ".md"}
    IGNORED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__"}

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.chunker = CodeChunker()
        self.store = VectorStore()
        self.chunk_count = 0

    def rebuild(self) -> int:
        # 全量遍历项目目录。每次 rebuild 都重新分析所有受支持文件。
        chunks: list[CodeChunk] = []
        for path in self.root.rglob("*"):
            if (
                path.is_file()
                and path.suffix in self.SUPPORTED_SUFFIXES
                and not self.IGNORED_DIRECTORIES.intersection(path.parts)
            ):
                # 一个文件可能产生多个块，因此用 extend 而不是 append。
                chunks.extend(self.chunker.chunk(path, self.root))
        self.store.replace(chunks)
        self.chunk_count = len(chunks)
        return self.chunk_count

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        # CodeIndex 本身不重复评分逻辑，只将查询转发给 VectorStore。
        return self.store.search(query, limit)

    def register_tool(self, registry: ToolRegistry) -> None:
        # handler 是 ToolRegistry 真正执行 search_code 时调用的本地 Python 函数。
        def search_code(arguments: dict[str, object]) -> str:
            query = str(arguments["query"])
            limit = int(arguments.get("top_k", 5))
            results = self.search(query, limit)
            if not results:
                return "No matching code found."
            # 将结构化结果转为文本回灌给模型；单个块截断到 1200 字符以限制上下文。
            return "\n\n".join(
                f"{item.chunk.path}:{item.chunk.start_line} "
                f"[{item.chunk.symbol}] score={item.score:.3f}\n"
                f"{item.chunk.content[:1200]}"
                for item in results
            )

        # 注册后，Agent 会在工具 schema 中看到 search_code，并可传入 query/top_k。
        registry.register(
            ToolSpec(
                name="search_code",
                description="Search indexed project code by meaning or identifier.",
                parameters=registry.object_schema(
                    {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=["query"],
                ),
                handler=search_code,
            )
        )
