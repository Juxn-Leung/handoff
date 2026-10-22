"""Business fields; IDs are always resolved from the remote field catalog."""

STATES = ["待对接", "处理中", "待验收", "已完成", "阻塞"]
CHANGE_TYPES = ["feat", "fix", "change", "remove", "refactor"]


def field(name, kind="text", **config):
    result = {"fieldName": name, "type": kind}
    if config:
        result["config"] = config
    return result


TICKET_FIELDS = [
    field("标题"), field("描述"),
    field("变更类型", "multipleSelect", options=[{"name": x} for x in CHANGE_TYPES]),
    field("发布人", "user", multiple=False),
    field("跟进人", "user", multiple=False), field("交接说明"), field("验收标准"),
    field("状态", "singleSelect", options=[{"name": x} for x in STATES]),
    field("优先级", "singleSelect", options=[{"name": x} for x in ("紧急", "普通", "低")]),
    field("来源"), field("处理结果"), field("结果版本"),
    field("验收人", "user", multiple=False), field("验收时间", "date"),
    field("创建时间", "date"), field("_工单键"), field("_执行标记"),
    field("_需求版本"), field("_结果证据"),
]
LOG_FIELDS = [field("标题"), field("操作者", "user", multiple=False),
              field("时间", "date"), field("内容"), field("_事件键")]


def ticket_links(table_id):
    return [field(x, "unidirectionalLink", linkedTableId=table_id, multiple=True)
            for x in ("关联工单", "前置依赖")]


def log_links(table_id):
    return [field("工单", "unidirectionalLink", linkedTableId=table_id, multiple=False)]


def normalized(value, kind):
    if kind == "singleSelect":
        return value.get("name") if isinstance(value, dict) else value
    if kind == "multipleSelect":
        return sorted(x.get("name") if isinstance(x, dict) else x for x in (value or []))
    if kind in ("unidirectionalLink", "bidirectionalLink"):
        values = value.get("linkedRecordIds", []) if isinstance(value, dict) else (value or [])
        return sorted(x.get("recordId", x.get("id")) if isinstance(x, dict) else x for x in values)
    if kind == "user":
        return sorted(({k: x[k] for k in ("userId", "corpId") if k in x} for x in (value or [])),
                      key=lambda x: (x.get("corpId", ""), x.get("userId", "")))
    if kind == "date":
        if value in (None, ""):
            return None
        from datetime import datetime, timezone
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)
        except ValueError:
            return value
    return "" if value is None and kind == "text" else value
