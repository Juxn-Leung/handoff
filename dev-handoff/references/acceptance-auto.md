# 真实双账号自动回写联调

本流程只测试隔离夹具上的“指定账号确认 → 原工作区后台自动完成”。普通业务仍说“验收工单 ID”。退回重提测试使用 acceptance_dual.py；这里通过后不主动退回，不手动调用 accept 或 worker step 代替后台。

两端各自使用本人已授权项目，双方已登记到同一共享表；从 members 唯一匹配另一真实账号。测试脚本将暂存固定的 service.py 夹具，验证列表、空态与失败态，不操作真实业务代码。工单显式标记“非业务验收”。

## 处理方准备

```bash
python3 "$SKILL_DIR/scripts/acceptance_auto.py" prepare \
  --repo "$REPO" --run-dir "$REPO/.dev-handoff-local/auto-acceptance/本轮名称" \
  --table-url '双方共享表URL' --reviewer-id 'members中的真实账号ID' --execute
```

prepare 保存恢复检查点和运行代码快照，发布独立测试单、领取并开始，执行夹具检查，然后调用冻结版本的标准 CLI ready，使用其默认后台启动路径。隔离进程只在内存中继承处理方本人授权，Token 不写入夹具、公开资料或命令参数。两端脚本指纹均须与工单匹配；冻结后不要修改快照或共享指纹来迁就其他版本。

后台通常每 60 秒检查，最长等待 7 天；原电脑需运行且网络可用。准备完成必须看到 auto_accept 中 worker_running 为 true；启动失败要排查并在同一目录重试，不能新建重复工单。短暂 local_busy 表示后台占用原工作区，稍后重试。未读取另一账号的真实确认与完成记录前，报告“等待 codedroll 验收”，不能报告联调通过。

## 验收方一句话

更新技能并让 AI 重新读取 SKILL.md 后，在本人已授权项目说：

```text
$dev-handoff 对共享表 〈URL〉执行自动回写联调 〈工单ID〉。
```

AI 调用：

```bash
python3 "$SKILL_DIR/scripts/acceptance_auto.py" reviewer \
  --repo "$REPO" --table-url '双方共享表URL' --record-id '工单ID' --execute
```

脚本从表中读取公开计划并固定版本，检查真实账号、测试类型、运行指纹和代码证据，在验收方隔离工作区执行技能内置可信夹具。三种行为都通过才写入逐项确认；检查执行失败会退回。账号、版本、证据或环境不符合时停止并报告原因，不伪造通过，也不执行工单提供的任意脚本。确认后共享状态可能暂时仍是“待验收”，等待处理方后台回写。无需 zip、Token 或处理方本地恢复状态。

## 处理方只读核验

验收方完成后，在原聊天说：

```text
$dev-handoff 查看自动回写联调结果 〈工单ID〉。
```

从本项目 `.dev-handoff-local/auto-acceptance/*/run.json` 按 record_id 找到唯一对应 RUN_DIR，使用该轮冻结代码：

```bash
python3 "$RUN_DIR/runtime/dev-handoff/scripts/acceptance_auto.py" audit \
  --repo "$REPO" --run-dir "$RUN_DIR"
```

audit 不同步写入、不调用 accept、不执行后台单步。它核对同版本指定账号的确认、原工作区代码、后台任务 completed、原会话 completed、共享已完成状态、实际验收人和唯一一条处理方完成日志。全部满足后才生成 phase=complete 的本机 report.json；否则报告等待或异常。该报告证明本轮协议联调，不代表业务验收。

若后台进程离线且任务仍为 waiting，可用本轮冻结 acceptance_auto.py 重跑相同 prepare 命令，恢复已启用任务；它仍通过标准自动回写入口启动。已停止、退回或代码发生变化时先排查，不修改旧证据、不用手动 accept 让本轮看起来成功。
