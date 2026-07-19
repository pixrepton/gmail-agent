"""Chunk sizing for mailbox/drive document embedding."""

from __future__ import annotations

import unittest

from mailbox_memory_runtime import CHUNK_TARGET_CHARS, build_document_chunks


class BuildDocumentChunksTests(unittest.TestCase):
    def test_splits_single_long_line_for_embedding_bounds(self) -> None:
        text = "x" * (CHUNK_TARGET_CHARS * 3 + 50)
        rows = build_document_chunks(
            case_id="c1",
            document_id="d1",
            file_name="sheet.xlsm",
            text=text,
            created_at="2026-05-23T12:00:00+00:00",
        )
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertLessEqual(len(str(row.get("chunk_text") or "")), CHUNK_TARGET_CHARS)


if __name__ == "__main__":
    unittest.main()
