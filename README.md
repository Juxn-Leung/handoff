# dev-handoff

通过腾讯文档个人 QQ／微信账号共享开发工单：从 Git 暂存区发布、领取任务、逐条处理、记录跟进历史，再由操作者确认验收并回写状态。

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

## 开始使用

在实际开发仓库聊天输入：

```text
$dev-handoff 建表 订单项目，不限定成员分工
```

或者：

```text
$dev-handoff 建表 订单项目，分工选项为：前端、后端、测试
```

AI 会协助本人完成腾讯文档授权。每位协作者使用自己的 Token，文档所有者通过腾讯文档分享面板邀请指定同事为可编辑。其他人绑定同一个表格链接并登记本人账号。Claude Code 的入口是 `/dev-handoff`。

日常输入“发布”“查看”“领取 工单ID”“处理 工单ID”“验收通过 工单ID”。AI 完成一条后停在待验收；“继续”不会自动验收。

完整操作见 [中文使用说明](dev-handoff/使用说明.md)，安装授权和跨设备联调见 [setup.md](dev-handoff/references/setup.md)。

## 开发验证

```sh
python3 -m unittest discover -s tests -q
```

Token、项目绑定和个人恢复状态不进入 Git。真实双账号访问权限需要双方在各自电脑完成联调。
