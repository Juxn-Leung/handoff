"""Ticket orchestration, durable operation journal and review invariants."""
from __future__ import annotations

import json
import uuid
from handoff_core import HandoffError, digest, now, table_time, owned, plan_queue, requirement, user_ids
from table_schema import CHANGE_TYPES

QUEUE_FIELDS = ["标题", "跟进人", "状态", "优先级", "关联工单", "前置依赖", "创建时间", "处理结果", "结果版本"]


class Workflow:
    def __init__(self, git, state, identity, binding, tickets, logs):
        self.git, self.state, self.identity = git, state, identity
        self.binding, self.tickets, self.logs = binding, tickets, logs
        if binding["corp_id"] != identity["corp_id"]:
            raise HandoffError("organization_mismatch", "工单表绑定的组织与当前账号不同。")
        self.namespace = binding["base_id"] + ":" + binding["table_id"]
        self.person = [{"userId": identity["user_id"], "corpId": identity["corp_id"]}]

    def key(self, rid):
        return self.namespace + ":" + rid

    def session(self, rid):
        session = self.state.data["sessions"].get(self.key(rid))
        if not session or session["profile"] != self.identity["profile"]:
            raise HandoffError("session_missing", "当前账号在这个工作区没有此工单的处理记录，请勿直接改写状态。")
        return session

    def check_owner(self, row):
        if not owned(row, self.identity):
            raise HandoffError("owner_changed", "工单跟进人不是当前账号，或跟进人不唯一。")

    def queue(self):
        records = self.tickets.records(names=QUEUE_FIELDS)
        result = plan_queue(records, self.identity, self.state.data["last"].get(self.namespace))
        for group in ("ready", "pending_acceptance", "blocked", "in_progress"):
            for row in result[group]:
                # Internal evidence is only needed during review, not in the queue.
                row.pop("_结果证据", None)
                row.pop("_执行标记", None)
        return result

    def claimable(self):
        rows = self.tickets.records()
        offered, hypothetical = {}, []
        for row in rows:
            if not row.get("跟进人") and row.get("状态") == "待对接" and not row.get("_执行标记"):
                offered[row["id"]] = row
                hypothetical.append({**row, "跟进人": self.person})
            else:
                hypothetical.append(row)
        queue = plan_queue(hypothetical, self.identity, self.state.data["last"].get(self.namespace))
        return [offered[r["id"]] for r in queue["ready"] if r["id"] in offered]

    def claim(self, rid, expected):
        self._guard_clean()
        row = self.tickets.get(rid)
        if row.get("跟进人") == self.person:
            return {"claimed": rid, "already_owned": True}
        if requirement(row) != expected:
            raise HandoffError("remote_changed", "领取前需求版本发生变化，请重新阅读。")
        if rid not in {r["id"] for r in self.claimable()}:
            raise HandoffError("not_claimable", "工单已有跟进人、依赖未满足或正在处理，不能领取。")
        self._apply(row, {"跟进人": self.person}, "领取工单", "由当前个人账号领取；开始编辑前仍需声明文件范围并 start。")
        return {"claimed": rid, "next": "阅读工单、声明计划文件并 start 后再编辑。"}

    def assign(self, rid, target, expected):
        self._guard_clean()
        row = self.tickets.get(rid)
        if requirement(row) != expected or row.get("状态") != "待对接" or row.get("_执行标记"):
            raise HandoffError("remote_changed", "只能分配需求版本未变且尚未开始的工单。")
        if self.identity["user_id"] not in user_ids(row.get("发布人")):
            raise HandoffError("not_publisher", "只有工单中记录的发布方账号可以分配。")
        person = [{"userId": target["user_id"], "corpId": self.identity["corp_id"]}]
        if row.get("跟进人") and row["跟进人"] != person:
            raise HandoffError("owner_changed", "工单已分配给其他账号，不能覆盖。")
        if not row.get("跟进人"):
            self._apply(row, {"跟进人": person}, "分配工单", "分配给已登记成员账号：" + target["user_id"])
        return {"assigned": rid, "user_id": target["user_id"]}

    def _pending_operations(self):
        return [op for op in self.state.data["operations"].values()
                if op["namespace"] == self.namespace and op["phase"] not in ("done", "abandoned")]

    def _guard_clean(self):
        pending = self._pending_operations()
        if pending:
            raise HandoffError("sync_required", "存在尚未确认的表格写入，请先执行 sync。", operation_ids=[p["id"] for p in pending])

    def _finish_operation(self, op):
        self.logs.create_once("_事件键", op["id"], {
            "标题": op["title"], "工单": [op["record_id"]], "操作者": op["person"],
            "时间": op["time"], "内容": op["message"],
        }, uncertain=op.get("log_uncertain", False))
        if op.get("session"):
            self.state.data["sessions"][self.key(op["record_id"])] = op["session"]
        if op.get("set_last"):
            self.state.data["last"][self.namespace] = op["record_id"]
        op["phase"] = "done"
        self.state.save()

    def _apply(self, row, patch, title, message, session=None, set_last=False):
        self._guard_clean()
        fresh = self.tickets.get(row["id"])
        if digest(fresh) != digest(row):
            raise HandoffError("remote_changed", "工单在读取后被修改，请重新查看后再操作。")
        op = {"id": str(uuid.uuid4()), "namespace": self.namespace, "record_id": row["id"],
              "before": row, "patch": patch, "title": title, "message": message,
              "person": self.person, "profile": self.identity["profile"], "time": table_time(),
              "phase": "prepared", "session": session, "set_last": set_last}
        self.state.data["operations"][op["id"]] = op
        self.state.save()
        updated = self.tickets.update(row["id"], patch)
        if requirement(updated) != requirement(row) or updated.get("跟进人") != patch.get("跟进人", row.get("跟进人")):
            raise HandoffError("remote_changed", "写入期间需求或跟进人发生变化，必须人工复核。")
        op["phase"] = "record_verified"
        self.state.save()
        try:
            self._finish_operation(op)
        except HandoffError as exc:
            op["log_uncertain"] = not exc.details.get("not_executed", False)
            self.state.save()
            raise
        return updated

    def sync(self):
        results = []
        for op in self._pending_operations():
            if op["profile"] != self.identity["profile"]:
                raise HandoffError("identity_mismatch", "待恢复写入属于另一个账号。")
            row = self.tickets.get(op["record_id"])
            owners = (op["before"].get("跟进人"), op["patch"].get("跟进人", op["before"].get("跟进人")))
            if requirement(row) != requirement(op["before"]) or row.get("跟进人") not in owners:
                raise HandoffError("remote_changed", "恢复时发现需求或负责人已变化，未覆盖远端内容。")
            if not self.tickets.matches(row, op["patch"]):
                if digest(row) != digest(op["before"]):
                    raise HandoffError("remote_changed", "恢复时远端状态与操作前后均不一致。")
                # Explicit sync may retry an idempotent patch only against unchanged input.
                if op["patch"].get("状态") == "已完成":
                    self._check_review(op["before"], op["before"].get("结果版本"))
                self.tickets.update(op["record_id"], op["patch"])
            if op["patch"].get("状态") == "已完成":
                # Even a successful-but-unlogged acceptance cannot be finalized using stale code.
                self._check_evidence(row)
            op["phase"] = "record_verified"
            self.state.save()
            try:
                self._finish_operation(op)
            except HandoffError as exc:
                op["log_uncertain"] = not exc.details.get("not_executed", False)
                self.state.save()
                raise
            results.append({"operation": op["id"], "record_id": op["record_id"], "verified": True})
        return results

    def abandon_operation(self, operation_id, reason):
        """Explicitly abandon only the local recovery attempt, never undo remote data."""
        op = self.state.data["operations"].get(operation_id)
        if not op or op["namespace"] != self.namespace or op["profile"] != self.identity["profile"]:
            raise HandoffError("operation_missing", "找不到当前账号、当前表格的恢复操作。")
        if op["phase"] in ("done", "abandoned") or not reason.strip():
            raise HandoffError("invalid_abandon", "只能放弃尚未确认的恢复操作，并必须记录操作者给出的原因。")
        remote = self.tickets.get(op["record_id"])
        op.update(phase="abandoned", abandoned_at=now(), reason=reason,
                  remote_state_at_abandon=remote.get("状态"))
        local = self.state.data["sessions"].get(self.key(op["record_id"]))
        if local:
            local["phase"] = "needs_manual_recovery"
        self.state.save()
        return {"abandoned_local_operation": operation_id, "record_id": op["record_id"],
                "remote_state": remote.get("状态"), "remote_modified": False,
                "next": "保留全部检查点；原工单仍须人工核实，不代表已验收、撤销或恢复。"}

    def publish(self, manifest):
        self._guard_clean()
        snapshot = self.git.staged()
        if manifest.get("snapshot") != snapshot["fingerprint"]:
            raise HandoffError("staged_changed", "暂存区与整理工单时的快照不同，请重新 capture。")
        drafts = manifest.get("tickets")
        if not isinstance(drafts, list) or not drafts:
            raise HandoffError("invalid_draft", "发布文件需要非空 tickets 数组。")
        keys, prepared, covered = set(), [], set()
        for draft in drafts:
            if not isinstance(draft, dict):
                raise HandoffError("invalid_draft", "每条工单必须是 JSON 对象。")
            key = draft.get("key")
            if not isinstance(key, str) or not key.strip() or key in keys:
                raise HandoffError("invalid_draft", "每个独立事项需要唯一且稳定的 key。")
            keys.add(key)
            for name in ("title", "description", "handoff", "acceptance"):
                if not isinstance(draft.get(name), str) or not draft[name].strip():
                    raise HandoffError("invalid_draft", f"工单缺少非空 {name}。", key=key)
            types = draft.get("change_types")
            if not isinstance(types, list) or not types or not set(types).issubset(CHANGE_TYPES):
                raise HandoffError("invalid_draft", "变更类型必须使用 feat/fix/change/remove/refactor。", key=key)
            if draft.get("critical_unknowns"):
                raise HandoffError("needs_clarification", "存在影响接口或验收的关键待确认项，暂不发布。", key=key, questions=draft["critical_unknowns"])
            paths = self.git.clean_paths(draft.get("paths") or [])
            if not paths or not set(paths).issubset(snapshot["paths"]):
                raise HandoffError("invalid_scope", "工单路径必须来自当前暂存区。", key=key)
            covered.update(paths)
            # New hard dependencies must be explicit existing record IDs; inferred links are suggestions in handoff text.
            for link in ("related", "dependencies"):
                if not isinstance(draft.get(link, []), list) or any(not isinstance(x, str) or not x for x in draft.get(link, [])):
                    raise HandoffError("invalid_draft", "关联和依赖必须是同表真实 recordId 数组。")
                for rid in draft.get(link, []):
                    self.tickets.get(rid)
            priority = draft.get("priority", "普通")
            if priority not in ("紧急", "普通", "低"):
                raise HandoffError("invalid_draft", "优先级必须是紧急、普通或低。")
            values = {"标题": draft["title"], "描述": draft["description"], "变更类型": types,
                      "发布人": self.person, "交接说明": draft["handoff"], "验收标准": draft["acceptance"],
                      "状态": "待对接", "优先级": priority, "跟进人": [],
                      "关联工单": draft.get("related", []), "前置依赖": draft.get("dependencies", []),
                      "来源": json.dumps({**snapshot, "paths": paths, "repository": self.git.root.name}, ensure_ascii=False),
                      "创建时间": table_time()}
            prepared.append((key, values))
        ignored = set(manifest.get("excluded_paths", []))
        if covered | ignored != set(snapshot["paths"]) or ignored & covered:
            raise HandoffError("incomplete_scope", "每个暂存文件必须属于某条工单或明确列为 excluded_paths。")
        if ignored and not manifest.get("exclusion_reason"):
            raise HandoffError("invalid_draft", "排除暂存文件必须说明原因。")
        batch_key = self.namespace + ":" + snapshot["fingerprint"]
        batches = self.state.data["batches"]
        batch = batches.setdefault(batch_key, {"manifest_hash": digest(manifest), "items": {}})
        if batch["manifest_hash"] != digest(manifest):
            raise HandoffError("draft_changed", "同一快照已有发布检查点；请使用原草稿恢复，避免重复拆单。")
        output = []
        for key, values in prepared:
            item = batch["items"].setdefault(key, {"phase": "new"})
            remote_key = digest({"snapshot": snapshot["fingerprint"], "key": key})
            uncertain = item["phase"] == "unknown"
            if item["phase"] == "verified":
                row = self.tickets.get(item["record_id"])
                if row.get("_工单键") != remote_key:
                    raise HandoffError("remote_changed", "已发布工单的标识发生变化。")
            else:
                item["phase"] = "unknown"
                self.state.save()
                try:
                    row = self.tickets.create_once("_工单键", remote_key, values, uncertain=uncertain)
                except HandoffError as exc:
                    if exc.details.get("not_executed"):
                        item["phase"] = "new"
                        self.state.save()
                    raise
                item.update(phase="verified", record_id=row["id"])
                self.state.save()
            output.append({"id": row["id"], "title": row.get("标题"), "key": key})
        return {"published": output, "table_url": self.binding["url"], "next": "由已登记成员本人 claim 领取，或由发布方 assign 给已登记成员；文档编辑权限另行管理。"}

    def _scope_conflicts(self, rid, paths):
        conflicts = []
        # Local code is shared even when the user switches to a different bound table.
        for key, session in self.state.data["sessions"].items():
            if key == self.key(rid) or session.get("phase") not in ("working", "pending_acceptance", "blocked"):
                continue
            overlap = sorted(set(paths) & set(session.get("paths", [])))
            if overlap:
                conflicts.append({"id": session["record_id"], "table_namespace": key.rsplit(":", 1)[0],
                                  "state": session["phase"], "overlap": overlap})
        for row in self.tickets.records(names=QUEUE_FIELDS + ["_结果证据"]):
            if row["id"] == rid or row.get("状态") not in ("待验收", "处理中"):
                continue
            evidence = json.loads(row.get("_结果证据") or "{}")
            local = self.state.data["sessions"].get(self.key(row["id"]), {})
            if evidence.get("workspace") == digest(str(self.git.root)) or local:
                protected = set(evidence.get("code", {})) | set(local.get("paths", []))
                overlap = sorted(set(paths) & protected)
                if overlap and not any(c["id"] == row["id"] and c.get("table_namespace") == self.namespace for c in conflicts):
                    conflicts.append({"id": row["id"], "table_namespace": self.namespace,
                                      "state": row.get("状态"), "overlap": overlap})
        if conflicts:
            raise HandoffError("pending_code_conflict", "下一条事项可能影响尚未验收的代码，已暂停；请先验收或改选独立事项。", conflicts=conflicts)

    def start(self, rid, paths, expected):
        self._guard_clean()
        row = self.tickets.get(rid)
        self.check_owner(row)
        if row.get("状态") != "待对接" or row.get("_执行标记"):
            raise HandoffError("already_started", "工单不是未领取的待对接事项，不能覆盖其他会话。")
        if requirement(row) != expected:
            raise HandoffError("remote_changed", "需求版本已变，请重新阅读工单。")
        queue = plan_queue(self.tickets.records(names=QUEUE_FIELDS), self.identity)
        if rid not in {x["id"] for x in queue["ready"]}:
            raise HandoffError("dependency_blocked", "工单前置依赖未完成或存在循环依赖。")
        paths = self.git.clean_paths(paths)
        if not paths:
            raise HandoffError("invalid_scope", "开始前必须声明计划修改的文件路径。")
        self._scope_conflicts(rid, paths)
        active = [s for s in self.state.data["sessions"].values() if s.get("phase") == "working"]
        if active:
            raise HandoffError("active_task", "当前工作区仍有一条正在处理的工单，请先完成或记录阻塞。")
        session = {"record_id": rid, "profile": self.identity["profile"], "phase": "working",
                   "paths": paths, "requirement": expected, "token": str(uuid.uuid4()),
                   "baseline": self.git.baseline(), "started_at": now()}
        self._apply(row, {"状态": "处理中", "_执行标记": session["token"], "_需求版本": expected},
                    "开始处理", "已阅读需求并确认当前仓库能够承接。计划文件：" + ", ".join(paths), session=session)
        return {"started": rid, "paths": paths, "next": "只处理此工单；修改范围扩大时先 extend，再编辑。"}

    def _working(self, rid):
        row, session = self.tickets.get(rid), self.session(rid)
        self.check_owner(row)
        if row.get("状态") != "处理中" or session.get("phase") != "working" or row.get("_执行标记") != session["token"]:
            raise HandoffError("session_conflict", "远端状态或执行会话已变化，未继续写入。")
        if requirement(row) != session["requirement"]:
            raise HandoffError("requirements_changed", "处理期间需求已修改，请重新确认并记录阻塞。")
        return row, session

    def extend(self, rid, paths):
        self._guard_clean()
        _, session = self._working(rid)
        paths = self.git.clean_paths(paths)
        self._scope_conflicts(rid, paths)
        session["paths"] = sorted(set(session["paths"]) | set(paths))
        self.state.save()
        return {"record_id": rid, "paths": session["paths"]}

    def ready(self, rid, result, revalidate=False):
        self._guard_clean()
        if revalidate:
            row, session = self.tickets.get(rid), self.session(rid)
            self.check_owner(row)
            if row.get("状态") != "待验收" or row.get("_执行标记") != session["token"]:
                raise HandoffError("session_conflict", "只能重新验证本会话提交的待验收结果。")
            if requirement(row) != session["requirement"]:
                raise HandoffError("requirements_changed", "需求变化后必须退回重新处理。")
        else:
            row, session = self._working(rid)
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            raise HandoffError("invalid_result", "处理结果需要非空 summary。")
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks:
            raise HandoffError("invalid_result", "必须提供验证记录；无法执行的验证需说明原因。")
        for check in checks:
            if not isinstance(check, dict) or not check.get("command") or check.get("outcome") not in ("passed", "not_run"):
                raise HandoffError("checks_failed", "失败验证不能提交待验收；请修复或记录阻塞。")
            if check["outcome"] == "not_run" and not check.get("reason"):
                raise HandoffError("invalid_result", "未执行验证需要给出原因。")
        if not revalidate:
            current = self.git.baseline()
            changed = {p for p in current.keys() | session["baseline"].keys() if current.get(p) != session["baseline"].get(p)}
            extra = changed - set(session["paths"])
            if extra:
                raise HandoffError("scope_changed", "检测到声明范围外的改动，需先核对影响并更新范围。", paths=sorted(extra))
        self._scope_conflicts(rid, session["paths"])
        evidence = {"workspace": digest(str(self.git.root)), "requirement": requirement(row),
                    "code": self.git.code(session["paths"]), "summary": result["summary"],
                    "checks": checks, "verified_at": now()}
        version = digest({k: v for k, v in evidence.items() if k != "verified_at"})
        session = {**session, "phase": "pending_acceptance", "result_version": version}
        patch = {"状态": "待验收", "处理结果": result["summary"] + "\n\n验证：\n" + json.dumps(checks, ensure_ascii=False, indent=2),
                 "结果版本": version, "_结果证据": json.dumps(evidence, ensure_ascii=False)}
        self._apply(row, patch, "提交待验收", "结果版本：" + version + "\n" + patch["处理结果"], session=session, set_last=True)
        return {"pending_acceptance": rid, "result_version": version, "stop": True,
                "queue": self.queue(), "next": "等待操作者验收；只有明确说继续才可处理下一条。"}

    def block(self, rid, reason, attempted, needed):
        self._guard_clean()
        row, session = self.tickets.get(rid), self.session(rid)
        self.check_owner(row)
        if row.get("状态") != "处理中" or row.get("_执行标记") != session["token"]:
            raise HandoffError("session_conflict", "不能阻塞其他会话正在处理的事项。")
        if not all(isinstance(x, str) and x.strip() for x in (reason, attempted, needed)):
            raise HandoffError("invalid_note", "阻塞记录需包含原因、已尝试方法和所需信息。")
        message = f"原因：{reason}\n已尝试：{attempted}\n所需信息：{needed}"
        self._apply(row, {"状态": "阻塞", "处理结果": message}, "处理阻塞", message,
                    session={**session, "phase": "blocked"}, set_last=True)
        return {"blocked": rid, "stop": True, "queue": self.queue()}

    def _check_evidence(self, row):
        try:
            evidence = json.loads(row.get("_结果证据") or "{}")
        except ValueError as exc:
            raise HandoffError("invalid_evidence", "结果证据不是有效 JSON。") from exc
        if evidence.get("workspace") != digest(str(self.git.root)):
            raise HandoffError("wrong_workspace", "此结果来自另一个工作区，请在原工作区验收。")
        if evidence.get("requirement") != requirement(row):
            raise HandoffError("requirements_changed", "需求已变化，旧结果必须重新处理和验收。")
        if not evidence.get("code") or self.git.code(evidence["code"].keys()) != evidence["code"]:
            raise HandoffError("code_changed", "待验收代码已变化，请重新验证并取得新结果版本。")
        expected = digest({k: v for k, v in evidence.items() if k != "verified_at"})
        if expected != row.get("结果版本"):
            raise HandoffError("invalid_evidence", "结果版本与验证证据不匹配。")
        return evidence

    def _check_review(self, row, confirmed_version):
        self.check_owner(row)
        if row.get("状态") != "待验收":
            raise HandoffError("not_pending_acceptance", "工单不是待验收状态。")
        if row.get("结果版本") != confirmed_version:
            raise HandoffError("review_version_changed", "操作者验收的结果版本与当前版本不同。")
        self._check_evidence(row)
        for rid in row.get("前置依赖") or []:
            if self.tickets.get(rid).get("状态") != "已完成":
                raise HandoffError("dependency_blocked", "前置依赖已退回或未完成，不能验收。", dependency=rid)

    def accept(self, rid, confirmed_version):
        self._guard_clean()
        row = self.tickets.get(rid)
        self._check_review(row, confirmed_version)
        session = {**self.session(rid), "phase": "completed"}
        self._apply(row, {"状态": "已完成", "验收人": self.person, "验收时间": table_time(), "_执行标记": ""},
                    "验收通过", "操作者明确通过结果版本：" + confirmed_version, session=session)
        return {"accepted": rid, "result_version": confirmed_version, "stop": True}

    def reject(self, rid, reason):
        self._guard_clean()
        row = self.tickets.get(rid)
        self.check_owner(row)
        if row.get("状态") != "待验收" or not reason.strip():
            raise HandoffError("invalid_rejection", "只有待验收工单可以退回，且必须填写原因。")
        affected = [r["id"] for r in self.tickets.records(names=["关联工单", "前置依赖"]) if rid in (r.get("前置依赖") or []) or rid in (r.get("关联工单") or [])]
        session = self.state.data["sessions"].get(self.key(rid))
        if session:
            session = {**session, "phase": "returned"}
        self._apply(row, {"状态": "待对接", "_执行标记": "", "验收人": [], "验收时间": None},
                    "验收退回", reason + "\n受影响工单：" + ", ".join(affected), session=session)
        return {"returned": rid, "affected": affected, "stop": True}

    def resume(self, rid, reason):
        self._guard_clean()
        row = self.tickets.get(rid)
        self.check_owner(row)
        if row.get("状态") != "阻塞" or not reason.strip():
            raise HandoffError("invalid_resume", "只有阻塞事项可恢复，必须说明已解除的条件。")
        session = self.state.data["sessions"].get(self.key(rid))
        self._apply(row, {"状态": "待对接", "_执行标记": ""}, "解除阻塞", reason,
                    session={**session, "phase": "resumed"} if session else None)
        return {"resumed": rid, "next": "重新阅读需求、声明文件范围后再 start。"}
