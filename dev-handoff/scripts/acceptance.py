"""Version-bound acceptance criteria, submitted evidence, and reviewer decisions."""
from __future__ import annotations

import re

from handoff_core import HandoffError, digest


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_policy(delivery, required=False):
    if not required and not ({"reviewer_id", "criteria"} & delivery.keys()):
        return  # Historical contracts keep their original acceptance path.
    reviewer = delivery.get("reviewer_id")
    if not isinstance(reviewer, str) or not re.fullmatch(r"[\w:-]{1,128}", reviewer):
        raise HandoffError("invalid_reviewer", "交付约定须填写实际验收人的稳定 reviewer_id，不能使用昵称或分工。")
    criteria = delivery.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise HandoffError("invalid_criteria", "交付约定须逐项填写 criteria。")
    ids, checks = set(), {check["id"] for check in delivery["checks"]}
    for criterion in criteria:
        if not isinstance(criterion, dict) or any(not nonempty(criterion.get(k)) for k in ("id", "description", "expected")):
            raise HandoffError("invalid_criteria", "每项验收标准须有 id、description 和 expected。")
        steps, references = criterion.get("steps"), criterion.get("check_ids")
        if (criterion["id"] in ids or not isinstance(steps, list) or not steps or any(not nonempty(s) for s in steps)
                or not isinstance(references, list) or not references
                or any(not isinstance(c, str) or c not in checks for c in references)
                or len(set(references)) != len(references)):
            raise HandoffError("invalid_criteria", "验收标准 id 须唯一，须提供操作步骤并关联已声明的检查 id。")
        ids.add(criterion["id"])


def policy(delivery):
    return delivery if delivery and "reviewer_id" in delivery else None


def materials(delivery, checks, supplied):
    """Keep observations and readable evidence in the result fingerprint, not mutable notes."""
    if not isinstance(supplied, list):
        raise HandoffError("acceptance_materials_missing", "结果须按每项验收标准提交 acceptance 实际结果与证据。")
    by_id, seen = {}, set()
    criteria = {c["id"]: c for c in delivery["criteria"]}
    passed = {c.get("id") for c in checks if c.get("outcome") == "passed"}
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get("criterion_id"), str):
            raise HandoffError("invalid_acceptance_materials", "验收材料需要 criterion_id。")
        cid = item["criterion_id"]
        if cid not in criteria or cid in seen or not nonempty(item.get("actual")):
            raise HandoffError("invalid_acceptance_materials", "验收材料须逐项对应唯一标准并提供实际结果。")
        criterion = criteria[cid]
        if not set(criterion["check_ids"]).issubset(passed):
            raise HandoffError("criterion_unverified", "验收标准关联的检查尚未全部通过。", criterion_id=cid)
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise HandoffError("acceptance_evidence_missing", "每项验收标准须提供可阅读的证据。", criterion_id=cid)
        normalized, referenced = [], set()
        for entry in evidence:
            if (not isinstance(entry, dict) or not isinstance(entry.get("check_id"), str)
                    or entry["check_id"] not in criterion["check_ids"] or not nonempty(entry.get("content"))):
                raise HandoffError("invalid_acceptance_evidence", "证据须关联本项检查并包含脱敏输出或实际观察。", criterion_id=cid)
            if "reference" in entry and not nonempty(entry["reference"]):
                raise HandoffError("invalid_acceptance_evidence", "证据 reference 须为非空引用。")
            value = {"check_id": entry["check_id"], "content": entry["content"]}
            if "reference" in entry:
                value["reference"] = entry["reference"]
            fingerprint = digest(value)
            if entry.get("digest", fingerprint) != fingerprint:
                raise HandoffError("acceptance_evidence_changed", "证据内容与指纹不匹配。", criterion_id=cid)
            normalized.append({**value, "digest": fingerprint})
            referenced.add(entry["check_id"])
        if referenced != set(criterion["check_ids"]):
            raise HandoffError("acceptance_evidence_missing", "本项每个检查都须关联具体证据。", criterion_id=cid)
        if len({e["digest"] for e in normalized}) != len(normalized):
            raise HandoffError("invalid_acceptance_evidence", "同一标准的证据不能重复。")
        by_id[cid] = {"criterion_id": cid, "actual": item["actual"], "evidence": normalized}
        seen.add(cid)
    if seen != set(criteria):
        raise HandoffError("acceptance_materials_missing", "验收材料未覆盖所有标准。", criterion_ids=sorted(set(criteria) - seen))
    return [by_id[c["id"]] for c in delivery["criteria"]]


def matrix(delivery, evidence):
    accepted = materials(delivery, evidence["checks"], evidence.get("acceptance"))
    by_id = {m["criterion_id"]: m for m in accepted}
    return [{**criterion, **by_id[criterion["id"]]} for criterion in delivery["criteria"]]


def confirmation(delivery, evidence, supplied, version):
    if not isinstance(supplied, dict) or supplied.get("result_version") != version:
        raise HandoffError("review_version_changed", "逐项确认必须显式绑定本次审阅的结果版本。")
    items = supplied.get("items")
    if not isinstance(items, list):
        raise HandoffError("invalid_confirmation", "确认材料须包含逐项 items。")
    expected = {m["criterion_id"]: {e["digest"] for e in m["evidence"]} for m in matrix(delivery, evidence)}
    seen, result = set(), {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("criterion_id"), str):
            raise HandoffError("invalid_confirmation", "每项确认须填写 criterion_id。")
        cid, references = item["criterion_id"], item.get("evidence_digests")
        if (cid not in expected or cid in seen or item.get("outcome") != "passed" or not nonempty(item.get("observed"))
                or item.get("result_version", version) != version
                or not isinstance(references, list) or any(not isinstance(r, str) for r in references)
                or len(set(references)) != len(references) or set(references) != expected[cid]):
            raise HandoffError("invalid_confirmation", "每项须明确通过、记录实际观察，并引用同一结果版本的全部对应证据。", criterion_id=cid)
        result[cid] = {"criterion_id": cid, "outcome": "passed", "observed": item["observed"],
                       "evidence_digests": sorted(references), "result_version": version}
        seen.add(cid)
    if seen != set(expected):
        raise HandoffError("confirmation_incomplete", "所有验收标准都须逐项明确确认。", criterion_ids=sorted(set(expected) - seen))
    return [result[c["id"]] for c in delivery["criteria"]]
