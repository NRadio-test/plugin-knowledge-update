from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

from .sync_service import (
    GitHubKnowledgeClient,
    GitHubKnowledgeSnapshot,
    KnowledgeSourceError,
    is_managed_document,
)

PLUGIN_NAME = "astrbot_plugin_nradio_knowledge"
DELETED_ENTRIES_KEY = "deleted_info_ids"


@dataclass(slots=True)
class TargetSyncResult:
    kb_name: str
    uploaded: bool
    removed_documents: int


@register(
    "astrbot_plugin_nradio_knowledge",
    "NRadio-test",
    "同步并按 InfoID 管理 NRadio AstrBot 知识库",
    "1.2.3",
)
class NRadioKnowledgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sync_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._source_snapshot: GitHubKnowledgeSnapshot | None = None
        self._register_manager_apis()

    def _register_manager_apis(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        register_api(
            f"/{PLUGIN_NAME}/entries",
            self.manager_entries,
            ["GET"],
            "List NRadio knowledge entries",
        )
        register_api(
            f"/{PLUGIN_NAME}/entries/<info_id>/delete",
            self.manager_delete_entry,
            ["POST"],
            "Delete one NRadio knowledge entry from AstrBot",
        )
        register_api(
            f"/{PLUGIN_NAME}/entries/<info_id>/restore",
            self.manager_restore_entry,
            ["POST"],
            "Restore one deleted NRadio knowledge entry",
        )
        register_api(
            f"/{PLUGIN_NAME}/sync",
            self.manager_sync,
            ["POST"],
            "Synchronize NRadio knowledge now",
        )

    async def initialize(self) -> None:
        if bool(self.config.get("auto_sync", True)):
            self._scheduler_task = asyncio.create_task(
                self._scheduler_loop(),
                name="nradio-knowledge-sync",
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ku-up", alias={"nradio_kb_sync"})
    async def sync_command(self, event: AstrMessageEvent):
        """立即把 GitHub 中的 NRadio 知识同步到选定的 AstrBot 知识库。"""
        if self._sync_lock.locked():
            yield event.plain_result("NRadio 知识库正在同步，请稍后再试。")
            return

        try:
            snapshot, results = await self._sync()
        except Exception as exc:
            await self._remember_failure(str(exc))
            logger.exception("NRadio 知识库手动同步失败")
            yield event.plain_result(f"NRadio 知识库同步失败：{exc}")
            return

        updated = sum(1 for result in results if result.uploaded)
        skipped = len(results) - updated
        yield event.plain_result(
            "NRadio 知识库同步完成："
            f"GitHub 版本 {snapshot.blob_sha[:12]}，共 {len(snapshot.entries)} 条知识，"
            f"更新 {updated} 个知识库，已是最新版 {skipped} 个。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ku-info", alias={"nradio_kb_status"})
    async def status_command(self, event: AstrMessageEvent):
        """查看 NRadio 知识库最近一次同步状态。"""
        last_check = await self.get_kv_data("last_sync", None)
        last_success = await self.get_kv_data("last_success", None)
        if (
            not isinstance(last_success, dict)
            and isinstance(last_check, dict)
            and last_check.get("ok")
        ):
            last_success = last_check
        if not isinstance(last_check, dict) and not isinstance(last_success, dict):
            yield event.plain_result("NRadio 知识库还没有同步记录。")
            return

        lines = ["NRadio 知识库状态"]
        if isinstance(last_check, dict):
            status = "成功" if last_check.get("ok") else "失败"
            lines.extend(
                [
                    f"最近检查：{status}",
                    f"检查时间：{_format_sync_time(last_check.get('at'))}",
                ]
            )
            if not last_check.get("ok"):
                lines.append(f"失败原因：{last_check.get('message', '无详情')}")

        if isinstance(last_success, dict):
            lines.extend(
                [
                    f"上次成功更新：{_format_sync_time(last_success.get('at'))}",
                    f"目前知识条数：{last_success.get('entry_count', '未知')}",
                    f"GitHub 版本：{str(last_success.get('sha', '未知'))[:12]}",
                ]
            )
            kb_names = last_success.get("kb_names", [])
            if isinstance(kb_names, list) and kb_names:
                lines.append(f"目标知识库：{'、'.join(map(str, kb_names))}")
        else:
            lines.append("上次成功更新：尚无")

        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task

    async def _scheduler_loop(self) -> None:
        initial_delay = max(int(self.config.get("initial_delay_seconds", 15)), 0)
        interval_minutes = max(int(self.config.get("sync_interval_minutes", 30)), 5)
        await asyncio.sleep(initial_delay)

        while True:
            try:
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._remember_failure(str(exc))
                logger.warning(f"NRadio 知识库自动同步失败：{exc}")
            await asyncio.sleep(interval_minutes * 60)

    async def _sync(
        self,
    ) -> tuple[GitHubKnowledgeSnapshot, list[TargetSyncResult]]:
        async with self._sync_lock:
            source_snapshot = await self._fetch_snapshot()
            return await self._sync_locked(source_snapshot)

    async def _sync_locked(
        self,
        source_snapshot: GitHubKnowledgeSnapshot,
    ) -> tuple[GitHubKnowledgeSnapshot, list[TargetSyncResult]]:
        target_ids = self._target_kb_ids()
        if not target_ids:
            raise KnowledgeSourceError("请先在插件配置中选择目标 AstrBot 知识库")

        deleted = await self._deleted_entries()
        snapshot = source_snapshot.excluding(set(deleted))
        if not snapshot.entries:
            raise KnowledgeSourceError("不能删除全部知识；AstrBot 检索库至少需要保留一条")

        results: list[TargetSyncResult] = []
        errors: list[str] = []
        for kb_id in target_ids:
            try:
                results.append(await self._sync_target(kb_id, snapshot))
            except Exception as exc:
                errors.append(f"{kb_id}: {exc}")
                logger.exception(f"NRadio 知识同步到目标 {kb_id} 失败")
        if errors:
            raise RuntimeError("；".join(errors))

        updated = sum(1 for result in results if result.uploaded)
        message = (
            f"GitHub {source_snapshot.blob_sha[:12]}，源数据 {len(source_snapshot.entries)} 条，"
            f"AstrBot 启用 {len(snapshot.entries)} 条，更新 {updated}/{len(results)} 个目标"
        )
        success_state = {
            "ok": True,
            "at": _utc_now(),
            "message": message,
            "sha": source_snapshot.blob_sha,
            "entry_count": len(snapshot.entries),
            "source_entry_count": len(source_snapshot.entries),
            "deleted_entry_count": len(source_snapshot.entries) - len(snapshot.entries),
            "kb_names": [result.kb_name for result in results],
        }
        await self.put_kv_data("last_sync", success_state)
        await self.put_kv_data("last_success", success_state)
        logger.info(f"NRadio 知识库同步完成：{message}")
        return snapshot, results

    async def _fetch_snapshot(self) -> GitHubKnowledgeSnapshot:
        configured_token = str(self.config.get("github_token", "")).strip()
        token_env = str(
            self.config.get("github_token_environment", "NRADIO_GITHUB_TOKEN")
        ).strip()
        token = configured_token or (os.environ.get(token_env, "") if token_env else "")
        if not token:
            raise KnowledgeSourceError(
                "未配置 GitHub Token；请填写插件配置或设置 NRADIO_GITHUB_TOKEN"
            )

        client = GitHubKnowledgeClient(token=token)
        snapshot = await client.fetch(
            repository=str(
                self.config.get("github_repository", "NRadio-test/nradio-web-platform")
            ),
            branch=str(self.config.get("github_branch", "main")),
            knowledge_path=str(
                self.config.get(
                    "knowledge_path", "knowledge-base/import/knowledge.jsonl"
                )
            ),
        )
        self._source_snapshot = snapshot
        return snapshot

    async def _manager_source(self, refresh: bool = False) -> GitHubKnowledgeSnapshot:
        if refresh or self._source_snapshot is None:
            return await self._fetch_snapshot()
        return self._source_snapshot

    async def _deleted_entries(self) -> dict[str, dict[str, str]]:
        value = await self.get_kv_data(DELETED_ENTRIES_KEY, {})
        if not isinstance(value, dict):
            return {}
        output: dict[str, dict[str, str]] = {}
        for info_id, details in value.items():
            if not isinstance(info_id, str) or not info_id.strip():
                continue
            clean_details = details if isinstance(details, dict) else {}
            output[info_id] = {
                "deleted_at": str(clean_details.get("deleted_at", "")),
                "deleted_by": str(clean_details.get("deleted_by", "")),
                "title": str(clean_details.get("title", "")),
            }
        return output

    async def manager_entries(self):
        try:
            refresh = request.query.get("refresh", "0") in {"1", "true", "yes"}
            source = await self._manager_source(refresh=refresh)
            deleted = await self._deleted_entries()
            query = str(request.query.get("q", "")).strip().casefold()
            status = str(request.query.get("status", "active")).strip().lower()
            if status not in {"active", "deleted", "all"}:
                status = "active"
            page = max(request.query.get("page", 1, type=int), 1)
            page_size = min(max(request.query.get("page_size", 24, type=int), 1), 100)

            matches = []
            for entry in source.entries:
                is_deleted = entry.entry_id in deleted
                if status == "active" and is_deleted:
                    continue
                if status == "deleted" and not is_deleted:
                    continue
                haystack = " ".join(
                    (entry.entry_id, entry.title, entry.text, entry.source_url,
                     entry.uploaded_by, entry.source_type, entry.verified_at,
                     entry.confidence, *entry.tags)
                ).casefold()
                if query and not all(part in haystack for part in query.split()):
                    continue
                matches.append(_entry_payload(entry, deleted.get(entry.entry_id)))

            start = (page - 1) * page_size
            source_ids = {entry.entry_id for entry in source.entries}
            return json_response({
                "entries": matches[start:start + page_size],
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": len(matches),
                    "pages": max((len(matches) + page_size - 1) // page_size, 1),
                },
                "stats": {
                    "source": len(source.entries),
                    "active": sum(entry.entry_id not in deleted for entry in source.entries),
                    "deleted": sum(entry.entry_id in deleted for entry in source.entries),
                    "orphaned_deleted": sum(info_id not in source_ids for info_id in deleted),
                    "sha": source.blob_sha,
                    "targets": self._target_kb_ids(),
                },
            })
        except Exception as exc:
            logger.exception("NRadio 知识管理器读取失败")
            return error_response(f"读取知识条目失败：{exc}", status_code=500)

    async def manager_delete_entry(self, info_id: str):
        return await self._manager_set_deleted(info_id, deleted=True)

    async def manager_restore_entry(self, info_id: str):
        return await self._manager_set_deleted(info_id, deleted=False)

    async def _manager_set_deleted(self, info_id: str, deleted: bool):
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("confirm_info_id") != info_id:
            return error_response("确认 InfoID 不匹配，操作已取消", status_code=400)
        try:
            async with self._sync_lock:
                source = await self._fetch_snapshot()
                entry = next((item for item in source.entries if item.entry_id == info_id), None)
                if entry is None:
                    return error_response("指定 InfoID 不存在", status_code=404)

                records = await self._deleted_entries()
                if deleted:
                    if info_id in records:
                        return json_response({"changed": False, "deleted": True, "info_id": info_id})
                    if len(source.entries) - sum(item.entry_id in records for item in source.entries) <= 1:
                        return error_response("不能删除最后一条启用知识", status_code=409)
                    records[info_id] = {
                        "deleted_at": _utc_now(),
                        "deleted_by": request.username or "AstrBot Dashboard",
                        "title": entry.title,
                    }
                else:
                    if info_id not in records:
                        return json_response({"changed": False, "deleted": False, "info_id": info_id})
                    records.pop(info_id)

                await self.put_kv_data(DELETED_ENTRIES_KEY, records)
                try:
                    snapshot, results = await self._sync_locked(source)
                except Exception as exc:
                    await self._remember_failure(str(exc))
                    return error_response(
                        f"状态已经保存，但同步到 AstrBot 知识库失败：{exc}",
                        status_code=502,
                    )
                return json_response({
                    "changed": True,
                    "deleted": deleted,
                    "info_id": info_id,
                    "active_count": len(snapshot.entries),
                    "updated_targets": sum(result.uploaded for result in results),
                })
        except Exception as exc:
            logger.exception(f"NRadio InfoID {info_id} 状态修改失败")
            return error_response(f"操作失败：{exc}", status_code=500)

    async def manager_sync(self):
        try:
            snapshot, results = await self._sync()
            return json_response({
                "entry_count": len(snapshot.entries),
                "sha": snapshot.blob_sha,
                "updated_targets": sum(result.uploaded for result in results),
            })
        except Exception as exc:
            await self._remember_failure(str(exc))
            logger.exception("NRadio 管理器手动同步失败")
            return error_response(f"同步失败：{exc}", status_code=500)

    async def _sync_target(
        self,
        kb_ref: str,
        snapshot: GitHubKnowledgeSnapshot,
    ) -> TargetSyncResult:
        kb_manager = self.context.kb_manager
        kb_helper = await kb_manager.get_kb(kb_ref)
        if kb_helper is None:
            get_kb_by_name = getattr(kb_manager, "get_kb_by_name", None)
            if callable(get_kb_by_name):
                kb_helper = await get_kb_by_name(kb_ref)
        if kb_helper is None:
            raise RuntimeError(f"目标知识库 {kb_ref} 不存在或尚未初始化")

        current_documents = await kb_helper.list_documents(
            limit=500,
            search="NRadio-Knowledge-",
        )
        if any(doc.doc_name == snapshot.document_name for doc in current_documents):
            return TargetSyncResult(
                kb_name=kb_helper.kb.kb_name,
                uploaded=False,
                removed_documents=0,
            )

        await kb_helper.upload_document(
            file_name=snapshot.document_name,
            file_content=None,
            file_type="md",
            pre_chunked_text=snapshot.chunks,
            batch_size=self._embedding_batch_size(),
        )

        removed = 0
        for document in current_documents:
            if is_managed_document(document.doc_name):
                await kb_helper.delete_document(document.doc_id)
                removed += 1

        return TargetSyncResult(
            kb_name=kb_helper.kb.kb_name,
            uploaded=True,
            removed_documents=removed,
        )

    def _embedding_batch_size(self) -> int:
        value = self.config.get("embedding_batch_size", 20)
        try:
            batch_size = int(value)
        except (TypeError, ValueError):
            logger.warning(
                "NRadio embedding_batch_size=%r 无效，已使用默认值 20。",
                value,
            )
            return 20
        return max(1, min(batch_size, 128))

    def _target_kb_ids(self) -> list[str]:
        value: Any = self.config.get("target_knowledge_bases", [])
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    async def _remember_failure(self, message: str) -> None:
        await self.put_kv_data(
            "last_sync",
            {
                "ok": False,
                "at": _utc_now(),
                "message": message,
            },
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_sync_time(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        beijing = parsed.astimezone(timezone(timedelta(hours=8)))
        return beijing.strftime("%Y-%m-%d %H:%M:%S（北京时间）")
    except ValueError:
        return value


def _entry_payload(entry: Any, deletion: dict[str, str] | None) -> dict[str, Any]:
    return {
        "info_id": entry.entry_id,
        "title": entry.title,
        "text": entry.text,
        "source_url": entry.source_url,
        "uploaded_by": entry.uploaded_by,
        "source_type": entry.source_type,
        "verified_at": entry.verified_at,
        "confidence": entry.confidence,
        "tags": list(entry.tags),
        "deleted": deletion is not None,
        "deleted_at": deletion.get("deleted_at", "") if deletion else "",
        "deleted_by": deletion.get("deleted_by", "") if deletion else "",
    }
