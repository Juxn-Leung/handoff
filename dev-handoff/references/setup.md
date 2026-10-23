# 安装、个人授权和双账号共享

## 安装

通过 npm / npx 从 Git 安装：

```sh
npx skills@latest add Juxn-Leung/handoff --skill dev-handoff -a codex
npx skills@latest add Juxn-Leung/handoff --skill dev-handoff -a claude-code
```

加 `-g` 安装为个人通用技能。安装需要 Node.js 22.20+。详情见 [skills CLI 官方说明](https://github.com/vercel-labs/skills)。技能运行需要 Python 3.10+、Git 和腾讯文档网络访问；使用 Python 标准库调用官方 MCP。

已有安装在原项目执行 `npx skills@latest update dev-handoff -p`；原为全局安装则使用 `npx skills@latest update dev-handoff -g`。仅更新本技能，按原安装范围操作；之后重新读取实际安装目录中的 SKILL.md。命令依据 [skills CLI 更新说明](https://github.com/vercel-labs/skills/blob/main/docs/commands.md#skills-update)。更新技能不删除或重建项目授权、绑定、工单和个人检查点。联调中还须核对工单记录的运行指纹，不能把“已更新”当成版本一致的证据。

下文 `SKILL_DIR` 是当前 SKILL.md 所在目录，`REPO` 是本人实际开发仓库绝对路径。命令由 AI 调用，操作者主要使用聊天和浏览器。所有 `--repo`、`--table` 等全局参数必须放在子命令前。

## 同事接入：一条指令

表格所有者先在腾讯文档“分享”面板把同事的个人 QQ／微信账号添加为可编辑协作者。同事安装技能后，在本人实际开发仓库输入：

```text
$dev-handoff 接入 <真实腾讯智能表URL>
```

Claude Code 将开头换成 `/dev-handoff`。别名和分工都可省略。需要授权时，同事只需打开 AI 给出的链接，用被邀请的账号点击“确认授权”，显示“已授权”后回复“已授权”。AI 随后继续绑定、登记和检查，无须再输入“绑定”“加入”“查看成员”等指令。已经授权的账号直接继续。

AI 按以下流程执行：

1. `doctor` 核对本人账号。缺少 Token 或鉴权已失效时，按下节 `auth-start` 展示授权链接；保留本次表格 URL、可选别名和分工，等待本人明确回复授权完成。不要把用户最初要求“接入”视为网页授权已完成。
2. 本人确认后执行 `auth-finish --confirmed` 和 `doctor`，接着恢复本次接入，不再询问是否绑定或加入。网络或权限错误按实际原因处理，不反复触发授权。
3. 未指定别名时，优先复用这个 URL 的已有绑定别名，否则使用“开发对接”。`bind URL 别名` 验证并保存绑定；别名冲突时询问新别名，不替换其他表。结构缺失时报告具体问题，不能自动 `--repair` 或建新表。
4. 对本次表执行 `join`。用户指定分工时传入 `--role`；否则保留已有登记分工，新成员留空。只有返回 `choose_role` 时才展示共享选项让本人选择，然后继续。
5. 对本次表执行 `members` 和 `queue`，确认当前账号恰有一条成员记录，队列读取完整。重复接入复用原绑定和成员记录，写入结果未知时先回读恢复，不重复创建成员。
6. 全部成功后简要报告“已接入”、本人昵称、表格链接和队列概况；队列为空也是正常结果。某步失败时报告已完成阶段和所需动作，不把部分成功称为接入完成。不自动领取、开始或验收工单。

`join` 的回读证明本人能写成员表，队列读取证明本人能访问工单；接入成功还不能代替后续的真实工单领取、处理和验收联调。双方成员是否都已登记可从结果判断，不要求同事另行整理账号 ID。

## 每个人各自授权

先运行 `python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" doctor` 读取本人资料。

若返回 `tencent_token_missing`，执行：

```sh
python3 "$SKILL_DIR/scripts/tencent_mcp.py" --repo "$REPO" auth-start
```

把返回的 authorization_url 展示成可点击链接。操作者用自己的个人 QQ／微信登录，点击“确认授权”，直到页面显示“已授权”。仅登录腾讯文档首页不代表已完成这次授权。

**操作者明确回复“已授权”或“已完成授权”后**，执行：

```sh
python3 "$SKILL_DIR/scripts/tencent_mcp.py" --repo "$REPO" auth-finish --confirmed
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" doctor
```

链接约 5 分钟有效。未授权时保留原链接等待本人完成，不持续轮询；过期后再 auth-start。网页授权成功后脚本自动获取 Token 并保存，不需要复制到聊天。

备用个人 Token 页面：[MCP 管理](https://docs.qq.com/open/auth/mcp.html)、[个人账号接入](https://docs.qq.com/scenario/open-claw.html)。已手动复制 Token 时，可让操作者在自己的交互终端执行 `tencent_mcp.py --repo "$REPO" configure`，在隐藏输入提示中粘贴。不要把 Token 放在命令参数、共享文档或 Git 里。也支持本人进程环境变量 TENCENT_DOCS_TOKEN；优先级高于本地文件。

Token 在本人仓库 `.dev-handoff-local/tencent-token.json`，权限 0600，父目录 0700。私有目录内自动创建忽略文件，排除凭据、授权检查点和恢复状态。不得读取并展示文件正文，也不将此目录发给同事。跨项目授权可分别进行，不自动复制凭据。

## 创建与绑定

创建包含“工单、跟进记录、成员”的智能表；默认不限定分工：

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" create 项目名 --alias 订单
# 需要固定的可选分工时，在建表时指定自己的选项
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" create 项目名 --alias 订单 --roles 前端 后端 测试
```

已有表从真实腾讯智能表 URL 绑定：

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" bind '真实智能表URL' 订单
```

如果操作者明确要求补齐已有表的必要结构，使用 bind ... --repair。保留现有数据；多余字段在默认视图隐藏，类型、选项、关联目标有冲突时停止，不删除重建。

已有单账号联通测试表可以 bind --repair 补上正式三张工作表，原联通测试子表保留。原测试记录不自动冒充正式业务任务。旧平台的历史工单不自动复制；需要数据迁移时单独明确来源和目标。

`.dev-handoff.json` 仅记录非敏感的共享表 ID、URL 和别名。两个人可以在不同仓库绑定同一个表。一个绑定直接使用；多个绑定要明确 `--table 别名`。不按名称相似度默认选第一张。

## 仅邀请指定同事

表格所有者在腾讯文档右上角“分享”面板添加同事使用的个人账号，并设为“可编辑”。由所有者自己把链接发给同事。技能不自动发送消息。

MCP 的 manage.set_privilege 当前仅提供公开可读／公开可编辑，不适合精确邀请指定同事，所以技能不调用它。可用 `handoff.py ... access` 只读检查分享策略。policy=0 表示私密；policy=1 表示部分人可见；2／3 表示所有人可读／可编辑。指定成员的实际读写能力还要由同事本人账号验证。

成员登记和文档权限相互独立：成员表里有账号不代表平台允许该账号访问，文档可编辑也不代表已经登记为成员。对方绑定前须先获得平台文档编辑权限。

## 每人加入，可选分工

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" --table 订单 join
# 可选填写；若此表配置了选项，须从共享选项中选择
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$REPO" --table 订单 join --role 测试
```

join 仅登记当前 Token 返回的本人稳定账号 ID 和昵称，不替同事登记。分工可留空、自由填写，或使用建表时配置的共享选项。分工不决定发布、领取、分配和验收权限；同分工也可协作。任务归属由跟进人账号 ID 决定，分配由该工单的发布人操作。

目标默认工单表头：标题、状态、优先级、发布人、跟进人、交接说明、验收标准、前置依赖、处理结果。跟进记录显示工单、操作者、时间、内容；成员显示姓名、分工。ID、版本、证据、恢复字段隐藏保留。需要整理既有表时运行 `handoff.py ... layout`；只整理默认表格视图，不改自定义视图或删除记录。当前实测腾讯接口对列显隐返回 22032，建表会返回 layout.pending；不能据此宣称表头已整理，也不重建表。可在网页的字段显隐面板按上述列表手动隐藏；平台能力恢复后再运行 layout。

已有旧版表需显式 `bind ... --repair` 补齐“发布人、发布人ID、分工”等通用字段，再 join；旧前端／后端列保留并隐藏，不把它们猜成发布人。旧工单的发布归属需核实后填写，不能凭角色推断。

## 第二台电脑的接入验收

1. 同事安装同一版本技能；文档所有者添加同事为指定可编辑协作者。
2. 同事在自己的仓库输入“接入 URL”，需要时用被邀请的 QQ／微信完成授权并回复“已授权”。
3. AI 自动绑定同一个 URL、登记本人账号并检查成员与队列，报告接入结果。
4. members 应读到双方账号和分工；发布方从暂存区发布一条小型真实事项。
5. 接手方 queue 查看，show 阅读，claim 领取；发布方 show 核对跟进人 ID 已变为接手方。
6. 接手方 start 后实施和验证，ready 提交待验收；发布方查看状态、结果版本和跟进记录。
7. 新单在发布时指定实际验收人及逐项标准。验收人用自己的账号在自己的工作区 review，核对步骤、预期与证据，明确通过指定版本后 confirm；此时仍为待验收。
8. 接手方回到原处理账号、原工作区，review、accept 复核同一版本并回写。双方读回“已完成”、实际验收人、确认时间和回写日志；跨工作区不能代验原代码。
9. 再走一轮指定版本退回、重新 start 和 ready、确认新版本、原工作区回写，检查旧确认不能复用。完整验收资料见 [acceptance.md](acceptance.md)。旧工单沿用原账号、原工作区的旧流程。

以上真实双账号跨设备链路未完成前，不宣称已真实验证双账号互通。离线的两个模拟客户端测试仅验证代码逻辑。

## 故障与恢复

- `tencent_not_authorized`：打开本次授权链接，点击确认直至“已授权”。不要只登录另一个 Token 页面。
- `tencent_unauthenticated`：重新授权；账号要与预期个人账号相同。
- `tencent_permission_denied`：检查邀请的个人账号和编辑权限，不改用同事 Token。
- `join_required`：绑定后用本人账号 join，核对共享成员 ID。
- 会员／额度错误：展示平台实际返回限制，由操作者决定，不自动购买。
- 网络错误：检查本机网络。执行环境禁止联网时申请该脚本的联网执行权限。
- 建表、补字段结果未知：保留检查点，先回读原表。创建超时不能换名重复建表。
- 处理中写入失败：按 processing.md 运行 sync；人工修改冲突须先核实，不删除状态目录绕过。

官方参考：[腾讯文档 MCP](https://developer.cloud.tencent.com.cn/mcp/server/11803)、[个人开发者说明](https://developer.cloud.tencent.com/article/2636993)。授权链接流程依据[官方技能包](https://cdn.addon.tencentsuite.com/static/tencent-docs.zip)；本技能不执行其安装脚本或输出 Token 调试日志。
