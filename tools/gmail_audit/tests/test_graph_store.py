from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from graph_store import InMemoryGraphStore, build_graph_edge, build_graph_node


class GraphStoreTests(unittest.TestCase):
    def test_in_memory_graph_store_upserts_nodes_edges_and_returns_case_hints(self) -> None:
        store = InMemoryGraphStore()
        store.bootstrap()
        observed_at = "2026-04-12T10:00:00+02:00"
        case_node = build_graph_node(
            node_type="Case",
            natural_key="case_drive_1",
            title="CASE-DRIVE-1",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/case",
            confidence=0.95,
            payload={"case_key": "CASE-DRIVE-1"},
            observed_at=observed_at,
        )
        model_node = build_graph_node(
            node_type="Model",
            natural_key="wh-adc0309k3e5",
            title="WH-ADC0309K3E5",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/model",
            confidence=0.88,
            payload={},
            observed_at=observed_at,
        )
        edge = build_graph_edge(
            src_node_id=case_node["node_id"],
            dst_node_id=model_node["node_id"],
            relation_type="case_has_document",
            source="gdrive",
            source_ref="https://drive.google.com/file/d/doc",
            confidence=0.84,
            metadata={"document_kind": "contract"},
            observed_at=observed_at,
        )

        store.upsert_many(nodes=[case_node, model_node], edges=[edge])
        hints = store.fetch_case_hints("case_drive_1", limit=10)

        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["relation_type"], "case_has_document")
        self.assertEqual(hints[0]["related_title"], "WH-ADC0309K3E5")
        self.assertEqual(hints[0]["source_ref"], "https://drive.google.com/file/d/doc")


if __name__ == "__main__":
    unittest.main()
