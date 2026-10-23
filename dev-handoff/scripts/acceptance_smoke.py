#!/usr/bin/env python3
"""Opt-in live protocol exercise in a NEW test table and two disposable Git workspaces.

Uses only the invoking account. Its automated confirmations are test fixtures, never
human business acceptance. Tokens are consumed in memory, never copied to fixtures.
Reuse the same --run-dir after an interruption so creation/publication checkpoints survive.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from handoff_core import Git, HandoffError, State, now, read_json, requirement, write_json
from tencent_mcp import TencentMCP, load_token
from tencent_tables import identity, create, bind, tables, join
from workflow import Workflow

SOURCE = "def render(items):\n    if items is None:\n        return 'error'\n    return ','.join(map(str, items)) if items else 'empty'\n"
CHECK = "from service import render; assert render([1, 2]) == '1,2'; assert render([]) == 'empty'; assert render(None) == 'error'; print('list=1,2; empty=empty; failure=error')"


def git_command(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git_command(root, "init", "-q")
        git_command(root, "config", "user.name", "dev-handoff acceptance fixture")
        git_command(root, "config", "user.email", "acceptance-fixture@example.invalid")
        (root / "service.py").write_text(SOURCE)
        (root / ".gitignore").write_text("__pycache__/\n.dev-handoff.json\n")
        git_command(root, "add", "service.py", ".gitignore")
        git_command(root, "commit", "-qm", "Initialize isolated acceptance fixture")
        (root / "service.py").write_text(SOURCE + "\n# staged protocol fixture\n")
        git_command(root, "add", "service.py")
    return Git(root)


def exercise(repo, run_dir):
    # Authenticate before creating any remote resource or isolated workspace.
    client = TencentMCP(load_token(repo))
    who = identity(client)
    if run_dir.exists() and any(run_dir.iterdir()) and not (run_dir / "run.json").is_file():
        raise HandoffError("unsafe_smoke_directory", "验证目录非空且没有本工具检查点，不能用作隔离夹具。")
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = run_dir / "run.json"
    checkpoint = read_json(checkpoint_path, {"fixture": "acceptance_closure_v1", "actor_id": who["user_id"], "steps": []})
    if checkpoint.get("fixture") != "acceptance_closure_v1":
        raise HandoffError("unsafe_smoke_directory", "此目录不是本验收流程夹具，不能继续。")
    if checkpoint["actor_id"] != who["user_id"]:
        raise HandoffError("identity_mismatch", "此验证目录属于另一个实际账号，不能切换身份续跑。")
    write_json(checkpoint_path, checkpoint)
    pg, rg = workspace(run_dir / "processor"), workspace(run_dir / "reviewer")
    ps, rs = State(pg, run_dir / "processor-state"), State(rg, run_dir / "reviewer-state")
    alias = "验收闭环演练"
    if ps.project["bindings"]:
        binding = ps.select(alias)
    else:
        binding = create(client, ps, who, "dev-handoff 验收闭环演练 " + run_dir.name, alias)["created"]
    pt, pl, pm = tables(client, binding, who)
    join(pm, ps, who)
    if not rs.project["bindings"]:
        bind(client, rs, who, binding["url"], alias)
    rt, rl, rm = tables(client, binding, who)
    processor = Workflow(pg, ps, who, binding, pt, pl, pm)
    reviewer = Workflow(rg, rs, who, binding, rt, rl, rm)
    processor.sync()
    reviewer.sync()

    def save_step(name, **values):
        checkpoint.update(values)
        if name not in checkpoint["steps"]:
            checkpoint["steps"].append(name)
        write_json(checkpoint_path, checkpoint)

    draft_path = run_dir / "draft.json"
    if not draft_path.exists():
        delivery = {"apps": ["acceptance-protocol-fixture"], "scope": "独立自动化协议演练，不承接真实业务交付",
                    "deliverable": "验证发布、退回、重提、逐项确认与原工作区回写",
                    "acceptance_owner": "当前授权账号执行自动化演练，不代表人工业务验收", "reviewer_id": who["user_id"],
                    "checks": [{"id": "render-check", "description": "演练夹具的列表、空态与失败状态", "kind": "local", "owner": "自动化演练", "required": True}],
                    "external_checks": [], "shared_contracts": [],
                    "criteria": [{"id": "render-states", "description": "演练夹具返回约定状态",
                                  "steps": [sys.executable + " -c " + CHECK], "expected": "list=1,2; empty=empty; failure=error", "check_ids": ["render-check"]}]}
        write_json(draft_path, {"snapshot": pg.staged()["fingerprint"], "tickets": [{
            "key": "acceptance-protocol-fixture", "title": "[自动化演练，非业务验收] 发布至验收回写",
            "description": "用户授权的验收协议测试；独立测试表，两个工作区，同一实际账号。", "change_types": ["feat"], "paths": ["service.py"],
            "handoff": "只修改本次生成的夹具；不操作任何真实业务工单。", "acceptance": "按结构化标准检查夹具，并演练退回和重提。", "delivery": delivery}],
            "excluded_paths": [], "exclusion_reason": ""})
    if "ticket_id" not in checkpoint:
        rid = processor.publish(read_json(draft_path))["published"][0]["id"]
        save_step("published", ticket_id=rid, table_url=binding["url"])
    rid = checkpoint["ticket_id"]

    def result(round_number):
        check = subprocess.run([sys.executable, "-c", CHECK], cwd=pg.root, check=True, capture_output=True, text=True)
        return {"summary": "自动化验收协议演练第 " + str(round_number) + " 轮；非人工业务验收",
                "checks": [{"id": "render-check", "kind": "local", "command": sys.executable + " -c " + CHECK, "outcome": "passed"}],
                "acceptance": [{"criterion_id": "render-states", "actual": check.stdout.strip(),
                                "evidence": [{"check_id": "render-check", "content": check.stdout.strip() + "\nexit_code=0"}]}]}

    if "first_version" not in checkpoint:
        row = pt.get(rid)
        if not row.get("跟进人"):
            processor.claim(rid, requirement(row))
            row = pt.get(rid)
        if row["状态"] == "待对接":
            processor.start(rid, ["service.py"], requirement(row))
        row = pt.get(rid)
        first = row.get("结果版本") if row["状态"] == "待验收" else processor.ready(rid, result(1))["result_version"]
        save_step("first_submission", first_version=first)
    first = checkpoint["first_version"]
    if "returned" not in checkpoint["steps"]:
        row = pt.get(rid)
        if row["状态"] == "待验收":
            reviewer.reject(rid, "自动化演练：主动执行退回分支，验证重提版本隔离；不代表业务缺陷。", first, ["render-states"])
        if pt.get(rid)["状态"] != "待对接":
            raise HandoffError("smoke_state_conflict", "退回后的测试状态不符，保留现场。")
        save_step("returned")
    if "second_version" not in checkpoint:
        row = pt.get(rid)
        if row["状态"] == "待对接":
            processor.start(rid, ["service.py"], requirement(row))
        row = pt.get(rid)
        second = row.get("结果版本") if row["状态"] == "待验收" else processor.ready(rid, result(2))["result_version"]
        if first == second:
            raise HandoffError("smoke_version_reused", "重提意外复用了旧结果版本。")
        save_step("resubmitted", second_version=second)
    second = checkpoint["second_version"]
    if "confirmed" not in checkpoint["steps"]:
        report = reviewer.review(rid)
        supplied = {"result_version": second, "items": [
            {"criterion_id": c["id"], "outcome": "passed", "observed": "自动化演练按步骤执行，stdout 与夹具预期一致；此记录不代表人工业务验收。",
             "evidence_digests": [e["digest"] for e in c["evidence"]]} for c in report["acceptance_matrix"]]}
        reviewer.confirm(rid, second, supplied)
        save_step("confirmed")

    def must_reject(action, expected_code):
        try:
            action()
        except HandoffError as exc:
            if exc.code != expected_code:
                raise
        else:
            raise HandoffError("smoke_guard_failed", "测试保护未拒绝操作。", expected_code=expected_code)

    if "guards_verified" not in checkpoint["steps"]:
        must_reject(lambda: processor.accept(rid, first), "review_version_changed")
        must_reject(lambda: reviewer.accept(rid, second), "wrong_workspace")
        source = pg.root / "service.py"
        original = source.read_text()
        try:
            source.write_text(original + "\n# changed after confirmation\n")
            must_reject(lambda: processor.accept(rid, second), "code_changed")
        finally:
            source.write_text(original)
        save_step("guards_verified")
    if pt.get(rid)["状态"] != "已完成":
        processor.accept(rid, second)
    completed = pt.get(rid)
    if completed["结果版本"] != second or completed.get("验收人") != [{"userId": who["user_id"], "corpId": who["corp_id"]}]:
        raise HandoffError("smoke_readback_failed", "完成状态、结果版本或实际验收人回读不符。")
    processor._check_evidence(completed)
    events = [row["标题"] for row in pl.records(equals={"工单": [rid]})]
    expected = {"领取工单": 1, "开始处理": 2, "提交待验收": 2, "验收退回": 1, "逐项验收确认": 1, "验收通过": 1}
    if any(events.count(name) != count for name, count in expected.items()):
        raise HandoffError("smoke_history_incomplete", "验收演练日志数量与完整流程不符，请回读核对。")
    save_step("finalized")
    report = {"mode": "live_single_account_two_workspaces", "business_acceptance": False, "completed_at": now(),
              "actual_account": {"user_id": who["user_id"], "name": who["name"]}, "table_url": binding["url"], "record_id": rid,
              "first_version": first, "final_version": second, "steps": checkpoint["steps"],
              "history_events": events,
              "limitations": ["尚未验证第二个真实腾讯账号；双账号权限由离线双客户端测试覆盖。"]}
    write_json(run_dir / "report.json", report)
    return {"report": str(run_dir / "report.json"), **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="仅从此仓库加载本人已有授权，不复制 Token")
    parser.add_argument("--run-dir", required=True, help="本次独立夹具与恢复目录；重试须复用")
    parser.add_argument("--live", action="store_true", help="明确执行真实腾讯测试表写入；不会修改业务表")
    args = parser.parse_args()
    if not args.live:
        parser.error("此命令会创建真实测试表，须指定 --live；测试确认不代表业务验收。")
    try:
        print(json.dumps({"ok": True, "data": exercise(Path(args.repo).resolve(), Path(args.run_dir).resolve())}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details},
                          "next": "保留同一 run-dir 与所有检查点，先核对并 sync，不换名重建或盲目重放。"}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
