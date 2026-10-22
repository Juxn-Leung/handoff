"""Keep technical evidence available without cluttering the business view."""
from __future__ import annotations

import json
from handoff_core import HandoffError
from tencent_tables import collection

LAYOUTS = {
    "工单": ("table_id", ["标题", "状态", "优先级", "发布人", "跟进人", "交接说明", "验收标准", "前置依赖", "处理结果"]),
    "跟进记录": ("log_table_id", ["工单", "操作者", "时间", "内容"]),
    "成员": ("member_table_id", ["姓名", "分工"]),
}


def clean_layout(client, binding):
    result = []
    for title, (key, visible) in LAYOUTS.items():
        args = {"file_id": binding["base_id"], "sheet_id": binding[key]}
        fields = collection(client, "smartsheet.list_fields", args, "fields")
        names = {f["field_title"] for f in fields}
        if not set(visible).issubset(names):
            raise HandoffError("layout_missing_fields", "请先 bind --repair 补齐必要业务字段。", table=title)
        views = collection(client, "smartsheet.list_views", args, "views")
        selected = [v for v in views if v.get("view_type") == "grid" and v.get("view_name") in ("默认视图", title)]
        if len(selected) != 1:
            raise HandoffError("layout_view_ambiguity", "未找到唯一的默认工单视图，保留自定义视图。", table=title)
        view_id = selected[0]["view_id"]
        patch = {"fields": [{"field_id": f["field_id"], "hidden": f["field_title"] not in visible} for f in fields]}
        # Setting visibility is idempotent; no records, fields, filters or sharing are deleted.
        client.call("smartsheet.update_view", {**args, "view_id": view_id,
                    "patch_json": json.dumps(patch, ensure_ascii=False)}, write=True)
        after = collection(client, "smartsheet.list_views", args, "views")
        matching = [v for v in after if v.get("view_id") == view_id]
        if len(matching) != 1:
            raise HandoffError("layout_unverified", "整理后未回读到原视图。", table=title)
        config = json.loads(matching[0]["config"])
        metas = config.get("fieldMetas", {})
        shown = {f["field_title"] for f in fields if f["field_id"] in config.get("fieldIds", [])
                 and not metas.get(f["field_id"], {}).get("hidden", False)}
        if shown != set(visible):
            raise HandoffError("layout_unverified", "表头显隐未通过回读校验。", table=title, visible=sorted(shown))
        result.append({"table": title, "visible": visible,
                       "url": binding["url"] + "?tab=" + binding[key] + "&viewId=" + view_id})
    return {"layouts": result, "records_preserved": True}
