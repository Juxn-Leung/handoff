# dev-handoff

通过腾讯文档个人 QQ／微信账号共享开发工单：从 Git 暂存区发布、领取任务、逐条处理、记录跟进历史，再由指定验收人授权 AI 按表中标准验收，原处理工作区自动复核并回写状态。

成员不限前端或后端。分工默认可以留空，也可在建表时自定义选项；任务归属使用稳定账号 ID。建表会尝试将工单默认视图精简为业务表头，内部证据与恢复字段隐藏保留。当前腾讯接口在实测中拒绝显隐更新（22032）；遇到此情况会报告表头待整理，业务流程仍可使用，可在腾讯文档字段显隐面板手动整理。

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
$dev-handoff 建表 订单项目，不限定成员分工
```

或者：

```text
$dev-handoff 建表 订单项目，分工选项为：前端、后端、测试
```

AI 会协助本人完成腾讯文档授权。文档所有者通过腾讯文档分享面板邀请指定同事为可编辑；同事安装后，在自己的开发仓库只需输入：

```text
$dev-handoff 接入 <真实腾讯智能表URL>
```

首次使用时，打开 AI 给出的链接，用被邀请的个人账号确认授权，再回复“已授权”。AI 自动继续绑定、登记成员和检查队列；已有有效授权则直接完成。每位协作者使用自己的 Token。Claude Code 的入口是 `/dev-handoff`。

日常输入“发布”“查看”“领取 工单ID”“处理 工单ID”“验收工单 ID”。AI 开发完成后停在待验收；“继续”不会自动验收。指定验收人说“验收工单 ID”，AI 默认读取表中步骤并逐项实际检查：全部通过就确认，有明确失败就退回并记录原因，没有失败但有无法验证项则列出原因、保持待验收，无需附加长授权。只想拿检查报告时说“验收工单 ID，只检查，不回写”；“验收通过 工单ID”用于确认已经核对的结果。

发布时明确应用范围、接手方交付和验收责任，按可独立验收的结果拆单。发布侧检查应用／共享契约重叠，接手方 `preflight` 提前检查实际计划文件；`queue --app admin-app` 默认精简分页，`show/review` 读取详情。阻塞提示读取当前状态与结果版本，历史、代码指纹和指定版本的授权与验收保护继续保留。旧表无需补列，旧工单和恢复检查点保持兼容。

新工单指定实际验收人的成员账号，逐项关联操作、预期和证据。CLI `ready` 默认启动原工作区的本机自动回写：验收人跨工作区 `confirm` 后，后台核对同版本确认、原代码、会话和依赖，再调用 `accept`，无需处理人再说一次回写。原电脑须保持运行并联网；默认每 60 秒检查，最长等待 7 天，异常或超时保留原因。历史待验收单由原处理方说“启用自动回写 工单ID”启用一次；新单默认启用，可在提交时选择手动回写。退回重提和重新验证都生成新版本，旧确认不能复用。详见 [逐项验收流程](dev-handoff/references/acceptance.md)。

完整操作见 [中文使用说明](dev-handoff/使用说明.md)，安装授权和跨设备联调见 [setup.md](dev-handoff/references/setup.md)。

## 开发验证

```sh
python3 -m unittest discover -s tests -q
```

Token、项目绑定和个人恢复状态不进入 Git。真实双账号访问权限需要双方在各自电脑完成联调。
