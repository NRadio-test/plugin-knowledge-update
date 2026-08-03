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
    web = types.ModuleType("astrbot.api.web")
    web.request = SimpleNamespace(query={}, username="tester")
    web.json_response = lambda value, **_kwargs: value
    web.error_response = lambda message, **kwargs: {
        "status": "error",
        "message": message,
        "status_code": kwargs.get("status_code", 400),
    }

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.web": web,
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
        manager = SimpleNamespace(
            get_kb=lambda _kb_id: _async_value(helper),
            get_kb_by_name=lambda _kb_name: _async_value(helper),
        )
        context = SimpleNamespace(kb_manager=manager)
        return NRadioKnowledgePlugin(context, {})

    def _snapshot(self):
        entry = parse_knowledge_jsonl(
            '{"id":"1","title":"标题","text":"内容","tags":[]}'
        )[0]
        return GitHubKnowledgeSnapshot("1234567890abcdef", (entry,))

    async def test_registers_manager_page_apis(self):
        routes = []
        context = SimpleNamespace(
            kb_manager=SimpleNamespace(),
            register_web_api=lambda *args: routes.append(args),
        )

        NRadioKnowledgePlugin(context, {})

        self.assertEqual(len(routes), 4)
        self.assertIn(
            "/astrbot_plugin_nradio_knowledge/entries/<info_id>/delete",
            [route[0] for route in routes],
        )

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
        self.assertEqual(upload["batch_size"], 20)

    async def test_uses_configured_embedding_batch_size(self):
        helper = FakeKnowledgeBaseHelper([])
        plugin = self._plugin(helper)
        plugin.config["embedding_batch_size"] = 10

        await plugin._sync_target("kb-1", self._snapshot())

        upload = next(call[1] for call in helper.calls if call[0] == "upload")
        self.assertEqual(upload["batch_size"], 10)

    def test_embedding_batch_size_is_bounded_and_tolerates_invalid_values(self):
        plugin = self._plugin(FakeKnowledgeBaseHelper([]))
        plugin.config["embedding_batch_size"] = 0
        self.assertEqual(plugin._embedding_batch_size(), 1)
        plugin.config["embedding_batch_size"] = 999
        self.assertEqual(plugin._embedding_batch_size(), 128)
        plugin.config["embedding_batch_size"] = "invalid"
        self.assertEqual(plugin._embedding_batch_size(), 20)

    async def test_skips_embedding_when_current_version_exists(self):
        current_document = SimpleNamespace(
            doc_name="NRadio-Knowledge-1234567890ab.md",
            doc_id="current-id",
        )
        helper = FakeKnowledgeBaseHelper([current_document])

        result = await self._plugin(helper)._sync_target("kb-1", self._snapshot())

        self.assertFalse(result.uploaded)
        self.assertEqual([call[0] for call in helper.calls], ["list"])

    async def test_resolves_knowledge_base_selected_by_name(self):
        helper = FakeKnowledgeBaseHelper([])
        calls = []

        async def get_kb(kb_ref):
            calls.append(("id", kb_ref))
            return None

        async def get_kb_by_name(kb_ref):
            calls.append(("name", kb_ref))
            return helper

        manager = SimpleNamespace(
            get_kb=get_kb,
            get_kb_by_name=get_kb_by_name,
        )
        plugin = NRadioKnowledgePlugin(SimpleNamespace(kb_manager=manager), {})

        result = await plugin._sync_target("鹏仔", self._snapshot())

        self.assertTrue(result.uploaded)
        self.assertEqual(calls, [("id", "鹏仔"), ("name", "鹏仔")])
        self.assertEqual(result.kb_name, "NRadio 正式知识库")

    async def test_status_reports_last_success_after_failed_check(self):
        plugin = self._plugin(FakeKnowledgeBaseHelper([]))
        states = {
            "last_sync": {
                "ok": False,
                "at": "2026-08-03T01:02:03+00:00",
                "message": "GitHub 暂时不可用",
            },
            "last_success": {
                "ok": True,
                "at": "2026-08-03T00:00:00+00:00",
                "sha": "1234567890abcdef",
                "entry_count": 22,
                "kb_names": ["鹏仔"],
            },
        }

        async def get_kv_data(key, default):
            return states.get(key, default)

        plugin.get_kv_data = get_kv_data
        event = SimpleNamespace(plain_result=lambda value: value)
        messages = [message async for message in plugin.status_command(event)]

        self.assertEqual(len(messages), 1)
        self.assertIn("最近检查：失败", messages[0])
        self.assertIn("上次成功更新：2026-08-03 08:00:00（北京时间）", messages[0])
        self.assertIn("目前知识条数：22", messages[0])
        self.assertIn("目标知识库：鹏仔", messages[0])

    async def test_deleted_info_id_is_excluded_from_synced_chunks(self):
        helper = FakeKnowledgeBaseHelper([])
        plugin = self._plugin(helper)
        plugin.config["target_knowledge_bases"] = ["kb-1"]
        source = parse_knowledge_jsonl(
            '{"id":"keep","title":"保留","text":"保留内容","tags":[]}\n'
            '{"id":"remove","title":"删除","text":"删除内容","tags":[]}'
        )

        async def get_kv_data(key, default):
            if key == plugin_module.DELETED_ENTRIES_KEY:
                return {"remove": {"deleted_at": "2026-08-03T00:00:00+00:00"}}
            return default

        async def put_kv_data(_key, _value):
            return None

        plugin.get_kv_data = get_kv_data
        plugin.put_kv_data = put_kv_data
        snapshot, _ = await plugin._sync_locked(
            GitHubKnowledgeSnapshot("1234567890abcdef", source)
        )

        self.assertEqual([entry.entry_id for entry in snapshot.entries], ["keep"])
        upload = next(call[1] for call in helper.calls if call[0] == "upload")
        self.assertEqual(len(upload["pre_chunked_text"]), 1)
        self.assertIn("InfoID：keep", upload["pre_chunked_text"][0])
        self.assertNotIn("remove", upload["pre_chunked_text"][0])

    async def test_manager_delete_persists_tombstone_and_rebuilds_target(self):
        helper = FakeKnowledgeBaseHelper([])
        plugin = self._plugin(helper)
        plugin.config["target_knowledge_bases"] = ["kb-1"]
        source = GitHubKnowledgeSnapshot(
            "1234567890abcdef",
            parse_knowledge_jsonl(
                '{"id":"keep","title":"保留","text":"保留内容","tags":[]}\n'
                '{"id":"remove","title":"删除","text":"删除内容","tags":[]}'
            ),
        )
        stored = {}

        async def fetch_snapshot():
            return source

        async def get_kv_data(key, default):
            return stored.get(key, default)

        async def put_kv_data(key, value):
            stored[key] = value

        async def request_json(default=None):
            return {"confirm_info_id": "remove"}

        plugin._fetch_snapshot = fetch_snapshot
        plugin.get_kv_data = get_kv_data
        plugin.put_kv_data = put_kv_data
        plugin_module.request.json = request_json
        plugin_module.request.username = "admin"

        result = await plugin.manager_delete_entry("remove")

        self.assertTrue(result["changed"])
        self.assertTrue(result["deleted"])
        self.assertIn("remove", stored[plugin_module.DELETED_ENTRIES_KEY])
        upload = next(call[1] for call in helper.calls if call[0] == "upload")
        self.assertEqual(len(upload["pre_chunked_text"]), 1)
        self.assertIn("InfoID：keep", upload["pre_chunked_text"][0])


async def _async_value(value):
    return value
