from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paicli.hybrid_rag import HashEmbeddingClient, HybridCodeIndex
from paicli.tools import ToolRegistry


class SemanticEmbedding:
    model = "semantic-test"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lower = text.lower()
            if "authenticate_user" in lower or "login flow" in lower:
                vectors.append([1.0, 0.0, 0.0])
            elif "calculate_invoice" in lower or "billing" in lower:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class HybridRagTest(unittest.TestCase):
    def test_dense_semantic_retrieval_returns_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth.py").write_text(
                "def authenticate_user(username: str, password: str) -> bool:\n"
                "    return username == 'admin' and password == 'secret'\n",
                encoding="utf-8",
            )
            (root / "billing.py").write_text(
                "def calculate_invoice(amount: int) -> int:\n"
                "    return amount * 2\n",
                encoding="utf-8",
            )
            embedding = SemanticEmbedding()
            index = HybridCodeIndex(root, embedding_client=embedding)
            try:
                self.assertEqual(2, index.rebuild())

                results = index.search("login flow", 2)

                self.assertEqual("auth.py", results[0].chunk.path)
                self.assertGreater(results[0].dense_score, 0.9)
                self.assertEqual("authenticate_user", results[0].chunk.symbol)
                self.assertEqual(1, results[0].chunk.start_line)
            finally:
                index.close()

    def test_rebuild_reuses_persisted_embeddings_and_tool_has_citations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(
                "def load_profile(user_id: str) -> dict:\n"
                "    return {'id': user_id}\n",
                encoding="utf-8",
            )
            embedding = SemanticEmbedding()
            database = root / ".paicli" / "index.db"
            index = HybridCodeIndex(root, database, embedding_client=embedding)
            try:
                index.rebuild()
                calls_after_first = len(embedding.calls)
                index.rebuild()
                self.assertEqual(calls_after_first, len(embedding.calls))

                tools = ToolRegistry(root)
                index.register_tool(tools)
                output = tools.execute(
                    "search_code",
                    '{"query":"load_profile","top_k":3}',
                )

                self.assertIn("SOURCE service.py:L1", output)
                self.assertIn("load_profile", output)
            finally:
                index.close()

    def test_lexical_only_index_persists_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.py").write_text(
                "def process_payment() -> str:\n    return 'ok'\n",
                encoding="utf-8",
            )
            database = root / ".paicli" / "index.db"
            first = HybridCodeIndex(root, database)
            first.rebuild()
            first.close()

            second = HybridCodeIndex(root, database)
            try:
                results = second.search("process payment", 3)
                self.assertEqual("worker.py", results[0].chunk.path)
                self.assertGreater(results[0].lexical_score, 0)
                self.assertEqual(0, second.stats()["embedded"])
            finally:
                second.close()

    def test_hash_embedding_is_deterministic_and_normalized(self) -> None:
        client = HashEmbeddingClient(32)

        first, second = client.embed(["same content", "same content"])

        self.assertEqual(first, second)
        self.assertAlmostEqual(1.0, sum(value * value for value in first), places=6)


if __name__ == "__main__":
    unittest.main()
