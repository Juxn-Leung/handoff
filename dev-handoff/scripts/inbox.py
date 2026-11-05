"""Read personal handoff updates only when explicitly invoked; no background work."""
from __future__ import annotations

from handoff_core import HandoffError, digest, now, user_ids
from delivery import contract


def roles(row, uid):
    result = []
    if uid in user_ids(row.get("发布人")):
        result.append("发布方")
    if uid in user_ids(row.get("跟进人")):
        result.append("跟进人")
    if (contract(row) or {}).get("reviewer_id") == uid:
        result.append("验收人")
    return result


def read(flow, record_id=None, limit=20, offset=0):
    """Observation cursors are local hints, never shared receipts or completion claims."""
    if not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
        raise HandoffError("invalid_page", "inbox 的 limit 须为 1–100，offset 不得小于 0。")
    rows = flow.tickets.records()
    uid = flow.identity["user_id"]
    scope = digest([flow.namespace, flow.identity["profile"]])
    prior = flow.state.data.get("inbox_observations", {}).get(scope, {})
    if record_id:
        selected = [r for r in rows if r["id"] == record_id]
        if not selected:
            raise HandoffError("record_missing", "工单不存在或无权读取。")
    else:
        # A specifically opened topic remains followed in this workspace.
        selected = [r for r in rows if roles(r, uid) or r["id"] in prior]
    selected.sort(key=lambda r: (r.get("状态") == "已完成", str(r.get("创建时间") or ""), r["id"]))
    items, observed = [], dict(prior)
    for row in selected[offset:offset + limit]:
        rid = row["id"]
        item = {"id": rid, "title": row.get("标题"), "status": row.get("状态"),
                "my_roles": roles(row, uid), "owner_ids": sorted(user_ids(row.get("跟进人"))),
                "publisher_ids": sorted(user_ids(row.get("发布人"))), "next": row.get("下一步"),
                "document": row.get("文档")}
        try:
            body = ""
            if row.get("_文档ID"):
                if flow.documents is None:
                    raise HandoffError("document_unavailable", "无法读取此事项的对话文档。")
                body = flow.documents.content(row["_文档ID"])
            # Detect changes during this read instead of acknowledging a mixed snapshot.
            if digest(flow.tickets.get(rid)) != digest(row):
                raise HandoffError("remote_changed", "读取期间事项已变化，请重新查看。")
            snapshot = digest({"record": row, "conversation": body})
            previous = prior.get(rid, {})
            item.update(update="first_read" if not previous else "changed" if previous["snapshot"] != snapshot else "unchanged",
                        last_observed_at=previous.get("observed_at"),
                        conversation_tail=body[-1600:], conversation_truncated=len(body) > 1600,
                        needs_document_migration=bool(flow.binding.get("group_id") and not row.get("_文档ID")))
            observed[rid] = {"snapshot": snapshot, "observed_at": now()}
        except HandoffError as exc:
            item.update(update="unreadable", error={"code": exc.code, "message": exc.message})
        items.append(item)
    flow.state.data.setdefault("inbox_observations", {})[scope] = observed
    flow.state.save()
    remaining = max(0, len(selected) - offset - limit)
    return {"items": items, "page": {"offset": offset, "limit": limit, "shown": len(items),
                                      "total": len(selected), "remaining": remaining,
                                      "next_offset": offset + limit if remaining else None},
            "unreadable": sum(item["update"] == "unreadable" for item in items),
            "remote_modified": False, "background_started": False,
            "next": "更新标记仅与本工作区上次读取比较，不代表对方已读或本事项已处理。需要回应或执行时先 show 阅读完整约定与对话；没有变化也须检查尚未完成的责任。"}
