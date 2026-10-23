"""Delivery contracts stored in the existing, requirement-versioned source field."""
from __future__ import annotations

import json

from handoff_core import HandoffError
from acceptance import validate_policy

CHECK_KINDS = {"local", "mock", "integration", "manual"}


def source(row):
    try:
        value = json.loads(row.get("来源") or "{}")
    except (ValueError, TypeError):
        raise HandoffError("invalid_source", "工单来源不是有效 JSON，请核对原资料。") from None
    if not isinstance(value, dict):
        raise HandoffError("invalid_source", "工单来源必须是 JSON 对象。")
    return value


def contract(row):
    value = source(row).get("delivery")
    return validate(value) if value is not None else None


def validate(value, require_acceptance=False):
    def fail(message):
        raise HandoffError("invalid_delivery", message)

    def text(obj, key):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            fail("交付约定缺少非空 " + key + "。")

    def strings(obj, key, required=False):
        items = obj.get(key, [])
        if (not isinstance(items, list) or (required and not items)
                or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in items)
                or len(set(items)) != len(items)):
            fail(key + " 必须是不重复的非空字符串数组。")

    if not isinstance(value, dict):
        fail("新发布工单必须提供 delivery，明确应用范围、交付结果与验收责任。")
    strings(value, "apps", required=True)
    strings(value, "shared_contracts")
    for name in ("scope", "deliverable", "acceptance_owner"):
        text(value, name)
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks:
        fail("delivery.checks 必须列出本单验证及证据提供方。")
    ids = set()
    for check in checks:
        if not isinstance(check, dict):
            fail("验证项必须是对象。")
        for name in ("id", "description", "owner"):
            text(check, name)
        if (check["id"] in ids or not isinstance(check.get("kind"), str)
                or check["kind"] not in CHECK_KINDS or not isinstance(check.get("required"), bool)):
            fail("验证项需要唯一 id、local/mock/integration/manual 类型和布尔 required。")
        ids.add(check["id"])
    if not any(c["required"] for c in checks):
        fail("至少一项本单验证必须为 required。")
    external = value.get("external_checks")
    if not isinstance(external, list):
        fail("external_checks 必须明确列出外部验证；没有则填写空数组。")
    for check in external:
        if not isinstance(check, dict):
            fail("外部验证必须是对象。")
        for name in ("description", "owner"):
            text(check, name)
        if check.get("gate") not in ("none", "start", "acceptance"):
            fail("外部验证 gate 必须是 none/start/acceptance。")
        if check.get("gate") != "none":
            text(check, "ticket_id")
        elif "ticket_id" in check:
            text(check, "ticket_id")
    validate_policy(value, required=require_acceptance)
    return value


def check_results(row, results):
    delivery = contract(row)
    if not delivery:
        return  # Existing tickets keep their original acceptance contract and fingerprints.
    declared = {c["id"]: c for c in delivery["checks"]}
    seen = {}
    for result in results:
        cid = result.get("id")
        if cid is None:
            continue  # Additional commands remain useful evidence.
        if not isinstance(cid, str) or cid in seen or cid not in declared:
            raise HandoffError("invalid_check_reference", "验证 id 重复或不属于本单交付约定。")
        if result.get("kind") != declared[cid]["kind"]:
            raise HandoffError("check_kind_mismatch", "验证类型与交付约定不符，mock 不能替代真实联调。", check_id=cid)
        seen[cid] = result
    missing = [c["id"] for c in declared.values() if c["required"] and seen.get(c["id"], {}).get("outcome") != "passed"]
    if missing:
        raise HandoffError("required_checks_missing", "本单必要验证尚未通过，请修复或记录阻塞。", check_ids=missing)


def external_blockers(row, records, stage):
    delivery = contract(row)
    if not delivery:
        return []
    by_id = {r["id"]: r for r in records}
    result = []
    for check in delivery["external_checks"]:
        if check["gate"] == "none" or (stage == "start" and check["gate"] != "start"):
            continue
        other = by_id.get(check["ticket_id"], {})
        if other.get("状态") != "已完成":
            result.append({"id": check["ticket_id"], "state": other.get("状态", "缺失或无权读取"),
                           "result_version": other.get("结果版本", ""), "gate": check["gate"],
                           "owner": check["owner"], "description": check["description"]})
    return result


def overlap_warnings(items, proposed_count):
    """Only compare declared applications/contracts, never publisher and receiver paths."""
    result = []
    for index, (key, delivery) in enumerate(items[:proposed_count]):
        if not delivery:
            continue
        for other_key, other in items[index + 1:]:
            if not other:
                continue
            apps = set(delivery["apps"]) & set(other["apps"])
            if "*" in delivery["apps"]:
                apps.update(other["apps"])
            if "*" in other["apps"]:
                apps.update(delivery["apps"])
            shared = set(delivery.get("shared_contracts", [])) & set(other.get("shared_contracts", []))
            if apps or shared:
                result.append({"tickets": [key, other_key], "apps": sorted(apps), "shared_contracts": sorted(shared),
                               "next": "核对是否独立验收；共享修改可合并或拆出独立前置事项。此提示不建立依赖或文件锁。"})
    return result
