"""Bounded local workers finalize only an authorized, unchanged submission."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from handoff_core import Git, HandoffError, State, digest, exclusive_lock, now, requirement
from acceptance import policy
from delivery import contract

POLL_SECONDS = 60
MAX_WAIT_SECONDS = 7 * 24 * 60 * 60
WAITING = {"confirmation_missing", "dependency_blocked", "external_verification_blocked"}
TRANSIENT = {"local_busy", "tencent_network_error", "tencent_rate_limited", "tencent_http_error"}


def jobs(state):
    return state.data.setdefault("auto_accept", {})


def register(flow):
    """Called under the workspace lock after ready/sync; never grants new consent."""
    active = []
    for key, session in flow.state.data["sessions"].items():
        rid = session["record_id"]
        if (key != flow.key(rid) or session.get("profile") != flow.identity["profile"]
                or session.get("phase") != "pending_acceptance" or not session.get("auto_accept")):
            continue
        jid = digest([key, session["result_version"]])
        job = jobs(flow.state).get(jid)
        if job is None:
            job = {"id": jid, "record_id": rid, "namespace": flow.namespace,
                   "profile": session["profile"], "token": session["token"],
                   "version": session["result_version"], "requirement": session["requirement"],
                   "workspace": digest(str(flow.git.root)), "table": flow.binding["url"],
                   "status": "waiting", "reason": "confirmation_missing", "created_at": now(),
                   "expires_at": time.time() + MAX_WAIT_SECONDS}
            jobs(flow.state)[jid] = job
    for job in jobs(flow.state).values():
        if (job["namespace"] == flow.namespace and job["profile"] == flow.identity["profile"]
                and job["status"] == "waiting"):
            active.append(dict(job))
    flow.state.save()
    return active


def enable(flow, rid):
    row, session = flow.tickets.get(rid), flow.session(rid)
    flow.check_owner(row)
    if not policy(contract(row)):
        raise HandoffError("legacy_acceptance", "旧工单没有指定验收人与逐项标准，不能自动回写。")
    if (row.get("状态") != "待验收" or session.get("phase") != "pending_acceptance"
            or session.get("token") != row.get("_执行标记")
            or session.get("result_version") != row.get("结果版本")):
        raise HandoffError("session_conflict", "自动回写须绑定原工作区当前待验收的执行会话。")
    flow._check_evidence(row)
    jid = digest([flow.key(rid), session["result_version"]])
    existing = jobs(flow.state).get(jid)
    if existing and existing["status"] == "stopped":
        raise HandoffError("auto_accept_stopped", "本版本自动回写因校验失败已停止；先排查并重新验证提交新版本。",
                           reason=existing["reason"])
    if existing and existing["status"] in ("cancelled", "expired"):
        existing.update(status="waiting", reason="confirmation_missing", expires_at=time.time() + MAX_WAIT_SECONDS)
    session["auto_accept"] = True
    flow.state.save()
    return register(flow)


def cancel(flow, rid):
    session = flow.session(rid)
    session["auto_accept"] = False
    for job in jobs(flow.state).values():
        if job["namespace"] == flow.namespace and job["record_id"] == rid and job["profile"] == flow.identity["profile"]:
            if job["status"] == "waiting":
                job.update(status="cancelled", reason="cancelled_by_processor", updated_at=now())
    flow.state.save()
    return {"record_id": rid, "auto_accept": "cancelled", "remote_modified": False}


def finish(state, job, status, reason):
    job.update(status=status, reason=reason, updated_at=now())
    state.save()
    return {"job_id": job["id"], "record_id": job["record_id"], "result_version": job["version"],
            "status": status, "reason": reason}


def step(flow, jid):
    """One locked attempt. accept/sync retain every existing verification guard."""
    job = jobs(flow.state).get(jid)
    if not job:
        raise HandoffError("auto_accept_missing", "找不到本机自动回写记录。")
    if job["status"] != "waiting":
        return finish(flow.state, job, job["status"], job["reason"])
    if time.time() >= job["expires_at"]:
        return finish(flow.state, job, "expired", "wait_expired")
    own = []
    try:
        if (job["namespace"] != flow.namespace or job["profile"] != flow.identity["profile"]
                or job["workspace"] != digest(str(flow.git.root))):
            raise HandoffError("identity_or_workspace_changed", "自动回写的账号、表格或原工作区已变化。")
        rid = job["record_id"]
        session = flow.session(rid)
        if (session.get("token") != job["token"] or session.get("result_version") != job["version"]
                or session.get("requirement") != job["requirement"]):
            raise HandoffError("session_conflict", "自动回写绑定的原执行会话已变化。")
        if not session.get("auto_accept"):
            return finish(flow.state, job, "cancelled", "cancelled_by_processor")
        # Recover only this submission's completion, never another queued operation.
        pending = flow._pending_operations()
        own = [op for op in pending if op["record_id"] == rid and op["profile"] == job["profile"]
               and op.get("patch", {}).get("状态") == "已完成" and op.get("acceptance_binding")
               and op["before"].get("结果版本") == job["version"]
               and (op.get("session") or {}).get("token") == job["token"]]
        if own:
            flow.sync(operation_ids=[op["id"] for op in own])
            session = flow.session(rid)
        row = flow.tickets.get(rid)
        flow.check_owner(row)
        if row.get("结果版本") != job["version"] or requirement(row) != job["requirement"]:
            raise HandoffError("review_version_changed", "交付或需求版本已变化，旧自动回写已停止。")
        if row.get("状态") == "已完成" and session.get("phase") == "completed":
            return finish(flow.state, job, "completed", "accepted")
        if row.get("状态") != "待验收":
            raise HandoffError("not_pending_acceptance", "工单已退回或状态变化，自动回写已停止。")
        if flow._pending_operations():
            return finish(flow.state, job, "waiting", "sync_required")
        report = flow.review(rid)
        issues = [item["code"] for item in report["blocking"]]
        hard = [code for code in issues if code not in WAITING | TRANSIENT]
        if hard:
            return finish(flow.state, job, "stopped", hard[0])
        if issues:
            reason = next((code for code in issues if code in TRANSIENT), issues[0])
            return finish(flow.state, job, "waiting", reason)
        if not report["can_finalize"]:
            return finish(flow.state, job, "stopped", "original_workspace_required")
        flow.accept(rid, job["version"])
        return finish(flow.state, job, "completed", "accepted")
    except HandoffError as exc:
        # A lost completion response is recovered by the existing journal on the next tick.
        recoverable = any(op["record_id"] == job["record_id"] and op.get("acceptance_binding")
                          for op in flow._pending_operations())
        if exc.code in TRANSIENT or (exc.code == "write_unknown" and recoverable and not own):
            return finish(flow.state, job, "waiting", exc.code)
        return finish(flow.state, job, "stopped", exc.code)


def lock_path(state, jid):
    if len(jid) != 64 or any(c not in "0123456789abcdef" for c in jid):
        raise HandoffError("invalid_auto_accept_id", "自动回写记录 ID 不合法。")
    return state.directory / "auto-accept" / (jid + ".lock")


def running(state, jid):
    try:
        with exclusive_lock(lock_path(state, jid)):
            return False
    except HandoffError as exc:
        if exc.code == "local_busy":
            return True
        raise


def status(state, namespace, rid=None):
    return [{"record_id": job["record_id"], "result_version": job["version"],
             "status": job["status"], "reason": job["reason"],
             "worker_running": running(state, job["id"]), "expires_at": job["expires_at"]}
            for job in jobs(state).values() if job["namespace"] == namespace and (rid is None or job["record_id"] == rid)]


def launch(repo, state_dir, job):
    state = State(Git(repo), state_dir)
    if running(state, job["id"]):
        return {"record_id": job["record_id"], "worker": "running"}
    command = [sys.executable, str(Path(__file__).with_name("handoff.py")), "--repo", str(repo),
               "--state-dir", str(state_dir), "--table", job["table"], "_auto-accept-worker", job["id"]]
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, cwd=str(repo), **options)
    except OSError:
        return {"record_id": job["record_id"], "worker": "start_failed", "reason": "process_start_failed"}
    # Reap children when execute() is embedded in a longer-lived Python host.
    threading.Thread(target=child.wait, daemon=True, name="handoff-auto-accept-reaper").start()
    # A spawn attempt is not evidence that the worker is online.
    for _ in range(20):
        if running(state, job["id"]):
            return {"record_id": job["record_id"], "worker": "running", "pid": child.pid}
        if child.poll() is not None:
            return {"record_id": job["record_id"], "worker": "exited", "exit_code": child.returncode}
        time.sleep(0.05)
    return {"record_id": job["record_id"], "worker": "starting", "pid": child.pid}


def worker(args, execute_step, sleep=time.sleep):
    git = Git(args.repo)
    state_dir = args.state_dir
    initial = State(git, state_dir)
    try:
        with exclusive_lock(lock_path(initial, args.job_id)):
            failures = 0
            while True:
                # Check cancellation/expiry locally even while the service is unreachable.
                try:
                    with exclusive_lock(initial.lock_path):
                        state = State(git, state_dir)
                        job = jobs(state).get(args.job_id)
                        if not job or job["status"] != "waiting":
                            return {"worker": "stopped"}
                        if time.time() >= job["expires_at"]:
                            return finish(state, job, "expired", "wait_expired")
                    result = execute_step(args)
                    if result["status"] != "waiting":
                        return result
                    failures = failures + 1 if result["reason"] in TRANSIENT else 0
                except HandoffError as exc:
                    failures += 1
                    if exc.code != "local_busy":
                        try:
                            with exclusive_lock(initial.lock_path):
                                state = State(git, state_dir)
                                job = jobs(state).get(args.job_id)
                                if not job or job["status"] != "waiting":
                                    return {"worker": "stopped"}
                                result = finish(state, job, "waiting" if exc.code in TRANSIENT else "stopped", exc.code)
                                if result["status"] != "waiting":
                                    return result
                        except HandoffError as save_error:
                            if save_error.code != "local_busy":
                                raise
                sleep(min(POLL_SECONDS * 2 ** min(failures, 3), 300))
    except HandoffError as exc:
        if exc.code == "local_busy":
            return {"worker": "already_running"}
        raise
