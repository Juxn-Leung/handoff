"""Tencent Docs SmartSheet storage for the complete handoff workflow."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from urllib.parse import urlparse

from handoff_core import HandoffError, digest, table_time
from table_schema import TICKET_FIELDS, LOG_FIELDS, ticket_links, log_links, normalized

CORP = "tencent-personal"
MEMBER_FIELDS = [
    {"fieldName": "姓名", "type": "text"}, {"fieldName": "账号ID", "type": "text"},
    {"fieldName": "分工", "type": "text"},
    {"fieldName": "加入时间", "type": "date"}, {"fieldName": "_成员键", "type": "text"},
]


def member_fields(roles=None):
    if roles is not None and (not isinstance(roles, list) or not 1 <= len(roles) <= 20
            or any(not isinstance(r, str) or not r.strip() or r != r.strip() or len(r) > 50 for r in roles)
            or len(set(roles)) != len(roles)):
        raise HandoffError("invalid_roles", "分工选项须为 1–20 个不重复的名称，每项不超过 50 字。")
    return [({"fieldName": "分工", "type": "singleSelect", "config": {"options": [{"name": r} for r in roles]}}
             if f["fieldName"] == "分工" and roles else f) for f in MEMBER_FIELDS]


def remote_member_fields(client, base, sheet):
    catalog = collection(client, "smartsheet.list_fields", {"file_id": base, "sheet_id": sheet}, "fields")
    choices = [f for f in catalog if f.get("field_title") == "分工" and f.get("field_type") == "singleSelect"]
    roles = [o["text"] for o in choices[0].get("property_single_select", {}).get("options", [])] if choices else None
    return member_fields(roles)


def identity(client):
    data = client.call("get_user_info", {})
    uid = data.get("user_id")
    if not isinstance(uid, str) or not re.fullmatch(r"[\w:-]{1,128}", uid) or not data.get("nick"):
        raise HandoffError("identity_missing", "腾讯文档未返回稳定账号 ID 和昵称。")
    return {"corp_id": CORP, "user_id": uid, "name": data["nick"], "profile": "tencent:" + uid}


def collection(client, name, args, key):
    """Read every page; missing pagination never means an empty queue."""
    rows, offset, seen = [], 0, set()
    for _ in range(1000):
        result = client.call(name, {**args, "offset": offset, "limit": 100})
        page = result.get(key)
        if not isinstance(page, list):
            raise HandoffError("pagination_incomplete", "腾讯文档未返回完整集合，不能当作没有任务。", collection=key)
        rows.extend(page)
        more, total = result.get("has_more"), result.get("total")
        if more is False or (more is None and isinstance(total, int) and len(rows) >= total):
            if isinstance(total, int) and len(rows) != total:
                raise HandoffError("pagination_changed", "读取期间集合数量变化，请重新扫描。")
            return rows
        nxt = result.get("next")
        if more is not True or not isinstance(nxt, int) or nxt <= offset or nxt in seen:
            raise HandoffError("pagination_incomplete", "腾讯文档分页不完整或游标未前进。")
        seen.add(nxt)
        offset = nxt
    raise HandoffError("pagination_limit", "腾讯文档分页超过上限。")


def specs(expected):
    result = []
    for f in expected:
        name, kind, cfg = f["fieldName"], f["type"], f.get("config", {})
        item = {"field_title": name}
        if kind == "user":
            result.extend([{"field_title": name, "field_type": "text", "property_text": {}},
                           {"field_title": name + "ID", "field_type": "text", "property_text": {}}])
            continue
        if kind in ("singleSelect", "multipleSelect"):
            item["field_type"] = "singleSelect" if kind == "singleSelect" else "select"
            item["property_single_select" if kind == "singleSelect" else "property_select"] = {
                "options": [{"text": o["name"]} for o in cfg.get("options", [])]}
        elif kind == "date":
            item.update(field_type="dateTime", property_date_time={"format": "yyyy-mm-dd hh:mm", "auto_fill": False})
        elif kind == "unidirectionalLink":
            item.update(field_type="reference", property_reference={"sub_id": cfg["linkedTableId"], "is_multiple": cfg.get("multiple", True)})
        elif kind == "text":
            item.update(field_type="text", property_text={})
        else:
            raise HandoffError("unsupported_field", "工单字段类型没有腾讯映射。", field=name)
        result.append(item)
    return result


class TencentTable:
    def __init__(self, client, file_id, sheet_id, expected, people=None):
        self.client, self.base, self.table = client, file_id, sheet_id
        self.expected = {f["fieldName"]: f for f in expected}
        self.fields = {n: {"type": f["type"]} for n, f in self.expected.items()}
        self.people = people or {}

    def call(self, method, args=None, write=False):
        return self.client.call("smartsheet." + method,
                                {"file_id": self.base, "sheet_id": self.table, **(args or {})}, write=write)

    def validate(self, repair=False, checkpoint=None, save=None):
        def catalog():
            rows = collection(self.client, "smartsheet.list_fields", {"file_id": self.base, "sheet_id": self.table}, "fields")
            result = {}
            for f in rows:
                name = f.get("field_title")
                if not name or name in result or not f.get("field_id"):
                    raise HandoffError("invalid_fields", "腾讯工单表字段缺失 ID 或标题重复。")
                result[name] = f
            return result
        actual = catalog()
        missing, wrong = [], []
        for f in specs(list(self.expected.values())):
            found = actual.get(f["field_title"])
            if not found:
                missing.append(f)
            elif found.get("field_type") != f["field_type"]:
                wrong.append(f["field_title"])
            elif f["field_type"] in ("singleSelect", "select"):
                key = "property_single_select" if f["field_type"] == "singleSelect" else "property_select"
                options = {o.get("text") for o in (found.get(key) or {}).get("options", [])}
                if not {o["text"] for o in f[key]["options"]}.issubset(options):
                    wrong.append(f["field_title"])
            elif f["field_type"] == "reference":
                cfg = found.get("property_reference") or {}
                if cfg.get("sub_id") != f["property_reference"]["sub_id"] or cfg.get("is_multiple", False) != f["property_reference"]["is_multiple"]:
                    wrong.append(f["field_title"])
        if wrong or (missing and not repair):
            raise HandoffError("schema_mismatch", "工单表缺少字段或字段类型／选项／关联目标不符；仅显式 repair 可补齐缺失字段。",
                               missing=[f["field_title"] for f in missing], wrong_fields=wrong)
        if missing:
            if checkpoint is not None and checkpoint.get("pending"):
                raise HandoffError("write_unknown", "新增字段结果尚未完整可见，保留检查点，不重复写入。")
            if checkpoint is not None:
                checkpoint["pending"] = True
                save()
            try:
                self.call("add_fields", {"fields": missing}, write=True)
            except HandoffError as exc:
                if checkpoint is not None and exc.details.get("not_executed"):
                    checkpoint["pending"] = False
                    save()
                raise
            # Recursive read validates types, options and references, not merely names.
            self.validate()
        if checkpoint is not None:
            checkpoint["pending"] = False
            save()
        return self

    def _encode(self, values):
        entries = []
        def text(name, value):
            entries.append({"field": name, "text_value": {"items": [{"type": "text", "text": value}]}})
        for name, value in values.items():
            if name not in self.expected:
                raise HandoffError("invalid_fields", "写入包含未定义的工单字段。", field=name)
            kind = self.expected[name]["type"]
            if kind == "user":
                people = value or []
                if not isinstance(people, list) or any(not isinstance(p, dict) or not re.fullmatch(r"[\w:-]{1,128}", p.get("userId", ""))
                        or p.get("corpId", CORP) != CORP for p in people):
                    raise HandoffError("invalid_person", "人员必须使用腾讯个人账号的稳定 ID。")
                if not self.expected[name].get("config", {}).get("multiple", False) and len(people) > 1:
                    raise HandoffError("invalid_person", "此人员字段只能填写一人。")
                ids = sorted({p["userId"] for p in people})
                text(name + "ID", ",".join(ids))
                text(name, "、".join(self.people.get(uid, uid) for uid in ids))
            elif kind == "text":
                if value is not None and not isinstance(value, str):
                    raise HandoffError("invalid_field_value", "文本字段必须是字符串。", field=name)
                text(name, value or "")
            elif kind in ("singleSelect", "multipleSelect"):
                options = [value] if kind == "singleSelect" and value else (value or [])
                allowed = {o["name"] for o in self.expected[name].get("config", {}).get("options", [])}
                if not isinstance(options, list) or any(v not in allowed for v in options):
                    raise HandoffError("invalid_field_value", "选项不在已定义列表。", field=name)
                entries.append({"field": name, "option_value": {"items": [{"text": v} for v in options]}})
            elif kind == "date":
                stamp = normalized(value, "date")
                entries.append({"field": name, "string_value": "" if stamp is None else str(stamp)})
            elif kind == "unidirectionalLink":
                if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
                    raise HandoffError("invalid_field_value", "关联字段必须是真实记录 ID 数组。", field=name)
                entries.append({"field": name, "reference_value": {"items": sorted(set(value))}})
        return entries

    def _decode(self, record):
        if not isinstance(record, dict) or not record.get("record_id") or not isinstance(record.get("field_values"), list):
            raise HandoffError("record_shape", "腾讯文档未返回明确工单 ID 和字段集合。")
        cells = {}
        for c in record["field_values"]:
            n = c.get("field")
            if not n or n in cells:
                raise HandoffError("record_shape", "字段值标题缺失或重复。")
            cells[n] = c
        def texts(name):
            c = cells.get(name, {})
            return "".join(i.get("text", "") for i in (c.get("text_value") or {}).get("items", []))
        row = {"id": record["record_id"]}
        for name, f in self.expected.items():
            kind, c = f["type"], cells.get(name, {})
            if kind == "text":
                row[name] = texts(name)
            elif kind == "user":
                ids = texts(name + "ID").strip()
                users = ids.split(",") if ids else []
                if any(not re.fullmatch(r"[\w:-]{1,128}", u) for u in users) or len(set(users)) != len(users):
                    raise HandoffError("invalid_person", "共享表中的账号 ID 格式错误。", field=name)
                row[name] = [{"userId": uid, "corpId": CORP} for uid in sorted(users)]
            elif kind in ("singleSelect", "multipleSelect"):
                opts = [o.get("text", "") for o in (c.get("option_value") or {}).get("items", [])]
                if kind == "singleSelect" and len(opts) > 1:
                    raise HandoffError("record_shape", "单选字段返回多个选项。")
                row[name] = opts[0] if kind == "singleSelect" and opts else ("" if kind == "singleSelect" else sorted(opts))
            elif kind == "date":
                stamp = c.get("string_value")
                row[name] = datetime.fromtimestamp(int(stamp) / 1000, timezone.utc).isoformat() if stamp else None
            elif kind == "unidirectionalLink":
                row[name] = sorted((c.get("reference_value") or {}).get("items", []))
        return row

    def records(self, ids=None, equals=None, names=None):
        args = {"file_id": self.base, "sheet_id": self.table}
        if ids:
            args["record_ids"] = ids
        # Read all business fields for stable comparisons and ownership checks.
        rows = [self._decode(r) for r in collection(self.client, "smartsheet.list_records", args, "records")]
        if len({r["id"] for r in rows}) != len(rows):
            raise HandoffError("duplicate_record", "腾讯文档返回重复记录 ID。")
        if ids and set(ids) != {r["id"] for r in rows}:
            raise HandoffError("record_missing", "部分请求的工单不存在或无权访问。")
        if equals:
            rows = [r for r in rows if self.matches(r, equals)]
        return rows

    def get(self, rid):
        rows = self.records(ids=[rid])
        if len(rows) != 1:
            raise HandoffError("record_missing", "工单不存在或无权读取。")
        return rows[0]

    def matches(self, row, values):
        return all(normalized(row.get(k), self.expected[k]["type"]) == normalized(v, self.expected[k]["type"]) for k, v in values.items())

    def create_once(self, key_field, key, values, uncertain=False):
        try:
            found = self.records(equals={key_field: key})
        except HandoffError as exc:
            if not uncertain:
                exc.details["not_executed"] = True
            raise
        if len(found) > 1:
            raise HandoffError("duplicate_key", "共享表存在重复业务键，未继续写入。")
        if found:
            return found[0]
        if uncertain:
            raise HandoffError("write_unknown", "上次新增结果未知，先回读，禁止重复创建。")
        values = {**values, key_field: key}
        encoded = self._encode(values)
        try:
            self.call("add_records", {"records": [{"field_values": encoded}]}, write=True)
        except HandoffError as exc:
            if exc.details.get("not_executed"):
                raise
        try:
            rows = self.records(equals={key_field: key})
            if len(rows) == 1 and self.matches(rows[0], values):
                return rows[0]
        except HandoffError:
            pass
        raise HandoffError("write_unknown", "新增已发出但未回读到唯一匹配记录，保留检查点。")

    def update(self, rid, values):
        encoded = self._encode(values)
        error = None
        try:
            self.call("update_records", {"records": [{"record_id": rid, "field_values": encoded}]}, write=True)
        except HandoffError as exc:
            error = exc
        row = self.get(rid)
        if self.matches(row, values):
            return row
        if error:
            raise error
        raise HandoffError("write_unknown", "更新回读不匹配，请用 sync 核对，未重复写入。")


def resolve_file(client, selector):
    if selector.startswith("https://"):
        parsed = urlparse(selector)
        if parsed.hostname != "docs.qq.com" or not parsed.path.startswith("/smartsheet/"):
            raise HandoffError("invalid_table_url", "请提供腾讯文档个人版智能表格链接。")
        doc = parsed.path.split("/")[2]
        # URL ID differs from file_id. Tencent documents the encoded doc ID format.
        import base64
        try:
            file_id = base64.b64decode(doc[1:] + "=" * (-len(doc[1:]) % 4), validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise HandoffError("invalid_table_url", "智能表格链接 ID 无法解析。") from None
    else:
        file_id = selector
    meta = client.call("manage.query_file_info", {"file_id": file_id})
    if meta.get("file_id") != file_id or meta.get("type") != "smartsheet" or not meta.get("url", "").startswith("https://docs.qq.com/smartsheet/"):
        raise HandoffError("invalid_table_url", "未回读到可访问的腾讯智能表格及真实链接。")
    return file_id, meta["url"]


def bind(client, state, who, selector, alias, repair=False, roles=None):
    if roles is not None:
        member_fields(roles)  # Validate before any remote mutation.
    base, url = resolve_file(client, selector)
    if any(b["alias"] == alias and b["base_id"] != base for b in state.project["bindings"]):
        raise HandoffError("alias_exists", "此别名已绑定其他表格。")
    listing = client.call("smartsheet.list_tables", {"file_id": base}).get("sheets")
    if not isinstance(listing, list):
        raise HandoffError("table_shape", "腾讯文档未返回工作表集合。")
    selected, created_tables = {}, set()
    for name in ("工单", "跟进记录", "成员"):
        matches = [r for r in listing if r.get("title") == name]
        if len(matches) > 1:
            raise HandoffError("table_ambiguity", "存在同名工作表，请先消歧。", table=name)
        if not matches:
            if not repair:
                raise HandoffError("missing_remote_tables", "缺少工单、跟进记录或成员表；显式 --repair 可补齐。", table=name)
            checkpoint = state.data.setdefault("tencent_table_creations", {}).setdefault(base + ":" + name, {})
            if checkpoint.get("pending"):
                raise HandoffError("create_unknown", "上次新增工作表结果未知且尚不可见，禁止重复创建。", table=name)
            checkpoint["pending"] = True
            state.save()
            try:
                client.call("smartsheet.add_table", {"file_id": base, "properties": {"title": name}}, write=True)
            except HandoffError as exc:
                if exc.details.get("not_executed"):
                    checkpoint["pending"] = False
                    state.save()
                raise
            listing = client.call("smartsheet.list_tables", {"file_id": base}).get("sheets", [])
            matches = [r for r in listing if r.get("title") == name]
            if len(matches) != 1:
                raise HandoffError("create_unknown", "新增工作表后未回读到唯一标题。", table=name)
            created_tables.add(matches[0]["sheet_id"])
        if not matches[0].get("sheet_id"):
            raise HandoffError("table_shape", "工作表没有稳定 ID。")
        selected[name] = matches[0]["sheet_id"]
        state.data.setdefault("tencent_table_creations", {}).setdefault(base + ":" + name, {})["pending"] = False
        state.save()
    definitions = {"工单": TICKET_FIELDS + ticket_links(selected["工单"]),
                   "跟进记录": LOG_FIELDS + log_links(selected["工单"]),
                   "成员": member_fields(roles) if roles is not None else remote_member_fields(client, base, selected["成员"])}
    for name, expected in definitions.items():
        if selected[name] in created_tables:
            catalog = collection(client, "smartsheet.list_fields", {"file_id": base, "sheet_id": selected[name]}, "fields")
            title = "姓名" if name == "成员" else "标题"
            defaults = [f for f in catalog if f.get("field_title") == "智能表列" and f.get("field_type") == "text"]
            if not any(f.get("field_title") == title for f in catalog) and len(defaults) == 1:
                # Reuse Tencent's empty default text column rather than adding a spare header.
                client.call("smartsheet.update_fields", {"file_id": base, "sheet_id": selected[name], "fields": [{
                    "field_id": defaults[0]["field_id"], "field_title": title, "field_type": "text", "property_text": {}}]}, write=True)
        checkpoint = state.data.setdefault("tencent_field_creations", {}).setdefault(base + ":" + selected[name], {})
        TencentTable(client, base, selected[name], expected).validate(repair, checkpoint, state.save)
    binding = {"provider": "tencent", "alias": alias, "url": url, "base_id": base, "table_id": selected["工单"],
               "log_table_id": selected["跟进记录"], "member_table_id": selected["成员"], "corp_id": CORP}
    state.project["bindings"] = [b for b in state.project["bindings"] if b.get("base_id") != base] + [binding]
    state.save_project()
    return binding


def create(client, state, who, name, alias, roles=None):
    member_fields(roles)
    if any(b["alias"] == alias for b in state.project["bindings"]):
        raise HandoffError("alias_exists", "此别名已绑定表格，请使用已有表。")
    key = digest({"user": who["user_id"], "name": name, "alias": alias})
    checkpoint = state.data.setdefault("tencent_creations", {}).setdefault(key, {})
    if "roles" in checkpoint and checkpoint["roles"] != roles:
        raise HandoffError("create_changed", "建表恢复时分工选项发生变化，请使用原选项。")
    checkpoint["roles"] = roles
    state.save()
    if not checkpoint.get("file_id"):
        if checkpoint.get("pending"):
            raise HandoffError("create_unknown", "上次建表结果未知，请在腾讯文档找到原表后 bind --repair，不重复创建。")
        checkpoint["pending"] = True
        state.save()
        try:
            result = client.call("manage.create_file", {"title": name, "file_type": "smartsheet"}, write=True)
        except HandoffError as exc:
            if exc.details.get("not_executed"):
                checkpoint["pending"] = False
                state.save()
            raise
        if not result.get("file_id"):
            raise HandoffError("create_unknown", "建表未返回文档 ID，禁止直接重建。")
        checkpoint.update(file_id=result["file_id"], pending=False)
        state.save()
    result = bind(client, state, who, checkpoint["file_id"], alias, repair=True, roles=roles)
    from tencent_layout import clean_layout
    try:
        layout = clean_layout(client, result)
    except HandoffError as exc:
        layout = {"pending": True, "error": exc.code,
                  "next": "结构已创建，但默认表头尚未整理。可运行 layout 重试，或在腾讯文档字段显隐面板隐藏内部列。"}
    return {"created": result, "layout": layout,
            "sharing": "请在腾讯文档分享面板添加指定同事为可编辑；不会自动改成所有人可编辑。"}


def tables(client, binding, who):
    members = TencentTable(client, binding["base_id"], binding["member_table_id"],
                           remote_member_fields(client, binding["base_id"], binding["member_table_id"])).validate()
    people = {r["账号ID"]: r["姓名"] for r in members.records() if r.get("账号ID")}
    people[who["user_id"]] = who["name"]
    tickets = TencentTable(client, binding["base_id"], binding["table_id"], TICKET_FIELDS + ticket_links(binding["table_id"]), people).validate()
    logs = TencentTable(client, binding["base_id"], binding["log_table_id"], LOG_FIELDS + log_links(binding["table_id"]), people).validate()
    return tickets, logs, members


def join(members, state, who, role=None):
    key = "tencent:" + who["user_id"]
    found = members.records(equals={"_成员键": key})
    if len(found) > 1:
        raise HandoffError("duplicate_member", "成员表存在重复账号，需消歧。")
    if found and found[0].get("账号ID") != who["user_id"]:
        raise HandoffError("member_changed", "成员键与账号 ID 不符。")
    role = role if role is not None else (found[0].get("分工", "") if found else "")
    spec = members.expected["分工"]
    if spec["type"] == "singleSelect" and role not in [o["name"] for o in spec["config"]["options"]]:
        raise HandoffError("choose_role", "请从此表的分工选项中选择；分工不限制领取权限。",
                           choices=[o["name"] for o in spec["config"]["options"]])
    values = {"姓名": who["name"], "账号ID": who["user_id"], "分工": role}
    if found:
        row = members.update(found[0]["id"], values)
    else:
        checkpoint = state.data.setdefault("member_joins", {}).setdefault(members.base + ":" + key, {})
        uncertain = checkpoint.get("pending", False)
        checkpoint["pending"] = True
        state.save()
        try:
            row = members.create_once("_成员键", key, {**values, "加入时间": table_time()}, uncertain=uncertain)
        except HandoffError as exc:
            if exc.details.get("not_executed"):
                checkpoint["pending"] = False
                state.save()
            raise
        checkpoint["pending"] = False
    state.data["identities"][who["profile"]] = {"role": role}
    state.save()
    return {"joined": row["id"], "identity": who, "role": role}
