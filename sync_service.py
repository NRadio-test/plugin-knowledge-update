from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


MANAGED_DOCUMENT_PREFIX = "NRadio-Knowledge-"


class KnowledgeSourceError(RuntimeError):
    """Raised when the GitHub knowledge source cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    entry_id: str
    title: str
    text: str
    source_url: str
    uploaded_by: str
    source_type: str
    verified_at: str
    confidence: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitHubKnowledgeSnapshot:
    blob_sha: str
    entries: tuple[KnowledgeEntry, ...]

    @property
    def document_name(self) -> str:
        return f"{MANAGED_DOCUMENT_PREFIX}{self.blob_sha[:12]}.md"

    @property
    def chunks(self) -> list[str]:
        return [render_entry(entry) for entry in self.entries]


def parse_knowledge_jsonl(payload: str) -> tuple[KnowledgeEntry, ...]:
    entries: list[KnowledgeEntry] = []
    seen_ids: set[str] = set()

    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KnowledgeSourceError(
                f"知识库第 {line_number} 行不是有效 JSON：{exc.msg}"
            ) from exc
        if not isinstance(item, dict):
            raise KnowledgeSourceError(f"知识库第 {line_number} 行必须是 JSON 对象")

        entry_id = _required_text(item, "id", line_number)
        if entry_id in seen_ids:
            raise KnowledgeSourceError(f"知识库存在重复 id：{entry_id}")
        seen_ids.add(entry_id)

        tags_value = item.get("tags", [])
        if not isinstance(tags_value, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in tags_value
        ):
            raise KnowledgeSourceError(
                f"知识库第 {line_number} 行的 tags 必须是非空字符串数组"
            )

        entries.append(
            KnowledgeEntry(
                entry_id=entry_id,
                title=_required_text(item, "title", line_number),
                text=_required_text(item, "text", line_number),
                source_url=_optional_text(item, "source_url"),
                uploaded_by=_optional_text(item, "uploaded_by") or "未知",
                source_type=_optional_text(item, "source_type") or "unspecified",
                verified_at=_optional_text(item, "verified_at"),
                confidence=_optional_text(item, "confidence") or "unspecified",
                tags=tuple(tag.strip() for tag in tags_value),
            )
        )

    if not entries:
        raise KnowledgeSourceError("知识库文件没有可同步的条目")
    return tuple(entries)


def render_entry(entry: KnowledgeEntry) -> str:
    lines = [
        f"# {entry.title}",
        "",
        f"知识编号：{entry.entry_id}",
        f"知识内容：{entry.text}",
        f"上传者：{entry.uploaded_by}",
        f"来源类型：{entry.source_type}",
        f"可信度：{entry.confidence}",
    ]
    if entry.verified_at:
        lines.append(f"核验日期：{entry.verified_at}")
    if entry.source_url:
        lines.append(f"来源：{entry.source_url}")
    if entry.tags:
        lines.append(f"标签：{'、'.join(entry.tags)}")
    return "\n".join(lines)


def is_managed_document(document_name: str) -> bool:
    return document_name.startswith(MANAGED_DOCUMENT_PREFIX)


class GitHubKnowledgeClient:
    def __init__(self, token: str, timeout_seconds: int = 30) -> None:
        self._token = token.strip()
        self._timeout_seconds = timeout_seconds

    async def fetch(
        self,
        repository: str,
        branch: str,
        knowledge_path: str,
    ) -> GitHubKnowledgeSnapshot:
        import aiohttp

        repository = repository.strip().strip("/")
        if repository.count("/") != 1:
            raise KnowledgeSourceError("GitHub 仓库必须使用 owner/repo 格式")

        encoded_path = quote(knowledge_path.strip().lstrip("/"), safe="/")
        url = f"https://api.github.com/repos/{repository}/contents/{encoded_path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "astrbot-plugin-nradio-knowledge",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers=headers,
                    params={"ref": branch.strip() or "main"},
                ) as response:
                    response_text = await response.text()
                    if response.status != 200:
                        raise KnowledgeSourceError(
                            f"GitHub 返回 HTTP {response.status}："
                            f"{_github_error_message(response_text)}"
                        )
        except aiohttp.ClientError as exc:
            raise KnowledgeSourceError(f"连接 GitHub 失败：{exc}") from exc

        try:
            data: dict[str, Any] = json.loads(response_text)
            blob_sha = str(data["sha"]).strip()
            encoded_content = str(data["content"]).replace("\n", "")
            decoded = base64.b64decode(encoded_content, validate=True).decode("utf-8")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise KnowledgeSourceError("GitHub 返回的知识库文件格式异常") from exc

        if not blob_sha:
            raise KnowledgeSourceError("GitHub 返回的知识库版本标识为空")
        return GitHubKnowledgeSnapshot(
            blob_sha=blob_sha,
            entries=parse_knowledge_jsonl(decoded),
        )


def _required_text(item: dict[str, Any], key: str, line_number: int) -> str:
    value = _optional_text(item, key)
    if not value:
        raise KnowledgeSourceError(f"知识库第 {line_number} 行缺少 {key}")
    return value


def _optional_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _github_error_message(payload: str) -> str:
    try:
        data = json.loads(payload)
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    except json.JSONDecodeError:
        pass
    return payload.strip()[:200] or "未知错误"
