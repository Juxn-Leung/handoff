# 单条处理、验收和恢复

以下 `$RUN` 代表 `python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" --table "$TABLE"`。
实际工具调用时展开为独立参数，不把表格内容拼成 shell 命令，不用 eval。

## 选择与开始

1. `queue` 精简列出本人处理事项、未分配候选和本人需验收的他人交付（awaiting_review）。claimable 为依赖已完成且当前预检无已知阻塞的可领取项，blocked 也包括未分配但受阻的候选项。依赖缺失／循环依赖进入阻塞清单，已完成事项不重复执行。未分配事项先 show，再 claim 工单ID --requirement 需求指纹；领取后仍须 start 才能编辑。
2. 有本工作区处理中工单就恢复它；远端执行标记不属于本地会话时停止，不能强占。原会话丢失须与操作者确认恢复办法，不能删除状态文件来绕过。
3. 推荐排序为：只选前置依赖已完成的事项，再优先上一条的关联事项，最后按优先级和创建时间。
4. `show 工单ID` 获取完整需求、delivery、requirement_version 与 current_blocking。核对本单交付边界、验证证据提供方、验收责任和外部开始／验收门槛；检查当前仓库／模块、代码与环境是否能够承接，不清楚先问。
5. 提前预检接手方计划修改文件，再以相对仓库根目录的路径启动：

```sh
$RUN preflight rec真实ID --paths src/api.ts src/orders.ts --requirement 'show 返回的需求指纹'
$RUN start rec真实ID --paths src/api.ts src/orders.ts --requirement 'show 返回的需求指纹'
```

preflight 只读取远端并保存本地计划，不领取、不改工单状态、不加文件锁、不授权编辑。范围会在后续 queue／show 中用于实时冲突检查，需求变化会提示重新预检。`scope_checked: false` 表示没有可用文件计划，不能据队列就绪断言没有重叠。

处理多条候选事项前，可在仓库外写计划文件提前发现共享文件：

```json
{"plans": [
  {"record_id": "rec真实A", "requirement_version": "show 返回的指纹", "paths": ["src/shared/dictionary.ts", "src/apps/a.ts"]},
  {"record_id": "rec真实B", "requirement_version": "show 返回的指纹", "paths": ["src/shared/dictionary.ts", "src/apps/b.ts"]}
]}
```

执行 `$RUN preflight --plan /临时目录/plans.json`。`planned_overlap` 提示候选计划重叠，安排共享修改归属或顺序；它不自动创建业务依赖。`current_blocking.files` 是当前已被处理中、待验收或本地阻塞会话保护的文件，必须先处理保护条件。

成功后才能编辑。开始时保存工作区已有代码基线，不重置、暂存或清除操作者已有改动。
动态发现还要修改文件时，在修改前运行 `$RUN extend rec真实ID --paths 额外文件`。
已知会影响待验收工单的公共契约、配置、接口或共享模块时，即使路径未重叠也先暂停并解释。
脚本提供文件重叠保护；AI 负责补充语义上的影响判断。纯“相关”不等于可以跳过待验收保护。

## 精简队列

```sh
$RUN queue --app admin-app --limit 20
$RUN queue --app admin-app --app orders-app --limit 20 --offset 20
```

默认最多返回 20 条摘要（上限 100），只显示标题、应用和交付范围、状态、优先级、简短阻塞原因、当前关联状态摘要及下一步，不返回完整需求、处理结果或证据。`counts` 是筛选后各组总数，`total_counts` 是未筛选总数；`page.shown/remaining/next_offset` 表示本页和后续数据，分页不是读取不完整。

应用按明确标识匹配，多个 `--app` 为任一匹配，`apps: ["*"]` 的全局共享事项始终包含。先读取完整依赖再筛选，跨应用依赖不会因此丢失。旧工单未声明应用时不猜范围；`unscoped_count` 提醒仍有范围未知事项，要用不带筛选的 queue 和 show 核对。详细需求用 show，验收证据用 review。

## 逐步开发与阻塞

只实现当前工单。读取项目规范，做必要验证；不要顺手实现其他关联工单。
遇到关键歧义先向操作者提问；没有答案就保留当前事项，不能猜契约继续。
无法推进时：

```sh
$RUN block rec真实ID --reason '缺少测试环境鉴权' --attempted '已检查配置和接口文档' --needed '提供测试环境的授权方式，不要直接发送令牌'
```

这会追加带人员和时间的记录并转为阻塞。汇报剩余数量后停止。
可机器核对的阻塞要明确结构，而非把旧结果版本写成长期操作指引：

```sh
$RUN block rec真实ID --kind files --paths src/shared/dictionary.ts --waiting-for rec保护方ID --reason '共享字典受待验收结果保护' --attempted '已预检并确认重叠' --needed '核对并验收保护事项的当前结果'
$RUN block rec真实ID --kind dependency --waiting-for rec前置ID --reason '等待前置交付' --attempted '已核对依赖' --needed '前置事项验收通过'
```

`--waiting-for` 只用同表真实 ID；文件阻塞的跨表保护由预检读取本地记录提示，不编造同表关联。`--paths` 只记录受阻计划，不扩大可编辑范围。处理结果保存结构化阻塞，跟进日志保留原因、尝试、所需动作与当时版本。

