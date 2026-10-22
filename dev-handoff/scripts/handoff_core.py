"""Local, deterministic parts of dev-handoff (Python 3.10+, standard library)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from datetime import datetime, timezone


class HandoffError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def table_time():
    """Shared table timestamps use minute precision for stable date round trips."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise HandoffError("invalid_local_state", f"无法读取 JSON 文件：{path}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def exclusive_lock(path):
    """OS releases this advisory local lock even when a process crashes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise HandoffError("local_busy", "当前工作区已有 dev-handoff 操作正在执行。") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Git:
    def __init__(self, path):
        self.root = Path(self.run_at(path, ["rev-parse", "--show-toplevel"]).strip()).resolve()

    @staticmethod
    def run_at(path, args, check=True, binary=False):
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(path), *args],
                                capture_output=True, timeout=30)
        if check and result.returncode:
            raise HandoffError("git_error", "Git 只读操作失败，请检查仓库及路径。", command=args[:2])
        if not check and result.returncode:
            return None
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    def run(self, args, **kwargs):
        return self.run_at(self.root, args, **kwargs)

    def clean_paths(self, paths):
        result = []
        for name in paths:
            path = PurePosixPath(name)
            if not name or path.is_absolute() or ".." in path.parts or "\\" in name or path.parts[0] == ".git":
                raise HandoffError("invalid_path", "只接受仓库内的相对文件路径。", path=name)
            resolved = (self.root / name).resolve()
            if not resolved.is_relative_to(self.root):
                raise HandoffError("invalid_path", "文件路径或符号链接指向仓库外。", path=name)
            result.append(path.as_posix())
        return sorted(set(result))

    def head(self):
        return (self.run(["rev-parse", "--verify", "HEAD"], check=False) or "").strip() or None

    def staged(self):
        files = self.run(["diff", "--cached", "--name-only", "--no-renames", "-z"], binary=True)
        paths = [x.decode("utf-8", errors="strict") for x in files.split(b"\0") if x]
        if not paths:
            raise HandoffError("empty_index", "暂存区没有改动；请先暂存要发布的功能。")
        # ls-files is read-only; unlike write-tree it does not create objects.
        index = self.run(["ls-files", "--stage", "-z"], binary=True)
        head = self.head()
        snapshot = {"base_commit": head, "index_hash": hashlib.sha256(index).hexdigest()}
        snapshot["fingerprint"] = digest(snapshot)
        snapshot.update({"branch": self.run(["branch", "--show-current"]).strip(),
                         "paths": paths, "availability": "staged_only", "captured_at": now()})
        return snapshot

    def staged_context(self, paths=None):
        snapshot = self.staged()
        if paths is not None:
            paths = self.clean_paths(paths)
            if not paths or not set(paths).issubset(snapshot["paths"]):
                raise HandoffError("invalid_scope", "上下文路径必须属于本次暂存改动。")
        else:
            paths = snapshot["paths"]
        diff = self.run(["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "--", *paths])
        return {"snapshot": snapshot, "diff": diff}

    def index_file(self, name):
        name = self.clean_paths([name])[0]
        data = self.run(["show", ":" + name], binary=True)
        if b"\0" in data:
            raise HandoffError("binary_file", "二进制暂存文件不能作为文本接口上下文。")
        return data.decode("utf-8", errors="strict")

    def code(self, paths):
        result = {}
        for name in self.clean_paths(paths):
            path = self.root / name
            if path.is_symlink():
                value = {"symlink": os.readlink(path)}
            elif path.is_file():
                value = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "executable": bool(path.stat().st_mode & 0o111)}
            elif not path.exists():
                value = {"missing": True}
            else:
                raise HandoffError("invalid_path", "处理范围必须是文件，不能是目录。", path=name)
            result[name] = value
        return result

    def baseline(self):
        tracked = self.run(["ls-files", "-z"], binary=True)
        untracked = self.run(["ls-files", "--others", "--exclude-standard", "-z"], binary=True)
        paths = [x.decode("utf-8", errors="strict") for x in (tracked + untracked).split(b"\0") if x]
        # Inspect links as links; do not follow external links merely to inventory a repo.
        result = {}
        for name in sorted(set(paths)):
            path = self.root / name
            if path.is_symlink():
                result[name] = {"symlink": os.readlink(path)}
            elif path.is_file():
                result[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "executable": bool(path.stat().st_mode & 0o111)}
            elif not path.exists():
                result[name] = {"missing": True}
        return result


class State:
    def __init__(self, git, state_dir=None):
        self.project_path = git.root / ".dev-handoff.json"
        base = Path(state_dir) if state_dir else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dev-handoff"
        self.directory = base / digest(str(git.root))[:24]
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "operation.lock"
        self.data = read_json(self.path, {"version": 1, "identities": {}, "sessions": {},
                                         "batches": {}, "operations": {}, "last": {}})
        self.project = read_json(self.project_path, {"version": 1, "bindings": []})

    def save(self):
        write_json(self.path, self.data)

    def save_project(self):
        write_json(self.project_path, self.project)

    def select(self, selector=None):
        bindings = self.project["bindings"]
        found = [b for b in bindings if selector in (b["alias"], b["url"], b["base_id"])] if selector else bindings
        if len(found) != 1:
            raise HandoffError("choose_table" if found else "table_not_bound",
                               "请选择当前项目的工单表；新 URL 请先绑定。",
                               candidates=[{k: b[k] for k in ("alias", "url", "base_id")} for b in found or bindings])
        return found[0]


REQUIREMENT_FIELDS = ("标题", "描述", "变更类型", "交接说明", "验收标准", "来源", "关联工单", "前置依赖")


def requirement(record):
    return digest({k: record.get(k) for k in REQUIREMENT_FIELDS})


def user_ids(value):
    if not isinstance(value, list):
        return set()
    return {v["userId"] for v in value if isinstance(v, dict) and v.get("userId")}


def owned(record, identity):
    users = record.get("跟进人") or []
    return len(users) == 1 and isinstance(users[0], dict) and users[0].get("userId") == identity["user_id"] and users[0].get("corpId", identity["corp_id"]) == identity["corp_id"]


def plan_queue(records, identity, last=None):
    by_id = {r["id"]: r for r in records}
    cyclic, visiting, visited = set(), [], set()

    def visit(rid):
        if rid in visiting:
            cyclic.update(visiting[visiting.index(rid):])
            return
        if rid in visited or rid not in by_id:
            return
        visiting.append(rid)
        for dep in by_id[rid].get("前置依赖") or []:
            visit(dep)
        visiting.pop()
        visited.add(rid)

    for rid in by_id:
        visit(rid)
    groups = {"ready": [], "pending_acceptance": [], "blocked": [], "in_progress": []}
    previous = by_id.get(last, {})
    for r in records:
        if not owned(r, identity) or r.get("状态") == "已完成":
            continue
        item = dict(r)
        status = r.get("状态")
        missing = [dep for dep in r.get("前置依赖") or [] if by_id.get(dep, {}).get("状态") != "已完成"]
        if status == "待验收":
            groups["pending_acceptance"].append(item)
        elif status == "处理中":
            groups["in_progress"].append(item)
        elif status == "阻塞" or missing or r["id"] in cyclic:
            item["queue_reason"] = "循环依赖" if r["id"] in cyclic else "前置依赖未完成" if missing else "工单已阻塞，需明确恢复"
            item["waiting_for"] = missing
            groups["blocked"].append(item)
        elif status == "待对接":
            groups["ready"].append(item)
        else:
            item["queue_reason"] = "未知状态，需人工确认"
            groups["blocked"].append(item)
    related = set(previous.get("关联工单") or [])
    priorities = {"紧急": 0, "普通": 1, "低": 2}
    groups["ready"].sort(key=lambda r: (
        0 if r["id"] in related or last in (r.get("关联工单") or []) else 1,
        priorities.get(r.get("优先级"), 1), r.get("创建时间") or "", r["id"]))
    groups["counts"] = {k: len(v) for k, v in groups.items()}
    groups["recommended"] = groups["ready"][0]["id"] if groups["ready"] else None
    return groups
