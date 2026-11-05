#!/usr/bin/env python3
"""Isolated, one-command-at-a-time helpers for a real two-person handoff trial."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from acceptance_dual import runtime_fingerprint
from handoff_core import HandoffError, now, read_json, write_json, exclusive_lock
from tencent_mcp import TencentMCP, load_token
from tencent_tables import identity

KIND = "on_demand_conversation_v1"
SKILL = Path(__file__).resolve().parents[1]
ALLOWED = {"bind", "bind-group", "bind-link-group", "join", "members", "inbox", "queue", "show",
           "publish-md", "assign", "preflight", "claim", "start", "note", "update-agreement",
           "sync-agreement", "ready", "review", "confirm", "accept", "reject", "sync", "block", "resume"}


def initialize(repo, directory, expected_runtime=None):
    runtime = runtime_fingerprint(SKILL)
    if expected_runtime and runtime != expected_runtime:
        raise HandoffError("runtime_changed", "安装的技能与本轮版本不符，请先更新并重读技能。")
    who = identity(TencentMCP(load_token(repo)))
    checkpoint = directory / "trial.json"
    if directory.exists() and any(directory.iterdir()) and not checkpoint.is_file():
        raise HandoffError("unsafe_trial_directory", "目录非空且不属于本联调，请使用独立新目录。")
    directory.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(directory / "trial.lock"):
        prior = read_json(checkpoint, {})
        plan = {"kind": KIND, "actor_id": who["user_id"], "runtime_fingerprint": runtime}
        if prior and any(prior.get(k) != v for k, v in plan.items()):
            raise HandoffError("trial_changed", "此目录的账号或运行版本已变化，保留现场并核对。")
        write_json(checkpoint, plan)
        workspace = directory / "workspace"
        workspace.mkdir(exist_ok=True)
        if not (workspace / ".git").exists():
            if any(workspace.iterdir()):
                raise HandoffError("unsafe_trial_directory", "隔离工作区已有未知文件，未覆盖。")
            subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True)
        if git("rev-parse", "--verify", "HEAD").returncode:
            # Only this new throwaway repository gets a synthetic baseline commit.
            (workspace / ".gitignore").write_text(".dev-handoff.json\n__pycache__/\n")
            (workspace / "order-labels.json").write_text("{}\n")
            git("add", ".gitignore", "order-labels.json").check_returncode()
            git("-c", "user.name=dev-handoff trial", "-c", "user.email=trial@example.invalid",
                "commit", "-qm", "Initialize isolated on-demand trial").check_returncode()
    return {**plan, "name": who["name"], "run_dir": str(directory), "workspace": str(workspace),
            "background_started": False}


def invoke(repo, directory, arguments):
    if not arguments or arguments[0] not in ALLOWED or "--auto-accept" in arguments:
        raise HandoffError("on_demand_only", "本轮只允许单次技能命令，不允许后台、监听或自动验收轮询。")
    plan = read_json(directory / "trial.json", {})
    if plan.get("kind") != KIND or plan.get("runtime_fingerprint") != runtime_fingerprint(SKILL):
        raise HandoffError("runtime_changed", "缺少本轮初始化或技能版本已变化，请先核对版本。")
    token = load_token(repo)
    who = identity(TencentMCP(token))
    if who["user_id"] != plan["actor_id"]:
        raise HandoffError("identity_mismatch", "本阶段须使用此目录原来的真实账号。")
    env = os.environ.copy()
    env["TENCENT_DOCS_TOKEN"] = token  # Process memory only; never an argument or persisted field.
    args = [sys.executable, str(SKILL / "scripts/handoff.py"), "--repo", str(directory / "workspace"),
            "--state-dir", str(directory / "state"), *arguments]
    with exclusive_lock(directory / "trial.lock"):
        event = {"actor_id": who["user_id"], "runtime_fingerprint": plan["runtime_fingerprint"],
                 "command": arguments[0], "started_at": now()}
        with (directory / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**event, "phase": "started"}, ensure_ascii=False) + "\n")
        completed = subprocess.run(args, env=env, capture_output=True, text=True)
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            raise HandoffError("trial_cli_unknown", "未得到有效回执；保留现场，先回读，不自动重放。") from None
        with (directory / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({**event, "phase": "finished", "finished_at": now(),
                                    "ok": bool(result.get("ok")), "exit_code": completed.returncode}, ensure_ascii=False) + "\n")
        if completed.returncode or not result.get("ok"):
            error = result.get("error", {})
            raise HandoffError(error.get("code", "trial_cli_failed"), error.get("message", "命令未完成；先核对共享状态。"))
        return {"actor_id": who["user_id"], "runtime_fingerprint": plan["runtime_fingerprint"],
                "background_started": False, "data": result["data"]}


def verify(path, separator):
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise HandoffError("invalid_fixture", "联调 JSON 文件超过大小上限。")
    value = json.loads(raw.decode("utf-8"))
    expected = {"separator": separator, "empty_text": "暂无订单", "error_text": "加载失败，请重试"}
    if value != expected:
        raise HandoffError("fixture_failed", "交付文件与本轮明确约定不符。", expected=expected, actual=value)
    return {"passed": True, "sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8"),
            "observed": {"list": value["separator"].join(["1", "2"]), "empty": value["empty_text"], "failure": value["error_text"]}}


def main():
    parser = argparse.ArgumentParser(description="按需真实双人联调：隔离工作区与单次命令，不包含自动阶段或后台")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("fingerprint")
    init = sub.add_parser("init")
    init.add_argument("--repo", required=True, help="仅读取本人已授权项目中的 Token")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--expected-runtime")
    run = sub.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--run-dir", required=True)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    check = sub.add_parser("verify")
    check.add_argument("artifact")
    check.add_argument("--separator", choices=[",", "、"], required=True)
    args = parser.parse_args()
    try:
        if args.action == "fingerprint":
            data = {"kind": KIND, "runtime_fingerprint": runtime_fingerprint(SKILL)}
        elif args.action == "init":
            data = initialize(Path(args.repo).resolve(), Path(args.run_dir).resolve(), args.expected_runtime)
        elif args.action == "run":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            data = invoke(Path(args.repo).resolve(), Path(args.run_dir).resolve(), arguments)
        else:
            data = verify(Path(args.artifact), args.separator)
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, **exc.details}}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(json.dumps({"ok": False, "error": {"code": "trial_failed", "message": "本地联调文件或命令不符合预期；保留现场核对。"}}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
