"""Shared SmartCanvas documents used by the Markdown-first handoff flow."""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import html
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from handoff_core import HandoffError, digest, table_time

AGREEMENT_PAGE = "当前约定"
AGREEMENT_HEADINGS = {AGREEMENT_PAGE, "事项描述"}
EVENT_PREFIX = "[dev-handoff-event:"


def dialogue_entry(actor, message, event_id, timestamp=None):
    """One chronological reply; keep its recovery marker after the readable body."""
    stamp = datetime.fromisoformat(timestamp or table_time()).astimezone(timezone(timedelta(hours=8)))
    author = re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", " ".join(actor.split()))
    return (f"\n\n---\n\n**{author}** · {stamp:%Y-%m-%d %H:%M}（UTC+8）\n\n"
            f"{message.strip()}\n\n{EVENT_PREFIX}{event_id}]\n")


def conversation_markdown(agreement, message, actor, event_id):
    # Line separators keep the current agreement in one editable block.
    current = agreement.strip().replace("\r\n", "\n").replace("\n", "\u2028")
    return (f"## 事项描述\n\n{current}\n\n---\n\n## 沟通记录"
            + dialogue_entry(actor, message, event_id))


def _element(value):
    raw = value.get("element") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _text(value):
    rich = _element(value).get("rich_text") or {}
    return rich.get("text", "") if isinstance(rich, dict) else ""