queue／show 每次按本次完整读取的状态、结果版本和文件保护计算提示；旧版本留在历史中，不能据此要求验收过期结果。条件消失只提示可恢复，不自动恢复或验收。其他表只有本地会话记录时 `freshness: local_only`，需到对应表核对，不能声称已读取其最新版本。旧版纯文字阻塞仍保留，通过重新 preflight 补充当前文件信息；无法核对的人工条件继续显示待核对。

阻塞解除后，操作者说明解除条件，执行 `$RUN resume rec真实ID --reason '已补齐鉴权说明'`，再重新阅读和 start。
不自动轮询或给同事发送消息。

## 提交待验收

结果 JSON 存在仓库外：

```json
{
  "summary": "订单页已接入列表接口，包含加载、空列表和失败重试。",
  "checks": [
    {"id": "ui-states", "kind": "mock", "command": "项目实际执行的页面状态测试命令", "outcome": "passed"},
    {"command": "真实环境联调", "outcome": "not_run", "reason": "尚未提供测试环境"}
  ],
  "acceptance": [
    {
      "criterion_id": "list-and-errors",
      "actual": "列表、空态和失败重试符合约定",
      "evidence": [{"check_id": "ui-states", "content": "实际脱敏输出或观察记录：对应页面状态用例通过，退出码 0"}]
    }
  ]
}
```

验证必须如实记录。失败验证先修复或 block，不能把 failed 改写成 passed；无法执行需给出具体原因。
新工单的检查必须按 delivery.checks 的 id 与 kind 对应，必要项必须 passed；required 项 not_run 时先 block，不提交待验收。可附加没有 id 的其他验证记录，但不能拿它替代必要项。类型不能更换，mock 通过不代表 integration 通过。旧工单保持原验证格式兼容。

外部 `gate: none` 只披露未完成限制；`start` 未验收不能开始；`acceptance` 未验收可以提交本单待验收，但 review／accept 会拒绝通过。报告时明确本单结果与整个功能的完成程度，不在开发完成后削减原约定的验收范围。

```sh
$RUN ready rec真实ID /临时目录/result.json
```

脚本检查声明范围、需求、跟进人、执行标记，记录文件指纹与结果版本，只更新到待验收。
新发布工单还须按 delivery.criteria 在结果 acceptance 中逐项提供 actual 和 evidence（check_id、可阅读的脱敏 content、可选 reference）。不得只提供命令状态；每个关联检查都要有具体证据，材料共同参与本轮结果版本。完整格式见 [acceptance.md](acceptance.md)。
必须停止并报告：本条结果、验证与限制、结果版本、可处理 X／待验收 Y／阻塞 Z，以及下一条建议。
“继续”只授权开始下一条独立事项，不能自动验收本条。下一条也需 show、声明文件范围和 start。

## 人工验收

新工单指定 reviewer_id 并有逐项 criteria 时，采用 [逐项验收、退回和原工作区回写](acceptance.md)：实际验收人可在自己的工作区 review，明确通过指定版本后 confirm；工单仍为待验收，由原处理账号在原工作区复核同一版本的完整确认、代码指纹和依赖，再 accept 回写。最终验收人记录实际确认者，日志另记回写操作者。review 会展示阻塞和逐项材料，不能将 can_finalize=false 当成没有材料可验收。

以下是缺少 reviewer_id／criteria 的旧工单兼容流程，不自动给旧结果添加验收人或标准：

用户明确通过后，先确定其验收的是哪条工单、哪个已展示结果；多条待验收且目标不清就让用户选择。

```sh
$RUN review rec真实ID
$RUN accept rec真实ID --confirmed-version '操作者已审阅的结果版本'
```

review 是代码和需求的只读复核，不能替代人工验收。
代码变化时重新运行真实验证，`ready ... --revalidate` 生成新版本，并再次展示结果等待操作者明确通过。
需求变化时退回后重新处理。不能改写旧证据来让校验通过。
仅“继续”“测试通过”“AI 检查没问题”都不是验收指令。
“通过并继续”先完成 accept 和读回验证，再扫描下一条；accept 失败则暂停。

退回：`$RUN reject rec真实ID --reason '缺少错误重试'`，回到待对接，保留历史并列出关联和下游事项。
前置依赖被退回后，其下游不能再直接验收，需要重新检查依赖和结果。

## 写入失败与并发

`$RUN sync` 恢复同一工作区、同一账号的未完成操作。优先确认远端记录，再追加缺失日志并同步本地会话。
日志采用事件键避免重复追加。远端需求、人员或状态不匹配就停止，不覆盖手工改动。
同一工作区只能有一条“处理中”工单；可同时保留多条互不影响的待验收工单。
本地操作锁在进程结束后由系统释放；它不是跨机器分布式锁。不同机器不要同时处理同一工单。
跨聊天可恢复，跨机器验收要求回到原工作区，不能从别的仓库代验。

`sync` 因人工改动而无法恢复时，先向操作者展示冲突，不自动清检查点。
只有操作者明确决定放弃这次恢复，才用 `abandon-operation 操作ID --reason '操作者给出的原因'`。
此命令仅标记本地操作并保留历史，不修改远端、不验收或撤销工单；原工单状态仍须人工核实。
它允许继续其他独立事项，不能用来绕过同一工单的执行标记或待验收保护。
