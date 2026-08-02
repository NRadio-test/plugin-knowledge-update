import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from sync_service import GitHubKnowledgeSnapshot, parse_knowledge_jsonl


def _install_astrbot_stubs() -> None:
    class FakeLogger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    class FakeStar:
        def __init__(self, context):
            self.context = context

    def decorator(*_args, **_kwargs):
        return lambda target: target

    fake_filter = SimpleNamespace(
        PermissionType=SimpleNamespace(ADMIN="admin"),
        permission_type=decorator,
        command=decorator,
    )

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = FakeLogger()
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = fake_filter
    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = FakeStar
    star.register = decorator

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


_install_astrbot_stubs()
package_name = "astrbot_plugin_nradio_knowledge"
package = types.ModuleType(package_name)
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package_name] = package
plugin_module = importlib.import_module(f"{package_name}.main")
NRadioKnowledgePlugin = plugin_module.NRadioKnowledgePlugin


class FakeKnowledgeBaseHelper:
    def __init__(self, documents):
        self.kb = SimpleNamespace(kb_name="NRadio 正式知识库")
        self.documents = documents
        self.calls = []

    async def list_documents(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.documents

    async def upload_document(self, **kwargs):
        self.calls.append(("upload", kwargs))

    async def delete_document(self, doc_id):
        self.calls.append(("delete", doc_id))


class PluginSyncTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, helper):
        manager = SimpleNamespace(get_kb=lambda _kb_id: _async_value(helper))
        context = SimpleNamespace(kb_manager=manager)
        return NRadioKnowledgePlugin(context, {})

    def _snapshot(self):
        entry = parse_knowledge_jsonl(
            '{"id":"1","title":"标题","text":"内容","tags":[]}'
        )[0]
        return GitHubKnowledgeSnapshot("1234567890abcdef", (entry,))

    async def test_uploads_new_version_before_deleting_old_version(self):
        old_document = SimpleNamespace(
            doc_name="NRadio-Knowledge-old.md",
            doc_id="old-id",
        )
        helper = FakeKnowledgeBaseHelper([old_document])

        result = await self._plugin(helper)._sync_target("kb-1", self._snapshot())

        self.assertTrue(result.uploaded)
        self.assertEqual([call[0] for call in helper.calls], ["list", "upload", "delete"])
        upload = helper.calls[1][1]
        self.assertEqual(upload["file_name"], "NRadio-Knowledge-1234567890ab.md")
        self.assertEqual(upload["file_type"], "md")
        self.assertEqual(len(upload["pre_chunked_text"]), 1)

    async def test_skips_embedding_when_current_version_exists(self):
        current_document = SimpleNamespace(
            doc_name="NRadio-Knowledge-1234567890ab.md",
            doc_id="current-id",
        )
        helper = FakeKnowledgeBaseHelper([current_document])

        result = await self._plugin(helper)._sync_target("kb-1", self._snapshot())

        self.assertFalse(result.uploaded)
        self.assertEqual([call[0] for call in helper.calls], ["list"])


async def _async_value(value):
    return value
