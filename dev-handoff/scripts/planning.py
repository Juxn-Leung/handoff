"""Current workspace protection and small, derived queue views; no remote mutations."""
from __future__ import annotations

import json

from delivery import contract, external_blockers
from handoff_core import HandoffError, digest, owned, requirement


def shortened(value, limit=160):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def block_note(row):
    try:
        value = json.loads(row.get("处理结果") or "{}")
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("type") == "handoff_block_v1" else None


class Planner:
    def __init__(self, git, state, identity, namespace, records, plans=None):
        self.git, self.state, self.identity, self.namespace = git, state, identity, namespace
        self.records = records
        self.by_id = {r["id"]: r for r in records}
        self.plans = state.data.get("plans", {}) if plans is None else plans

    def key(self, rid):
        return self.namespace + ":" + rid

    def file_conflicts(self, rid, paths):
        conflicts = {}
        workspace = digest(str(self.git.root))
        for key, session in self.state.data["sessions"].items():
            if key == self.key(rid) or session.get("phase") not in ("working", "pending_acceptance", "blocked"):
                continue
            namespace = key.rsplit(":", 1)[0]
            row = self.by_id.get(session["record_id"]) if namespace == self.namespace else None
            overlap = sorted(set(paths) & set(session.get("paths", [])))
            if overlap:
                conflicts[key] = {"id": session["record_id"], "table_namespace": namespace,
                                  "state": row.get("状态") if row else session["phase"],
                                  "result_version": row.get("结果版本", "") if row else session.get("result_version", ""),
                                  "freshness": "remote" if row else "local_only", "overlap": overlap}
        for row in self.records:
            if row["id"] == rid or row.get("状态") not in ("待验收", "处理中", "阻塞"):
                continue
            try:
                evidence = json.loads(row.get("_结果证据") or "{}")
            except (ValueError, TypeError):
                raise HandoffError("invalid_evidence", "结果证据不是有效 JSON，不能判断文件保护。", record_id=row["id"]) from None
            if not isinstance(evidence, dict) or not isinstance(evidence.get("code", {}), dict):
                raise HandoffError("invalid_evidence", "结果证据结构不符，不能判断文件保护。", record_id=row["id"])
            local = self.state.data["sessions"].get(self.key(row["id"]), {})
            if evidence.get("workspace") == workspace or local:
                overlap = sorted(set(paths) & (set(evidence.get("code", {})) | set(local.get("paths", []))))
                if overlap:
                    conflicts[self.key(row["id"])] = {"id": row["id"], "table_namespace": self.namespace,
                        "state": row.get("状态"), "result_version": row.get("结果版本", ""),
                        "freshness": "remote", "overlap": overlap}
        return list(conflicts.values())

    def plan(self, row):
        session = self.state.data["sessions"].get(self.key(row["id"]), {})
        if session.get("profile") == self.identity["profile"] and session.get("phase") in ("working", "pending_acceptance"):
            return session
        plan = self.plans.get(self.key(row["id"]))
        if plan and plan.get("profile") == self.identity["profile"]:
            return plan
        if session.get("profile") == self.identity["profile"] and session.get("phase") == "blocked":
            return session
        return None

    def current(self, row, planned=None):
        dependencies = []
        for rid in row.get("前置依赖") or []:
            other = self.by_id.get(rid, {})
            if other.get("状态") != "已完成":
                dependencies.append({"id": rid, "state": other.get("状态", "缺失或无权读取"),
                                     "result_version": other.get("结果版本", "")})
        stage = "acceptance" if row.get("状态") == "待验收" else "start"
        external = external_blockers(row, self.records, stage)
        plan = planned if planned is not None else self.plan(row)
        note = block_note(row) if row.get("状态") == "阻塞" else None
        if planned is not None or (plan and plan.get("planned_at")):
            paths = plan.get("paths", [])
        else:
            paths = (note.get("paths", []) if note else []) or (plan.get("paths", []) if plan else [])
        stale = bool(plan and plan.get("requirement") != requirement(row))
        files = self.file_conflicts(row["id"], paths) if paths else []
        waiting = []
        if note and note.get("kind") == "dependency":
            for rid in note.get("waiting_for", []):
                other = self.by_id.get(rid, {})
                if other.get("状态") != "已完成":
                    waiting.append({"id": rid, "state": other.get("状态", "缺失或无权读取"),
                                    "result_version": other.get("结果版本", "")})
        reasons = []
        if dependencies or external or waiting:
            reasons.append("前置或外部验证尚未验收")
        if files:
            reasons.append("计划文件仍受其他工单保护")
        if stale:
            reasons.append("本地文件计划对应的需求已变化")
        if row.get("queue_reason") == "循环依赖":
            reasons.append("循环依赖")
        elif row.get("queue_reason") and row["queue_reason"] not in ("前置依赖未完成", "工单已阻塞，需明确恢复"):
            reasons.append(row["queue_reason"])
        if row.get("状态") == "阻塞":
            if note and note.get("kind") in ("files", "dependency"):
                if not reasons:
                    reasons.append("已记录的阻塞条件已解除，仍需显式 resume")
            else:
                reasons.append(shortened(note["reason"] if note else "历史阻塞说明需重新核对", 100))
        if reasons:
            next_step = "show " + row["id"] + " 查看当前条件"
            if row.get("状态") == "阻塞" and note and note.get("kind") in ("files", "dependency") and not (files or dependencies or external or waiting or stale):
                next_step = "确认解除原因后 resume；随后重新 show / preflight / start"
            elif stale:
                next_step = "show 后重新 preflight 声明计划；旧计划不授权编辑"
            elif files:
                next_step = "核对保护工单的当前结果；验收或改选独立事项"
        elif row.get("状态") == "待验收":
            next_step = "review " + row["id"] + "，等待明确验收指定结果版本"
        elif row.get("状态") == "已完成":
            next_step = "本单交付已验收；查看保留结果与历史"
        elif row.get("状态") == "处理中":
            next_step = "恢复本工作区原会话；扩大范围前先 extend"
        elif not row.get("跟进人"):
            next_step = "show → claim → preflight → start"
        else:
            next_step = "show → preflight → start"
        return {"reason": "；".join(dict.fromkeys(reasons)), "next": next_step,
                "dependencies": dependencies, "external_checks": external, "files": files,
                "waiting_for": waiting, "plan_stale": stale, "scope_checked": bool(paths) and not stale,
                "historical_note": note if row.get("状态") == "阻塞" else None}

    def compact(self, row, current):
        delivery = contract(row)
        refs = current["dependencies"] + current["external_checks"] + current["files"] + current["waiting_for"]
        refs = list({(r.get("table_namespace", self.namespace), r["id"]): r for r in refs}.values())
        return {"id": row["id"], "title": shortened(row.get("标题"), 100),
                "apps": [shortened(app, 80) for app in delivery["apps"][:8]] if delivery else [],
                "app_count": len(delivery["apps"]) if delivery else 0,
                "scope": shortened(delivery["scope"] if delivery else "未声明交付范围（旧工单）"),
                "status": row.get("状态"), "priority": row.get("优先级", "普通"),
                "assigned_to_me": owned(row, self.identity), "scope_checked": current["scope_checked"],
                "block_reason": shortened(current["reason"]), "next": current["next"],
                "blockers": [{"id": r["id"], "state": shortened(r["state"], 40),
                              "version": r.get("result_version", "")[:12],
                              "freshness": r.get("freshness", "remote")} for r in refs[:3]],
                "blocker_count": len(refs)}
