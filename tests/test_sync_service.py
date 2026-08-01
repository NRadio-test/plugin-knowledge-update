import json
import unittest

from sync_service import (
    MANAGED_DOCUMENT_PREFIX,
    GitHubKnowledgeSnapshot,
    KnowledgeSourceError,
    is_managed_document,
    parse_knowledge_jsonl,
    render_entry,
)


class KnowledgeParserTests(unittest.TestCase):
    def test_parses_and_renders_one_entry(self) -> None:
        payload = json.dumps(
            {
                "id": "contact-001",
                "title": "临时联系方式",
                "text": "企业微信异常期间请联系指定 QQ。",
                "uploaded_by": "FallaxAura",
                "source_type": "internal_notice",
                "confidence": "high",
                "tags": ["联系", "通知"],
            },
            ensure_ascii=False,
        )

        entries = parse_knowledge_jsonl(payload)

        self.assertEqual(len(entries), 1)
        rendered = render_entry(entries[0])
        self.assertIn("知识内容：企业微信异常期间请联系指定 QQ。", rendered)
        self.assertIn("上传者：FallaxAura", rendered)
        self.assertIn("标签：联系、通知", rendered)

    def test_rejects_duplicate_ids(self) -> None:
        line = json.dumps({"id": "same", "title": "A", "text": "B", "tags": []})
        with self.assertRaisesRegex(KnowledgeSourceError, "重复 id"):
            parse_knowledge_jsonl(f"{line}\n{line}\n")

    def test_rejects_empty_source(self) -> None:
        with self.assertRaisesRegex(KnowledgeSourceError, "没有可同步"):
            parse_knowledge_jsonl("\n")

    def test_snapshot_uses_blob_sha_as_document_version(self) -> None:
        entry = parse_knowledge_jsonl(
            json.dumps({"id": "1", "title": "A", "text": "B", "tags": []})
        )[0]
        snapshot = GitHubKnowledgeSnapshot("1234567890abcdef", (entry,))
        self.assertEqual(snapshot.document_name, "NRadio-Knowledge-1234567890ab.md")

    def test_only_prefixed_documents_are_managed(self) -> None:
        self.assertTrue(is_managed_document(f"{MANAGED_DOCUMENT_PREFIX}abc.md"))
        self.assertFalse(is_managed_document("人工导入资料.md"))


if __name__ == "__main__":
    unittest.main()