def _doc_id(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "docs.qq.com" or not parsed.path.startswith(("/doc/", "/smartcanvas/", "/aio/")):
        raise HandoffError("invalid_document_url", "请提供腾讯智能文档的 HTTPS 链接。")
    encoded = parsed.path.strip("/").split("/")[-1]
    if not encoded.startswith("D"):
        raise HandoffError("invalid_document_url", "文档链接没有可识别的 ID。")
    try:
        return base64.b64decode(encoded[1:] + "=" * (-len(encoded[1:]) % 4), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise HandoffError("invalid_document_url", "文档链接 ID 无法解析。") from None


class Documents:
    def __init__(self, client):
        self.client = client
        self.mode = None

    def require_capabilities(self):
        if self.mode:
            return self.mode
        if not hasattr(self.client, "list_tools"):
            self.mode = "elements"  # In-memory test clients provide the methods directly.
            return self.mode
        available = set(self.client.list_tools())
        common = {"manage.create_file", "manage.folder_list", "manage.query_file_info", "get_content",
                  "smartcanvas.get_top_level_pages"}
        elements = {"smartcanvas.get_page_info", "smartcanvas.create_smartcanvas_element",
                    "smartcanvas.update_element", "smartcanvas.append_insert_smartcanvas_by_markdown"}
        blocks = {"create_smartcanvas_by_mdx", "manage.move_file", "smartcanvas.read", "smartcanvas.edit"}
        if common <= available and blocks <= available:
            self.mode = "blocks"
        elif common <= available and elements <= available:
            self.mode = "elements"
        else:
            raise HandoffError("tencent_document_tools_missing", "当前腾讯账号缺少项目组文档所需工具；未创建或移动任何文件。",
                               common_missing=sorted(common - available),
                               supported_modes={"elements": sorted(elements - available), "blocks": sorted(blocks - available)})
        return self.mode

    def source(self, selector):
        if selector.startswith("https://"):
            file_id = _doc_id(selector)
            meta = self.client.call("manage.query_file_info", {"file_id": file_id})
            if meta.get("file_id") != file_id or meta.get("type") != "smartcanvas":
                raise HandoffError("invalid_document", "未读到指定腾讯智能文档。")
            body = self.content(file_id)
            return {"content": body, "source": meta.get("url") or selector, "digest": digest(body)}
        path = Path(selector).expanduser().resolve()
        if path.suffix.lower() != ".md" or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise HandoffError("invalid_markdown", "请提供不超过 4 MB 的本地 .md 文件或腾讯智能文档链接。")
        body = path.read_text(encoding="utf-8")
        return {"content": body, "source": str(path), "digest": digest(body)}

    def content(self, file_id):
        if self.require_capabilities() == "blocks":
            return self._blocks(file_id)
        value = self.client.call("get_content", {"file_id": file_id})
        body = value.get("content")
        if not isinstance(body, str):
            raise HandoffError("document_unreadable", "腾讯文档未返回完整正文。")
        return body

    def _has_event(self, file_id, marker):
        value = self.client.call("get_content", {"file_id": file_id})
        body = value.get("content")
        if not isinstance(body, str):
            raise HandoffError("document_unreadable", "腾讯文档未返回完整正文。")
        return marker in body

    def folder_items(self, folder_id):
        items, start = [], 0
        for _ in range(100):
            value = self.client.call("manage.folder_list", {"folder_id": folder_id, "start": start})
            page = value.get("list")
            if not isinstance(page, list):
                raise HandoffError("folder_unreadable", "项目组目录未返回完整文件列表。")
            items.extend(page)
            if value.get("finish") is True:
                return items
            if not page:
                raise HandoffError("folder_unreadable", "项目组目录分页未前进。")
            start += len(page)
        raise HandoffError("folder_unreadable", "项目组目录超过分页上限。")

    def create(self, folder_id, title, key, agreement, initial_text, actor, source_ref="", uncertain=False):
        if not isinstance(agreement, str) or not agreement.strip():
            raise HandoffError("invalid_agreement", "每条工单须有非空的当前约定。")
        mode = self.require_capabilities()
        stable_title = (title[:23].rstrip() + " · " + digest(key)[:8])[:36]
        link_mode = isinstance(folder_id, str) and folder_id.startswith("link:")
        target_folder = "" if link_mode else folder_id
        found = [item for item in self.folder_items(target_folder) if item.get("title") == stable_title]
        if mode == "blocks":
            result = self._create_blocks(target_folder, stable_title, key, agreement, initial_text, actor, source_ref,
                                         found, uncertain, link_mode=link_mode)
            if link_mode:
                self.ensure_link_edit(result["file_id"])
            return result
        if len(found) > 1:
            raise HandoffError("document_ambiguous", "项目组中存在重复工单文档标题。")
        if found:
            file_id = found[0].get("id")
            meta = self.client.call("manage.query_file_info", {"file_id": file_id})
            if meta.get("type") != "smartcanvas":
                raise HandoffError("document_ambiguous", "同名文件不是腾讯智能文档。")
        else:
            if uncertain:
                raise HandoffError("document_create_unknown", "先前创建结果仍未在目录中出现；已停止自动重建，避免重复文档。")
            try:
                meta = self.client.call("manage.create_file", {"title": stable_title, "file_type": "smartcanvas", "parent_id": target_folder}, write=True)
            except HandoffError as exc:
                if exc.code == "write_unknown":
                    raise HandoffError("document_create_unknown", "创建文档结果未知；请重试，系统会先按稳定标题回读。") from None
                raise
            file_id = meta.get("file_id")
            if not file_id:
                raise HandoffError("document_create_unknown", "创建文档未返回 ID；请重试并先回读目录。")
        self.ensure_agreement(file_id, agreement, uncertain=uncertain)
        note = "**发布事项**\n\n" + ((f"相关资料：{source_ref}\n\n" if source_ref else "") + initial_text)
        if note.strip():
            self.append_once(file_id, "source-" + digest(key)[:16], actor, note, uncertain=uncertain)
        url = meta.get("url") or self.client.call("manage.query_file_info", {"file_id": file_id}).get("url")
        if not isinstance(url, str) or not url.startswith("https://docs.qq.com/"):
            raise HandoffError("document_url_missing", "文档已创建，但未回读到可访问链接。")
        if link_mode:
            self.ensure_link_edit(file_id)
        return {"file_id": file_id, "url": url, "agreement_hash": digest(agreement.strip())}

    def ensure_link_edit(self, file_id):
        policy = self.client.call("manage.get_privilege", {"file_id": file_id}).get("policy")
        if policy == 3:
            return
        if policy not in (0, 1, 2):
            raise HandoffError("document_policy_unknown", "未读到工单文档的有效分享策略；未写入表格记录。")
        try:
            self.client.call("manage.set_privilege", {"file_id": file_id, "policy": 3}, write=True)
        except HandoffError as exc:
            if exc.code != "write_unknown":
                raise
        if self.client.call("manage.get_privilege", {"file_id": file_id}).get("policy") != 3:
            raise HandoffError("document_share_unknown", "工单文档的链接编辑权限未通过回读确认；未写入表格记录。")

    def _create_blocks(self, folder_id, stable_title, key, agreement, initial_text, actor, source_ref, found, uncertain,
                       link_mode=False):
        if len(found) > 1:
            raise HandoffError("document_ambiguous", "项目组中存在重复工单文档标题。")
        if found:
            file_id = found[0].get("id")
        else:
            in_root = [item for item in self.folder_items("") if item.get("title") == stable_title]
            if len(in_root) > 1:
                raise HandoffError("document_ambiguous", "首页存在重复工单文档标题。")
            if in_root:
                file_id = in_root[0].get("id")
            else:
                if uncertain:
                    raise HandoffError("document_create_unknown", "先前创建结果仍未在首页或组内出现；已停止自动重建。")
                event = "source-" + digest(key)[:16]
                note = "**发布事项**\n\n" + ((f"相关资料：{source_ref}\n\n" if source_ref else "") + initial_text)
                markdown = conversation_markdown(agreement, note, actor, event)
                try:
                    created = self.client.call("create_smartcanvas_by_mdx",
                                               {"title": stable_title, "mdx": markdown, "content_format": "markdown"},
                                               write=True)
                except HandoffError as exc:
                    if exc.code == "write_unknown":
                        raise HandoffError("document_create_unknown", "创建结果未知；重试时会先从首页和组内回读。") from None
                    raise
                file_id = created.get("file_id")
                if not file_id:
                    raise HandoffError("document_create_unknown", "创建文档未返回 ID；请先回读首页。")
            try:
                if not link_mode:
                    self.client.call("manage.move_file", {"file_id": file_id, "target_folder_id": folder_id}, write=True)
            except HandoffError as exc:
                if exc.code != "write_unknown":
                    raise
            if file_id not in {item.get("id") for item in self.folder_items(folder_id)}:
                raise HandoffError("document_move_unknown", "文档尚未在项目组目录中出现；已停止后续表格写入。")
        meta = self.client.call("manage.query_file_info", {"file_id": file_id})
        if meta.get("type") != "smartcanvas":
            raise HandoffError("document_ambiguous", "稳定标题对应的文件不是智能文档。")
        current = self.agreement(file_id)
        expected = agreement.strip().replace("\r\n", "\n")
        if current["text"] != expected:
            raise HandoffError("agreement_changed", "现有当前约定与待发布草稿不同，未覆盖。")
        marker = EVENT_PREFIX + "source-" + digest(key)[:16] + "]"
        if not self._has_event(file_id, marker):
            raise HandoffError("document_create_unknown", "来源对话未通过回读校验；未创建表格记录。")
        url = meta.get("url")
        if not isinstance(url, str) or not url.startswith("https://docs.qq.com/"):
            raise HandoffError("document_url_missing", "文档已创建，但未回读到可访问链接。")
        return {"file_id": file_id, "url": url, "agreement_hash": current["hash"]}

    def _blocks(self, file_id):
        chunks, cursor = [], ""
        for _ in range(100):
            args = {"file_id": file_id}
            if cursor:
                args["next_token"] = cursor
            value = self.client.call("smartcanvas.read", args)
            body = value.get("content")
            if not isinstance(body, str):
                raise HandoffError("agreement_unreadable", "智能文档未返回完整 MDX 内容。")
            if chunks:
                # The live read API repeats document front matter on every page.
                body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", body, count=1, flags=re.S)
            chunks.append(body)
            nxt = value.get("next_token") or ""
            if not nxt:
                return "\n".join(chunks)
            if nxt == cursor:
                raise HandoffError("agreement_unreadable", "智能文档分页游标未前进。")
            cursor = nxt
        raise HandoffError("agreement_unreadable", "智能文档超过分页上限。")

    def _block_agreement(self, file_id):
        mdx = self._blocks(file_id)
        blocks = list(re.finditer(r'<(Heading|Paragraph|BulletedList|NumberedList)\b[^>]*\bid="([^"]+)"[^>]*>\s*(.*?)\s*</\1>|<Divider\b[^>]*/>',
                                 mdx, re.S))
        start = next((i for i, block in enumerate(blocks)
                      if block.group(1) == "Heading" and html.unescape(block.group(3) or "").strip() in AGREEMENT_HEADINGS), None)
        if start is None:
            raise HandoffError("agreement_unreadable", "文档顶部缺少当前约定标题。")
        selected = []
        for block in blocks[start + 1:]:
            if block.group(1) is None or (block.group(1) == "Heading" and any(
                    label in (block.group(3) or "") for label in ("对话", "沟通记录"))):
                break
            selected.append(block)
        if not selected:
            raise HandoffError("agreement_unreadable", "当前约定不能为空。")
        parts = []
        for block in selected:
            body = html.unescape(block.group(3) or "").strip().replace("\u2028", "\n")
            # SmartCanvas exports literal Markdown punctuation escaped in MDX.
            # Compare the document text, not serialization escapes (e.g. empty\_text).
            body = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", body)
            if block.group(1) == "BulletedList":
                body = "- " + body
            elif block.group(1) == "NumberedList":
                body = "1. " + body
            parts.append(body)
        value = "\n\n".join(parts).strip()
        if not value:
            raise HandoffError("agreement_unreadable", "当前约定不能为空。")
        return {"text": value, "hash": digest(value),
                "element_id": selected[0].group(2) if len(selected) == 1 and selected[0].group(1) == "Paragraph" else None}

    def _agreement_elements(self, file_id):
        pages = self.client.call("smartcanvas.get_top_level_pages", {"file_id": file_id}).get("top_level_pages")
        if not isinstance(pages, list):
            raise HandoffError("agreement_unreadable", "无法读取文档页面结构。")
        found = [p for p in pages if _element(p).get("title") == AGREEMENT_PAGE]
        if len(found) > 1:
            raise HandoffError("agreement_ambiguous", "文档中存在多个当前约定页面。")
        if not found:
            return None, []
        page_id = found[0].get("id")
        items, cursor = [], None
        for _ in range(100):
            args = {"file_id": file_id, "page_id": page_id}
            if cursor:
                args["cursor"] = cursor
            response = self.client.call("smartcanvas.get_page_info", args)
            batch = response.get("element_infos")
            if not isinstance(batch, list):
                raise HandoffError("agreement_unreadable", "当前约定页面未返回完整内容。")
            items.extend(batch)
            if response.get("is_over") is True:
                return page_id, [item for item in items if item.get("type") == "Text"]
            next_cursor = response.get("cursor")
            if not next_cursor or next_cursor == cursor:
                raise HandoffError("agreement_unreadable", "当前约定分页未前进。")
            cursor = next_cursor
        raise HandoffError("agreement_unreadable", "当前约定内容超过分页上限。")

    def ensure_agreement(self, file_id, agreement, uncertain=False):
        page_id, texts = self._agreement_elements(file_id)
        if page_id is None:
            if uncertain:
                raise HandoffError("agreement_create_unknown", "当前约定页面尚未出现；已停止自动重建。")
            result = self.client.call("smartcanvas.create_smartcanvas_element", {"file_id": file_id, "pages": [{"title": AGREEMENT_PAGE}]}, write=True)
            pages = result.get("element_infos") or []
            page_id = pages[0].get("id") if pages else None
            if not page_id:
                page_id, texts = self._agreement_elements(file_id)
            if not page_id:
                raise HandoffError("agreement_create_unknown", "当前约定页面创建结果未知，请回读后重试。")
        if texts:
            if len(texts) != 1 or _text(texts[0]).strip() != agreement.strip():
                raise HandoffError("agreement_changed", "现有当前约定与待发布草稿不同，未覆盖。")
            return
        if uncertain:
            raise HandoffError("agreement_create_unknown", "当前约定文本尚未出现；已停止自动重建。")
        self.client.call("smartcanvas.create_smartcanvas_element", {"file_id": file_id, "parent_id": page_id,
                         "texts": [{"rich_text": {"text": agreement.strip()}}]}, write=True)
        actual = self.agreement(file_id)
        if actual["text"] != agreement.strip():
            raise HandoffError("agreement_create_unknown", "当前约定未通过回读校验。")

    def agreement(self, file_id):
        if self.require_capabilities() == "blocks":
            return self._block_agreement(file_id)
        page_id, texts = self._agreement_elements(file_id)
        if not page_id or len(texts) != 1 or not _text(texts[0]).strip():
            raise HandoffError("agreement_unreadable", "文档须有唯一且非空的当前约定文本。")
        value = _text(texts[0]).strip()
        return {"text": value, "hash": digest(value), "element_id": texts[0]["id"]}

    def update_agreement(self, file_id, expected_hash, text):
        if not isinstance(text, str) or not text.strip():
            raise HandoffError("invalid_agreement", "当前约定不能留空。")
        current = self.agreement(file_id)
        target = text.strip()
        if current["hash"] == digest(target):
            return current
        if current["hash"] != expected_hash:
            raise HandoffError("agreement_changed", "当前约定已由其他成员修改，请重新阅读。")
        if self.require_capabilities() == "blocks":
            if not current["element_id"]:
                raise HandoffError("agreement_structure_changed", "当前约定已变成多个文档块；请在文档中明确修改后运行 sync-agreement。")
            replacement = target.replace("\r\n", "\n").replace("\n", "\u2028")
            try:
                self.client.call("smartcanvas.edit", {"file_id": file_id, "action": "UPDATE",
                                  "id": current["element_id"], "content": replacement}, write=True)
            except HandoffError as exc:
                if exc.code != "write_unknown":
                    raise
            actual = self.agreement(file_id)
            if actual["text"] != target:
                raise HandoffError("agreement_update_unknown", "当前约定写入结果未确认，请重新读取文档。")
            return actual
        try:
            self.client.call("smartcanvas.update_element", {"file_id": file_id,
                             "updates": [{"element_id": current["element_id"], "text": {"rich_text": {"text": target}}}]}, write=True)
        except HandoffError as exc:
            if exc.code != "write_unknown":
                raise
        actual = self.agreement(file_id)
        if actual["hash"] != digest(target):
            raise HandoffError("agreement_update_unknown", "当前约定写入结果未确认，请重新读取文档。")
        return actual

    def append_once(self, file_id, event_id, actor, message, uncertain=False):
        marker = EVENT_PREFIX + event_id + "]"
        if self._has_event(file_id, marker):
            return {"already_present": True}
        if uncertain:
            raise HandoffError("document_append_unknown", "先前追加的结果仍未在文档中出现；已停止自动重发，避免重复对话。")
        markdown = dialogue_entry(actor, message, event_id)
        try:
            if self.require_capabilities() == "blocks":
                self.client.call("smartcanvas.edit", {"file_id": file_id, "action": "INSERT_AFTER", "id": "",
                                  "content": markdown}, write=True)
            else:
                self.client.call("smartcanvas.append_insert_smartcanvas_by_markdown",
                                 {"file_id": file_id, "markdown": markdown}, write=True)
        except HandoffError as exc:
            if exc.code != "write_unknown":
                raise
        if not self._has_event(file_id, marker):
            raise HandoffError("document_append_unknown", "对话写入结果未确认，请重试时先回读。")
        return {"appended": True}
