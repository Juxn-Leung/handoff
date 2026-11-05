# 按需真实双人对话联调

仅用于用户明确要求“按需双人联调／发布、追问、处理到验收联调”。两位使用者在各自电脑、本人腾讯账号和 AI 中操作。每阶段一次调用，完成即停；不使用 acceptance_dual.py 的主动退回流程，不使用后台联调，不代替另一真人账号。

## 先更新，再启动

先确认本轮版本已经可以从分发源安装。若更新只在本地，不能让同事运行普通 update 后声称拿到了新版。使用本轮给出的固定 GitHub 提交 URL，按原安装范围更新 dev-handoff；项目安装不加 -g，全局安装加 -g，不覆盖其他技能。更新后重新定位实际安装目录，重读 SKILL.md 与本文件，然后运行：

```sh
python3 "$SKILL_DIR/scripts/on_demand_trial.py" fingerprint
```

返回的完整 runtime_fingerprint 须与本轮共享计划一致。两端不得在本轮中途再次切换版本；不一致先停止并更新，不跳过检查。若用户要先给出可复制指令，必须带明确分发源，不能只说“更新到最新”。

## 隔离与授权

用户本人为发布方和验收方，同事为处理方。既有账号和测试表从真实 members 与测试资源核对；不得根据昵称猜 ID。测试表须已有双方可访问的分享方式，未授权不能扩大分享；接入失败定位到具体表或文档后停止，保留原事项。

每端选择独立 RUN_DIR，并令 REPO 为本人已经授权的开发仓库。初始化只在新隔离工作区建立基线提交，不改 REPO 里的业务文件。Token 只经进程内存传给单次 CLI，不复制到隔离目录、报告或命令参数。

```sh
python3 "$SKILL_DIR/scripts/on_demand_trial.py" init --repo "$REPO" --run-dir "$RUN_DIR" --expected-runtime "$RUNTIME"
python3 "$SKILL_DIR/scripts/on_demand_trial.py" run --repo "$REPO" --run-dir "$RUN_DIR" -- bind-link-group "$TABLE_URL" 按需双人联调
python3 "$SKILL_DIR/scripts/on_demand_trial.py" run --repo "$REPO" --run-dir "$RUN_DIR" -- join
```

上述 bind-link-group 仅用于原本就采用链接编辑的测试表；定向分享组用 bind-group。每次 run 的回执含实际账号与脚本指纹；本地 calls.jsonl 记录起止时间和命令名，调用结束没有后台工作。后文 CLI 命令都经此 run 入口调用；它拒绝后台命令和 --auto-accept。

## 一个事项，分六次调用

1. **发布方发布。** 从 Markdown 发布明确标记“按需双人联调，非业务交付”的一条事项。交付文件为 order-labels.json，字段 separator、empty_text、error_text。初始约定允许 separator 为英文逗号或顿号，要求处理方交付前向发布方确认选择；空态为“暂无订单”，失败态为“加载失败，请重试”。这是明确保留的文案选择，不猜测业务接口。delivery 指定发布方为 reviewer_id，使用 kind=local 的 JSON 检查和逐项标准；verification_run 存 kind=on_demand_conversation_v1、run_id、runtime_fingerprint、publisher_id、processor_id。发布后返回固定工单 ID、表与对话链接，已有同轮事项不得重建。
2. **处理方读取并追问。** 先更新、核对指纹、本人接入登记，再 inbox ID → show ID，核对实际账号为 processor_id、发布方为另一个真实账号。claim 后在同一文档 note 询问“列表编号使用英文逗号还是顿号？”。消息说明本人账号与本轮角色；此阶段不 start、不修改交付文件，发完停止。回执给出真实账号、事件 ID 与当前状态。
3. **发布方下一次调用时答复。** inbox ID → show ID，确实读到同事追问后回复“采用顿号，展示为 1、2”。用 update-agreement 与重新核对的 delivery 将唯一取值确定为顿号，保留本轮 verification_run，记录新需求版本，然后停止。不得在同事追问出现前假装已读取；不得在未调用期间轮询等待。
4. **处理方下一次调用时处理。** inbox ID → show ID，确实读到答复及同步后的约定。preflight、start 只声明 order-labels.json，在 RUN_DIR/workspace 写入本次 JSON；执行下方本地验证。ready 的 checks 和 acceptance 引用实际输出，证据包含文件完整文本和 SHA-256，以便另一端核验同一交付；不传 --auto-accept。停在待验收并报告完整结果版本。
5. **发布方下一次调用时验收。** inbox ID → show/review ID，固定结果版本。取证据中的完整 JSON 原文字节，保存到本端 RUN_DIR 下的独立 received 文件；不执行对方文档中的脚本。运行安装包内 verify，核对 SHA-256 与 _结果证据.code["order-labels.json"].sha256 一致，逐项观察为 list=1、2、empty=暂无订单、failure=加载失败，请重试，再按原验收规则 confirm。明确失败则 reject；无证据或无法验证则保持待验收。不代替同事 accept，确认后停止。
6. **处理方下一次调用时完成。** inbox ID → review ID，核对指定验收人的同版本完整确认、原会话、原代码及依赖，再 accept 回写。回读已完成、实际验收人和日志后停止。发布方可再单次只读检查最终结果，不触发后续事项。

## 检查交付

本轮固定选择顿号后，两端均使用已安装的可信检查器：

```sh
python3 "$SKILL_DIR/scripts/on_demand_trial.py" verify "$ARTIFACT" --separator '、'
```

检查器只解析 JSON，不执行交付内容。输出完整文本、字节 SHA-256 和三种展示结果。处理方提交时使用真实输出；验收方从共享证据恢复相同字节，再对比原代码指纹。不能把处理方说“通过”直接当作验收观察，也不能另写一份正确 JSON 代替核验收到的交付。

## 本轮如何判定成功

共享表保留一条事项、文档保留完整追问与答复，处理者和验收者是两个不同真实账号，运行指纹一致。需求更新、交付与确认绑定正确版本；最终由同事原工作区完成回写。每次调用都有实际回执，没有常驻进程、监听或自动阶段。

在处理方停在待验收后，以及发布方确认后，可让双方停止调用一段由人控制的时间，再由下一阶段单次读取验证没有自行推进。不运行 sleep 轮询来“证明”没有监听。没有变化只证明该观察区间没有状态推进；结合代码默认不启动后台、两端调用记录与本机进程检查报告验证范围。两个人尚未分别完成各阶段前，只报告当前已完成阶段和下一位参与者，不写“真实双人联调通过”。
