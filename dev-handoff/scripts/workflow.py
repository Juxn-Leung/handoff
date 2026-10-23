"""Ticket orchestration, durable operation journal and review invariants."""
from __future__ import annotations

import json
import uuid
from handoff_core import HandoffError, digest, now, table_time, owned, plan_queue, requirement, user_ids
from table_schema import CHANGE_TYPES
from delivery import contract, validate as validate_delivery, check_results, external_blockers, overlap_warnings
from planning import Planner, shortened
from acceptance import policy as acceptance_policy, materials as acceptance_materials, matrix as acceptance_matrix, confirmation as validate_confirmation

QUEUE_FIELDS = ["标题", "跟进人", "状态", "优先级", "关联工单", "前置依赖", "创建时间", "处理结果", "结果版本", "来源"]


class Workflow:
    def __init__(self, git, state, identity, binding, tickets, logs, members=None):
        self.git, self.state, self.identity = git, state, identity
        self.binding, self.tickets, self.logs = binding, tickets, logs
        self.members = members
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

    def registered_member(self, uid):
        if self.members is None:
            raise HandoffError("members_required", "请读取共享成员表，核对实际验收人的稳定账号。")
        matches = [r for r in self.members.records() if r.get("账号ID") == uid]
        if len(matches) != 1:
            raise HandoffError("choose_reviewer", "实际验收人须是共享成员表中的唯一账号。", user_id=uid)
        return matches[0]

    def _check_reviewer(self, row):
        delivery = acceptance_policy(contract(row))
        if not delivery or delivery["reviewer_id"] != self.identity["user_id"]:
            raise HandoffError("not_reviewer", "当前账号不是本单约定的实际验收人。")
        self.registered_member(delivery["reviewer_id"])
        return delivery

    def planner(self, records=None):
        return Planner(self.git, self.state, self.identity, self.namespace,
                       records if records is not None else self.tickets.records())

    def queue(self, apps=None, limit=20, offset=0):
        if not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
            raise HandoffError("invalid_page", "queue 的 limit 须为 1–100，offset 不得小于 0。")
        planner = self.planner()
        offered = {r["id"] for r in planner.records if not r.get("跟进人") and r.get("状态") == "待对接" and not r.get("_执行标记")}
        hypothetical = [{**r, "跟进人": self.person} if r["id"] in offered else r for r in planner.records]
        planned = plan_queue(hypothetical, self.identity, self.state.data["last"].get(self.namespace))
        groups = {name: [] for name in ("in_progress", "awaiting_review", "pending_acceptance", "ready", "claimable", "blocked")}
        total_counts = {name: 0 for name in groups}
        reviewer_rows = [r for r in planner.records if r.get("状态") == "待验收" and not owned(r, self.identity)
                         and (acceptance_policy(contract(r)) or {}).get("reviewer_id") == self.identity["user_id"]]
        planned["awaiting_review"] = reviewer_rows
        log_rows = self.logs.records() if reviewer_rows or any(acceptance_policy(contract(r)) for r in planned["pending_acceptance"]) else []
        unscoped = 0
        for name in ("in_progress", "awaiting_review", "pending_acceptance", "ready", "blocked"):
            for planned_row in planned[name]:
                row = planner.by_id[planned_row["id"]]
                current = planner.current({**row, "queue_reason": planned_row.get("queue_reason")})
                group = "claimable" if name == "ready" and row["id"] in offered else name
                if group in ("ready", "claimable") and current["reason"]:
                    group = "blocked"
                total_counts[group] += 1
                delivery = contract(row)
                if not delivery:
                    unscoped += 1
                if apps and (not delivery or not (set(apps) & set(delivery["apps"]) or "*" in delivery["apps"])):
                    continue
                groups[group].append(self._acceptance_queue_item(row, planner.compact(row, current), log_rows))
        counts = {name: len(rows) for name, rows in groups.items()}
        recommended = next((r["id"] for name in ("ready", "claimable") for r in groups[name]), None)
        flattened = [(name, row) for name, rows in groups.items() for row in rows]
        page = {name: [] for name in groups}
        for name, row in flattened[offset:offset + limit]:
            page[name].append(row)
        remaining = max(0, len(flattened) - offset - limit)
        return {**page, "counts": counts, "total_counts": total_counts, "recommended": recommended,
                "apps": apps or [], "unscoped_count": unscoped,
                "page": {"offset": offset, "limit": limit, "shown": sum(map(len, page.values())),
                         "remaining": remaining, "next_offset": offset + limit if remaining else None}}

    def _acceptance_queue_item(self, row, item, logs):
        delivery = acceptance_policy(contract(row))
        if not delivery:
            return item
        item["reviewer_id"] = delivery["reviewer_id"]
        if row.get("状态") != "待验收":
            return item
        try:
            evidence = self._shared_evidence(row)
            approved = self._approval(row, evidence, logs=logs)
            item["acceptance_status"] = "confirmed" if approved else "awaiting_confirmation"
            if approved:
                item["next"] = "原处理账号在原工作区复核同一版本并 accept 回写"
            elif self.identity["user_id"] == delivery["reviewer_id"]:
                item["next"] = "review 查看逐项材料；明确通过指定版本后 confirm"
            else:
                item["next"] = "等待指定验收人 review / confirm 本结果版本"
            job = self.state.data.get("auto_accept", {}).get(digest([self.key(row["id"]), row.get("结果版本")]))
            if job and job["profile"] == self.identity["profile"]:
                from auto_accept import running
                item["auto_accept"] = {"status": job["status"], "reason": job["reason"],
                                       "worker_running": running(self.state, job["id"])}
                if job["status"] == "waiting":
                    item["next"] = ("原工作区自动回写进程等待完整确认及依赖就绪" if item["auto_accept"]["worker_running"]
                                    else "原工作区执行 auto-accept 恢复已启用的自动回写")
                else:
                    item["next"] = "auto-accept --status 查看停止原因，排查后恢复或重新验证提交"
        except HandoffError as exc:
            item["acceptance_status"] = "needs_review"
            item["block_reason"] = shortened(item["block_reason"] + "；" + exc.message)
            item["next"] = "review 核对失效原因，必要时退回或重新提交"
        return item

    def claimable(self):
        # Internal callers must check the complete candidate set, independent of display pagination.
        planner = self.planner()
        offered = {r["id"] for r in planner.records if not r.get("跟进人") and r.get("状态") == "待对接" and not r.get("_执行标记")}
        hypothetical = [{**r, "跟进人": self.person} if r["id"] in offered else r for r in planner.records]
        queue = plan_queue(hypothetical, self.identity, self.state.data["last"].get(self.namespace))
        result = []
        for row in queue["ready"]:
            if row["id"] in offered:
                original = planner.by_id[row["id"]]
                current = planner.current(original)
                if not current["reason"]:
                    result.append(planner.compact(original, current))
        return result

    def show(self, rid):
        planner = self.planner()
        if rid not in planner.by_id:
            raise HandoffError("record_missing", "工单不存在或无权读取。")
        row = planner.by_id[rid]
        return {"record": row, "requirement_version": requirement(row), "delivery": contract(row),
                "current_blocking": planner.current(row),
                "next": "当前提示按本次读取计算；历史备注不作为当前操作指引。"}

    def preflight(self, plans):
        """Save receiver-side plans locally; never claim, lock files, or modify remote tickets."""
        if not isinstance(plans, list) or not plans:
            raise HandoffError("invalid_plan", "需要非空 plans 数组。")
        planner, prepared = self.planner(), {}
        for plan in plans:
            if not isinstance(plan, dict) or not isinstance(plan.get("record_id"), str):
                raise HandoffError("invalid_plan", "每项计划需要 record_id 与 paths。")
            rid = plan["record_id"]
            if rid in prepared or rid not in planner.by_id:
                raise HandoffError("invalid_plan", "计划工单重复、不存在或无权读取。")
            row = planner.by_id[rid]
            if row.get("状态") not in ("待对接", "处理中", "阻塞"):
                raise HandoffError("invalid_plan", "只为待对接、处理中或阻塞事项声明计划。")
            expected = requirement(row)
            if plan.get("requirement_version", expected) != expected:
                raise HandoffError("requirements_changed", "计划对应的需求已变化，请重新 show。", record_id=rid)
            raw_paths = plan.get("paths")
            if not isinstance(raw_paths, list) or not raw_paths:
                raise HandoffError("invalid_plan", "预检必须声明接手方计划文件。")
            paths = self.git.clean_paths(raw_paths)
            self.git.code(paths)  # Validate files, including missing future files; directories are not a scope.
            prepared[rid] = {"record_id": rid, "paths": paths, "requirement": expected,
                             "profile": self.identity["profile"], "planned_at": now()}
        # Do not mutate state until every input has been checked.
        local_plans = {**self.state.data.get("plans", {}), **{self.key(rid): plan for rid, plan in prepared.items()}}
        planner = Planner(self.git, self.state, self.identity, self.namespace, planner.records, local_plans)
        items = []
        for rid, plan in prepared.items():
            row = planner.by_id[rid]
            items.append({"record_id": rid, "requirement_version": plan["requirement"], "paths": plan["paths"],
                          "current_blocking": planner.current(row, planned=plan)})
        peers = []
        candidates = []
        for row in planner.records:
            plan = prepared.get(row["id"]) or planner.plan(row)
            if plan and plan.get("requirement") == requirement(row) and row.get("状态") in ("待对接", "处理中", "阻塞"):
                candidates.append((row["id"], plan["paths"]))
        for index, (rid, paths) in enumerate(candidates):
            for other, other_paths in candidates[index + 1:]:
                overlap = sorted(set(paths) & set(other_paths))
                if overlap and (rid in prepared or other in prepared):
                    peers.append({"tickets": [rid, other], "overlap": overlap,
                                  "next": "规划共享修改的归属或顺序；候选计划不建立文件锁或业务依赖。"})
        self.state.data["plans"] = local_plans
        self.state.save()
        return {"plans": items, "planned_overlap": peers, "remote_modified": False,
                "next": "检查预检提示；仍须 claim / start 成功后才能编辑，扩大范围前先 extend。"}

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
        values = {
            "标题": op["title"], "工单": [op["record_id"]], "操作者": op["person"],
            "时间": op["time"], "内容": op["message"],
        }
        logged = self.logs.create_once("_事件键", op["id"], values, uncertain=op.get("log_uncertain", False))
        if not self.logs.matches(logged, values):
            raise HandoffError("log_changed", "事件键对应的历史记录与本次操作不符，未覆盖。")
        if op.get("session"):
            self.state.data["sessions"][self.key(op["record_id"])] = op["session"]
        if op.get("set_last"):
            self.state.data["last"][self.namespace] = op["record_id"]
        op["phase"] = "done"
        self.state.save()

    def _apply(self, row, patch, title, message, session=None, set_last=False, acceptance_binding=None):
        self._guard_clean()
        fresh = self.tickets.get(row["id"])
        if digest(fresh) != digest(row):
            raise HandoffError("remote_changed", "工单在读取后被修改，请重新查看后再操作。")
        if patch.get("状态") == "已完成":
            approval = self._check_review(row, row.get("结果版本"))
            if approval != acceptance_binding:
                raise HandoffError("confirmation_changed", "验收确认记录发生变化，请重新审阅。")
        op = {"id": str(uuid.uuid4()), "namespace": self.namespace, "record_id": row["id"],
              "before": row, "patch": patch, "title": title, "message": message,
              "person": self.person, "profile": self.identity["profile"], "time": table_time(),
              "phase": "prepared", "session": session, "set_last": set_last}
        if acceptance_binding:
            op["acceptance_binding"] = acceptance_binding
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

    def sync(self, operation_ids=None):
        results = []
        for op in self._pending_operations():
            if operation_ids is not None and op["id"] not in operation_ids:
                continue
            if op["profile"] != self.identity["profile"]:
                raise HandoffError("identity_mismatch", "待恢复写入属于另一个账号。")
            if op.get("kind") == "acceptance_confirmation":
                existing = self.logs.records(equals={"_事件键": op["id"]})
                if not existing:
                    row = self.tickets.get(op["record_id"])
                    delivery = self._check_reviewer(row)
                    evidence = self._shared_evidence(row, op["result_version"])
                    if row.get("状态") != "待验收":
                        raise HandoffError("not_pending_acceptance", "验收对象已变化，不能补写旧版本确认。")
                    event = json.loads(op["message"])
                    validate_confirmation(delivery, evidence, {"result_version": event["result_version"], "items": event["items"]}, op["result_version"])
                self._finish_operation(op)
                results.append({"operation": op["id"], "record_id": op["record_id"], "verified": True})
                continue
            row = self.tickets.get(op["record_id"])
            owners = (op["before"].get("跟进人"), op["patch"].get("跟进人", op["before"].get("跟进人")))
            if requirement(row) != requirement(op["before"]) or row.get("跟进人") not in owners:
                raise HandoffError("remote_changed", "恢复时发现需求或负责人已变化，未覆盖远端内容。")
            if not self.tickets.matches(row, op["patch"]):
                if digest(row) != digest(op["before"]):
                    raise HandoffError("remote_changed", "恢复时远端状态与操作前后均不一致。")
                # Explicit sync may retry an idempotent patch only against unchanged input.
                if op["patch"].get("状态") == "已完成":
                    self._check_operation_acceptance(op, op["before"])
                self.tickets.update(op["record_id"], op["patch"])
            if op["patch"].get("状态") == "已完成":
                # Even a successful-but-unlogged acceptance cannot be finalized using stale code.
                self._check_operation_acceptance(op, {**row, "状态": "待验收"})
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

    def publish(self, manifest, check_only=False):
        self._guard_clean()
        snapshot = self.git.staged()
        if manifest.get("snapshot") != snapshot["fingerprint"]:
            raise HandoffError("staged_changed", "暂存区与整理工单时的快照不同，请重新 capture。")
        batch_key = self.namespace + ":" + snapshot["fingerprint"]
        previous_batch = self.state.data["batches"].get(batch_key)
        legacy_retry = bool(previous_batch and previous_batch["manifest_hash"] == digest(manifest))
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
            delivery = validate_delivery(draft.get("delivery"), require_acceptance=not legacy_retry) if draft.get("delivery") is not None or not legacy_retry else None
            if delivery:
                if acceptance_policy(delivery):
                    self.registered_member(delivery["reviewer_id"])
                for check in delivery["external_checks"]:
                    if check.get("ticket_id"):
                        self.tickets.get(check["ticket_id"])
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
                      "来源": json.dumps({**snapshot, "paths": paths, "repository": self.git.root.name,
                                          **({"delivery": delivery} if delivery else {})}, ensure_ascii=False),
                      "创建时间": table_time()}
            prepared.append((key, values))
        ignored = set(manifest.get("excluded_paths", []))
        if covered | ignored != set(snapshot["paths"]) or ignored & covered:
            raise HandoffError("incomplete_scope", "每个暂存文件必须属于某条工单或明确列为 excluded_paths。")
        if ignored and not manifest.get("exclusion_reason"):
            raise HandoffError("invalid_draft", "排除暂存文件必须说明原因。")
        remote_keys = {digest({"snapshot": snapshot["fingerprint"], "key": key}) for key, _ in prepared}
        candidates = [("draft:" + key, contract(values)) for key, values in prepared]
        candidates += [(row["id"], contract(row)) for row in self.tickets.records()
                       if row.get("状态") != "已完成" and row.get("_工单键") not in remote_keys]
        warnings = overlap_warnings(candidates, len(prepared))
        if check_only:
            return {"checked": [{"key": key, "title": values["标题"], "delivery": contract(values)} for key, values in prepared],
                    "overlap_warnings": warnings, "remote_modified": False}
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
        return {"published": output, "table_url": self.binding["url"], "overlap_warnings": warnings,
                "next": "由已登记成员本人 claim 领取，或由发布方 assign 给已登记成员；接手方用 preflight 提前检查实际文件。"}

    def _scope_conflicts(self, rid, paths):
        conflicts = self.planner().file_conflicts(rid, paths)
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
        blockers = external_blockers(row, self.tickets.records(), "start")
        if blockers:
            raise HandoffError("external_verification_blocked", "交付约定要求的外部前置验证尚未验收。", blockers=blockers)
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

    def ready(self, rid, result, revalidate=False, auto_accept=False):
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
        check_results(row, checks)
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
        delivery = acceptance_policy(contract(row))
        if delivery:
            self.registered_member(delivery["reviewer_id"])
            evidence.update({"submission_id": str(uuid.uuid4()), "processor": self.person,
                             "reviewer_id": delivery["reviewer_id"],
                             "acceptance": acceptance_materials(delivery, checks, result.get("acceptance"))})
        version = digest({k: v for k, v in evidence.items() if k != "verified_at"})
        session = {**session, "phase": "pending_acceptance", "result_version": version,
                   "auto_accept": bool(delivery and auto_accept)}
        patch = {"状态": "待验收", "处理结果": result["summary"] + "\n\n验证：\n" + json.dumps(checks, ensure_ascii=False, indent=2),
                 "结果版本": version, "_结果证据": json.dumps(evidence, ensure_ascii=False)}
        self._apply(row, patch, "提交待验收", "结果版本：" + version + "\n" + patch["处理结果"], session=session, set_last=True)
        return {"pending_acceptance": rid, "result_version": version, "stop": True,
                "queue": self.queue(), "next": "等待操作者验收；只有明确说继续才可处理下一条。"}

    def block(self, rid, reason, attempted, needed, kind="manual", waiting_for=None, paths=None):
        self._guard_clean()
        row, session = self.tickets.get(rid), self.session(rid)
        self.check_owner(row)
        if row.get("状态") != "处理中" or row.get("_执行标记") != session["token"]:
            raise HandoffError("session_conflict", "不能阻塞其他会话正在处理的事项。")
        if not all(isinstance(x, str) and x.strip() for x in (reason, attempted, needed)):
            raise HandoffError("invalid_note", "阻塞记录需包含原因、已尝试方法和所需信息。")
        if kind not in ("manual", "files", "dependency"):
            raise HandoffError("invalid_note", "阻塞类型须为 manual/files/dependency。")
        waiting_for = waiting_for or []
        if not isinstance(waiting_for, list) or any(not isinstance(x, str) or not x or x == rid for x in waiting_for):
            raise HandoffError("invalid_note", "waiting_for 必须使用其他工单的真实 ID。")
        observed = [self.tickets.get(other) for other in waiting_for]
        if kind == "dependency" and not waiting_for:
            raise HandoffError("invalid_note", "依赖阻塞必须声明 waiting_for。")
        blocked_paths = self.git.clean_paths(paths or session["paths"]) if kind == "files" else []
        note = {"type": "handoff_block_v1", "kind": kind, "reason": reason, "attempted": attempted,
                "needed": needed, "waiting_for": waiting_for, "paths": blocked_paths,
                "observed": [{"id": r["id"], "state": r.get("状态"), "result_version": r.get("结果版本", "")} for r in observed]}
        message = f"原因：{reason}\n已尝试：{attempted}\n所需信息：{needed}\n\n" + json.dumps(note, ensure_ascii=False)
        self._apply(row, {"状态": "阻塞", "处理结果": json.dumps(note, ensure_ascii=False)}, "处理阻塞", message,
                    session={**session, "phase": "blocked"}, set_last=True)
        return {"blocked": rid, "stop": True, "queue": self.queue()}

    def _shared_evidence(self, row, confirmed_version=None, allow_outdated=False):
        if confirmed_version is not None and row.get("结果版本") != confirmed_version:
            raise HandoffError("review_version_changed", "操作者审阅的结果版本与当前版本不同。")
        try:
            evidence = json.loads(row.get("_结果证据") or "{}")
        except (ValueError, TypeError) as exc:
            raise HandoffError("invalid_evidence", "结果证据不是有效 JSON。") from exc
        if not isinstance(evidence, dict) or not isinstance(evidence.get("code"), dict) or not evidence["code"]:
            raise HandoffError("invalid_evidence", "结果证据缺少代码指纹。")
        current = evidence.get("requirement") == requirement(row)
        if not current and not allow_outdated:
            raise HandoffError("requirements_changed", "需求已变化，旧结果必须重新处理和验收。")
        expected = digest({k: v for k, v in evidence.items() if k != "verified_at"})
        if expected != row.get("结果版本"):
            raise HandoffError("invalid_evidence", "结果版本与验证证据不匹配。")
        delivery = acceptance_policy(contract(row))
        if delivery and current:
            if not evidence.get("submission_id") or evidence.get("processor") != row.get("跟进人"):
                raise HandoffError("invalid_evidence", "结果缺少本轮交付标识或原处理账号不匹配。")
            if evidence.get("reviewer_id") != delivery["reviewer_id"]:
                raise HandoffError("invalid_evidence", "结果未绑定本单实际验收人。")
            if not isinstance(evidence.get("checks"), list) or any(not isinstance(c, dict) for c in evidence["checks"]):
                raise HandoffError("invalid_evidence", "结果验证记录不完整。")
            check_results(row, evidence.get("checks", []))
            acceptance_matrix(delivery, evidence)
        return evidence

    def _check_evidence(self, row):
        evidence = self._shared_evidence(row)
        if evidence.get("workspace") != digest(str(self.git.root)):
            raise HandoffError("wrong_workspace", "此结果来自另一个工作区，请在原工作区验收回写。")
        if self.git.code(evidence["code"].keys()) != evidence["code"]:
            raise HandoffError("code_changed", "待验收代码已变化，请重新验证并取得新结果版本。")
        return evidence

    def _confirmation_key(self, rid, version, reviewer):
        return digest({"kind": "acceptance_confirmation_v1", "namespace": self.namespace,
                       "ticket": rid, "result_version": version, "reviewer_id": reviewer})

    def _approval(self, row, evidence, required=False, logs=None):
        delivery = acceptance_policy(contract(row))
        if not delivery:
            return None
        key = self._confirmation_key(row["id"], row["结果版本"], delivery["reviewer_id"])
        rows = [r for r in logs if r.get("_事件键") == key] if logs is not None else self.logs.records(equals={"_事件键": key})
        if not rows:
            if required:
                raise HandoffError("confirmation_missing", "指定验收人尚未逐项确认这个结果版本。", reviewer_id=delivery["reviewer_id"])
            return None
        if len(rows) != 1:
            raise HandoffError("invalid_confirmation", "同一版本存在重复验收确认事件。")
        logged = rows[0]
        try:
            event = json.loads(logged.get("内容") or "{}")
        except (ValueError, TypeError):
            raise HandoffError("invalid_confirmation", "验收确认事件不是有效 JSON。") from None
        reviewer = [{"userId": delivery["reviewer_id"], "corpId": self.identity["corp_id"]}]
        if (not isinstance(event, dict) or event.get("type") != "acceptance_confirmation_v1"
                or event.get("decision") != "approved" or event.get("record_id") != row["id"]
                or event.get("reviewer_id") != delivery["reviewer_id"] or logged.get("操作者") != reviewer
                or logged.get("工单") != [row["id"]] or event.get("requirement_version") != requirement(row)
                or event.get("confirmed_at") != logged.get("时间")):
            raise HandoffError("invalid_confirmation", "确认事件的实际账号、工单、需求或时间不匹配。")
        validate_confirmation(delivery, evidence, {"result_version": event.get("result_version"), "items": event.get("items")}, row["结果版本"])
        return {"event_key": key, "digest": digest(event), "reviewer_id": delivery["reviewer_id"], "confirmed_at": event["confirmed_at"]}

    def confirm(self, rid, confirmed_version, supplied):
        self._guard_clean()
        row = self.tickets.get(rid)
        delivery = self._check_reviewer(row)
        if row.get("状态") != "待验收":
            raise HandoffError("not_pending_acceptance", "只能确认待验收的结果。")
        evidence = self._shared_evidence(row, confirmed_version)
        if evidence["workspace"] == digest(str(self.git.root)):
            self._check_evidence(row)
        items = validate_confirmation(delivery, evidence, supplied, confirmed_version)
        existing = self._approval(row, evidence)
        key = self._confirmation_key(rid, confirmed_version, self.identity["user_id"])
        if existing:
            prior = json.loads(self.logs.records(equals={"_事件键": key})[0]["内容"])
            if prior["items"] != items:
                raise HandoffError("confirmation_changed", "本版本已有不可覆盖的确认；需要调整时先退回或提交新版本。")
            return {"confirmed": rid, "result_version": confirmed_version, "already_confirmed": True,
                    "next": "仍待原处理账号在原工作区复核代码并 accept 回写。"}
        if digest(self.tickets.get(rid)) != digest(row):
            raise HandoffError("remote_changed", "确认前工单已变化，请重新 review。")
        stamp = table_time()
        event = {"type": "acceptance_confirmation_v1", "decision": "approved", "record_id": rid,
                 "result_version": confirmed_version, "requirement_version": requirement(row),
                 "reviewer_id": self.identity["user_id"], "confirmed_at": stamp, "items": items}
        op = {"id": key, "kind": "acceptance_confirmation", "namespace": self.namespace, "record_id": rid,
              "result_version": confirmed_version, "before": row, "title": "逐项验收确认", "message": json.dumps(event, ensure_ascii=False),
              "person": self.person, "profile": self.identity["profile"], "time": stamp, "phase": "prepared"}
        self.state.data["operations"][key] = op
        self.state.save()
        try:
            self._finish_operation(op)
        except HandoffError as exc:
            op["log_uncertain"] = not exc.details.get("not_executed", False)
            self.state.save()
            raise
        return {"confirmed": rid, "result_version": confirmed_version, "reviewer_id": self.identity["user_id"],
                "event_key": key, "stop": True, "next": "业务确认已记录；等待原工作区已启用的自动回写，或由原处理账号 accept。尚不能报告已完成。"}

    def review(self, rid):
        row = self.tickets.get(rid)
        delivery = acceptance_policy(contract(row))
        if not delivery:
            self._check_review(row, row.get("结果版本"))
            return {"record_id": rid, "summary": row["处理结果"], "result_version": row["结果版本"],
                    "delivery": contract(row), "code_and_requirements_current": True,
                    "next": "旧版工单仍由原处理账号在原工作区明确验收。"}
        issues, matrix, approval = [], [], None
        local_code = "not_checked"
        try:
            evidence = self._shared_evidence(row)
            matrix = acceptance_matrix(delivery, evidence)
            approval = self._approval(row, evidence)
            if evidence["workspace"] == digest(str(self.git.root)):
                self._check_evidence(row)
                local_code = "current"
                if owned(row, self.identity):
                    session = self.state.data["sessions"].get(self.key(rid), {})
                    if (session.get("profile") != self.identity["profile"] or session.get("phase") != "pending_acceptance"
                            or session.get("token") != row.get("_执行标记") or session.get("result_version") != row.get("结果版本")):
                        issues.append({"code": "session_conflict", "message": "当前工作区缺少匹配的原执行会话，不能回写。"})
                    if self._pending_operations():
                        issues.append({"code": "sync_required", "message": "原工作区存在待恢复的写入，需先 sync。"})
        except HandoffError as exc:
            issues.append({"code": exc.code, "message": exc.message})
        if row.get("状态") != "待验收":
            issues.append({"code": "not_pending_acceptance", "message": "本单当前不是待验收状态。"})
        if not approval:
            issues.append({"code": "confirmation_missing", "message": "尚无指定验收人对本版本的完整确认。"})
        try:
            self._check_acceptance_dependencies(row)
        except HandoffError as exc:
            issues.append({"code": exc.code, "message": exc.message, **exc.details})
        return {"record_id": rid, "result_version": row.get("结果版本"), "requirement_version": requirement(row),
                "summary": row.get("处理结果"), "delivery": delivery, "reviewer_id": delivery["reviewer_id"],
                "acceptance_matrix": matrix, "confirmation": approval, "local_code": local_code,
                "blocking": issues, "can_finalize": not issues and local_code == "current" and owned(row, self.identity),
                "next": "指定验收人逐项确认；随后原处理账号在原工作区复核同一版本并回写。"}

    def _check_operation_acceptance(self, op, row):
        approval = self._check_review(row, op["before"].get("结果版本"))
        if approval != op.get("acceptance_binding"):
            raise HandoffError("confirmation_changed", "待恢复验收所绑定的确认记录已变化。")

    def _check_acceptance_dependencies(self, row):
        for rid in row.get("前置依赖") or []:
            if self.tickets.get(rid).get("状态") != "已完成":
                raise HandoffError("dependency_blocked", "前置依赖已退回或未完成，不能验收。", dependency=rid)
        blockers = external_blockers(row, self.tickets.records(), "acceptance")
        if blockers:
            raise HandoffError("external_verification_blocked", "交付约定要求的外部验收验证尚未完成。", blockers=blockers)

    def _check_review(self, row, confirmed_version):
        self.check_owner(row)
        if row.get("状态") != "待验收":
            raise HandoffError("not_pending_acceptance", "工单不是待验收状态。")
        if row.get("结果版本") != confirmed_version:
            raise HandoffError("review_version_changed", "操作者验收的结果版本与当前版本不同。")
        evidence = self._check_evidence(row)
        self._check_acceptance_dependencies(row)
        approval = self._approval(row, evidence, required=True)
        if approval:
            self.registered_member(approval["reviewer_id"])
        return approval

    def accept(self, rid, confirmed_version):
        self._guard_clean()
        row = self.tickets.get(rid)
        approval = self._check_review(row, confirmed_version)
        session = self.session(rid)
        if (session.get("phase") != "pending_acceptance" or session.get("token") != row.get("_执行标记")
                or session.get("result_version") != confirmed_version):
            raise HandoffError("session_conflict", "验收回写必须使用原处理账号和原执行会话。")
        reviewer = [{"userId": approval["reviewer_id"], "corpId": self.identity["corp_id"]}] if approval else self.person
        self._apply(row, {"状态": "已完成", "验收人": reviewer, "验收时间": approval["confirmed_at"] if approval else table_time(), "_执行标记": ""},
                    "验收通过", "原工作区复核并回写结果版本：" + confirmed_version + "\n确认记录：" + json.dumps(approval, ensure_ascii=False),
                    session={**session, "phase": "completed"}, acceptance_binding=approval)
        return {"accepted": rid, "result_version": confirmed_version, "reviewer": reviewer, "stop": True}

    def reject(self, rid, reason, confirmed_version=None, criterion_ids=None):
        self._guard_clean()
        row = self.tickets.get(rid)
        delivery = acceptance_policy(contract(row))
        if delivery:
            if not owned(row, self.identity):
                self._check_reviewer(row)
            if not confirmed_version:
                raise HandoffError("review_version_required", "退回必须明确指定已审阅的结果版本。")
            submitted = self._shared_evidence(row, confirmed_version, allow_outdated=True)
            known = {c["criterion_id"] for c in submitted.get("acceptance", [])} or {c["id"] for c in delivery["criteria"]}
            criterion_ids = criterion_ids if criterion_ids is not None else sorted(known)
            if (not isinstance(criterion_ids, list) or not criterion_ids
                    or any(not isinstance(c, str) or c not in known for c in criterion_ids) or len(set(criterion_ids)) != len(criterion_ids)):
                raise HandoffError("invalid_criteria", "退回反馈须关联本单验收标准。")
        else:
            self.check_owner(row)
        if row.get("状态") != "待验收" or not reason.strip():
            raise HandoffError("invalid_rejection", "只有待验收工单可以退回，且必须填写原因。")
        affected = [r["id"] for r in self.tickets.records() if rid in (r.get("前置依赖") or []) or rid in (r.get("关联工单") or [])
                    or any(c.get("ticket_id") == rid for c in (contract(r) or {}).get("external_checks", []))]
        session = self.state.data["sessions"].get(self.key(rid))
        if session and session.get("profile") == self.identity["profile"] and session.get("token") == row.get("_执行标记"):
            session = {**session, "phase": "returned"}
        else:
            session = None
        message = reason + "\n受影响工单：" + ", ".join(affected)
        if delivery:
            message = json.dumps({"type": "acceptance_rejection_v1", "record_id": rid, "result_version": confirmed_version,
                                  "requirement_version": submitted["requirement"], "current_requirement_version": requirement(row), "actor_id": self.identity["user_id"],
                                  "criterion_ids": criterion_ids, "reason": reason, "affected": affected}, ensure_ascii=False)
        self._apply(row, {"状态": "待对接", "_执行标记": "", "验收人": [], "验收时间": None},
                    "验收退回", message, session=session)
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
