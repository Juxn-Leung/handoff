# dev-handoff

目标是让多个使用者各自的 AI 能像协作同事一样对接：代表本人提出事项、读取对方的补充、澄清问题、在自己的工作区处理、提交证据并接续验收，减少使用者来回复制消息和解释上下文。腾讯文档是双方共享的事项记录；“一事项一页对话”服务于这条协作流程。

当前已实现共享事项、按需读取本人相关事项及更新、文档回复、领取处理和版本化交付验收。仅在调用技能时读取与接续，不做常驻或实时监听。读取位置保存在本人工作区，不代表对方已读；逐条消息的结构化接收人和共享已读尚未实现。能力边界与流程见 [按需对接](dev-handoff/references/markdown-group.md#按需对接入口)。

通过腾讯文档个人 QQ／微信账号共享开发项目组：一个事项一页对话，顶部放事项描述，下面按姓名、时间连续追加沟通、处理结果和验收反馈。同一事项的补充、退回和重提都留在原页；可独立验收的新事项另开一页。精简进度智能表用于查找事项、看状态和进入对话。后端继续用 `.md` 写改动和需求，AI 从中整理各事项；指定验收人授权 AI 按标准验收后，原处理工作区复核并回写状态。

成员不限前端或后端。分工默认可以留空，任务归属使用稳定账号 ID。建组会尝试让进度表只显示“标题、状态、跟进人、下一步、文档”；证据与恢复字段隐藏保留。当前腾讯接口在实测中可能拒绝显隐更新（22032）；遇到此情况会报告表头待整理，可在腾讯文档字段显隐面板手动整理。

## 使用 npm / npx 安装

需要 Node.js 22.20+（安装）、Python 3.10+、Git（运行）。在开发仓库运行：

```sh
# Codex，安装到当前项目
npx skills@latest add Juxn-Leung/handoff --skill dev-handoff -a codex

# Claude Code，安装到当前项目
npx skills@latest add Juxn-Leung/handoff --skill dev-handoff -a claude-code

# 两个工具都安装，所有项目可用
npx skills@latest add Juxn-Leung/handoff --skill dev-handoff -a codex claude-code -g
```

使用 npm 提供的 [skills CLI](https://github.com/vercel-labs/skills) 从本 Git 仓库安装完整技能。无需下载压缩包，无需单独发布 npm 包。

已安装的协作者在原项目更新本技能：

```sh
npx skills@latest update dev-handoff -p
# 原来全局安装的使用：npx skills@latest update dev-handoff -g
```

更新后让 AI 重新读取已安装的 `dev-handoff/SKILL.md`。更新方式见 [skills CLI 命令说明](https://github.com/vercel-labs/skills/blob/main/docs/commands.md)。真实双账号联调可直接输入“`$dev-handoff 对共享表 URL 的测试工单 ID 执行双账号联调第一轮，确认当前版本后退回`”；AI 按 [验收联调流程](dev-handoff/references/acceptance.md#更新已安装技能后继续联调)读取共享计划并使用匹配指纹的已安装代码，操作者无需另传 zip 或记住脚本命令。

## 开始使用

在实际开发仓库聊天输入：

```text
$dev-handoff 建组 订单项目，不限定成员分工
```

或者：

```text
$dev-handoff 建组 订单项目，分工选项为：前端、后端、测试
```

AI 会协助本人完成腾讯文档授权。所有者先把项目组目录定向分享给指定同事并设为可编辑，把目录链接发给对方；同事安装后，在自己的开发仓库输入：

```text
$dev-handoff 接入项目组 <目录链接或ID>
```

首次使用时，打开 AI 给出的链接，用被邀请的个人账号确认授权，再回复“已授权”。AI 继续核对目录、表格和已有工单文档；若子文件未继承目录权限，仅补分享报错的文件。个人账号权限继承须以对方实测为准；目录中尚无工单时，文档权限检查待首次发布后完成。每位协作者使用自己的 Token。Claude Code 的入口是 `/dev-handoff`。

日常输入“发布 <本地.md路径或腾讯文档链接>”“查看”“领取 工单ID”“处理 工单ID”“验收工单 ID”。AI 从 Markdown 整理可独立验收的交付，创建一行一文档；Git 暂存改动可作核对，不是发布门槛。AI 开发完成后停在待验收；“继续”不会自动验收。指定验收人说“验收工单 ID”，AI 逐项实际检查并根据结果确认、退回或说明无法验证项。

发布时明确应用范围、接手方交付和验收责任，按可独立验收的结果拆单。发布侧检查应用／共享契约重叠，接手方 `preflight` 提前检查实际计划文件；`show` 同时读取表格与文档。旧表可逐步升级为项目组，旧工单再次处理时才生成对应文档；历史、代码指纹和版本验收保护继续保留。

新工单指定实际验收人的成员账号，逐项关联操作、预期和证据。CLI `ready` 默认只提交待验收，不启动后台。指定验收人确认后，原处理方下次调用技能“处理／继续／对接”时，AI 复核同版本确认、原代码、会话和依赖，再调用 `accept`；只读查看不会回写。退回重提和重新验证都生成新版本，旧确认不能复用。历史后台入口仅供明确请求后台模式时兼容使用。详见 [逐项验收流程](dev-handoff/references/acceptance.md)。

完整操作见 [中文使用说明](dev-handoff/使用说明.md)，安装授权和跨设备联调见 [setup.md](dev-handoff/references/setup.md)。

要验证两位使用者各自的 AI 只在调用时接续，使用 [按需双人对话联调](dev-handoff/references/on-demand-dual.md)：先更新并重读双方技能、核对固定版本，再按发布、追问、答复、处理、验收、完成分次执行。使用隔离工作区，不启用后台；同事阶段必须由本人在自己的电脑运行。

## 开发验证

```sh
python3 -m unittest discover -s tests -q
```

Token、项目绑定和个人恢复状态不进入 Git。真实双账号访问权限需要双方在各自电脑完成联调。
