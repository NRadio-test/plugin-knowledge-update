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

from .sync_service import (
    GitHubKnowledgeClient,
    GitHubKnowledgeSnapshot,
    KnowledgeSourceError,
    is_managed_document,
)


@dataclass(slots=True)
class TargetSyncResult:
    kb_name: str
    uploaded: bool
    removed_documents: int


@register(
    "astrbot_plugin_nradio_knowledge",
    "NRadio-test",
    "将 NRadio GitHub 知识库安全同步到 AstrBot",
    "1.1.0",
)
class NRadioKnowledgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._sync_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[None] | None = None

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
            target_ids = self._target_kb_ids()
            if not target_ids:
                raise KnowledgeSourceError("请先在插件配置中选择目标 AstrBot 知识库")

            snapshot = await self._fetch_snapshot()
            results: list[TargetSyncResult] = []
            errors: list[str] = []

            for kb_id in target_ids:
                try:
                    result = await self._sync_target(kb_id, snapshot)
                    results.append(result)
                except Exception as exc:
                    errors.append(f"{kb_id}: {exc}")
                    logger.exception(f"NRadio 知识同步到目标 {kb_id} 失败")

            if errors:
                raise RuntimeError("；".join(errors))

            updated = sum(1 for result in results if result.uploaded)
            message = (
                f"GitHub {snapshot.blob_sha[:12]}，{len(snapshot.entries)} 条知识，"
                f"更新 {updated}/{len(results)} 个目标"
            )
            success_state = {
                "ok": True,
                "at": _utc_now(),
                "message": message,
                "sha": snapshot.blob_sha,
                "entry_count": len(snapshot.entries),
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
        return await client.fetch(
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

    async def _sync_target(
        self,
        kb_id: str,
        snapshot: GitHubKnowledgeSnapshot,
    ) -> TargetSyncResult:
        kb_helper = await self.context.kb_manager.get_kb(kb_id)
        if kb_helper is None:
            raise RuntimeError("目标知识库不存在或尚未初始化")

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
