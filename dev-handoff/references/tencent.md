# 腾讯文档完整流程

腾讯个人账号已是唯一运行后端，统一入口为 `scripts/handoff.py`。`scripts/tencent_handoff.py` 是同一完整 CLI 的兼容入口，不再提供绕过结果版本校验的直接状态修改命令。

- 授权、安装、指定同事共享和第二台电脑加入：[setup.md](setup.md)。
- 读取暂存区并发布：[publishing.md](publishing.md)。
- 领取、范围保护、逐条处理、人工验收与恢复：[processing.md](processing.md)。
- 当前验证范围：[validation.md](validation.md)。

每人 Token 和执行会话留在自己的机器；共享智能表含“工单、跟进记录、成员”。账号归属按腾讯稳定 user_id，人工验收仍须在实际处理工作区由原处理账号发起，以核对真实代码证据。
