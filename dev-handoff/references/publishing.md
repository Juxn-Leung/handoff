# 从暂存区发布交接工单

## 读取与整理

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" capture
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" index-file path/to/controller.py
```

`capture` 返回 snapshot 和暂存 diff。大项目可用 `--paths 文件...` 分组读取；快照指纹仍代表完整暂存区。
需要本地文件时用 `capture --output /临时目录/context.json`。快照包含 HEAD、索引哈希、分支、暂存路径及时间。
未暂存和被忽略的文件不属于本次交接；补充上下文须区分暂存版本与当前工作文件。
删除文件阅读 diff 中的旧契约，不调用 index-file 读取已删除条目。二进制或子模块改动需要人工补充可用的契约资料。

以业务上可独立接入和验收的事项拆单。多个接口共同组成一个功能时保持一条；同一文件可以参与多个事项。
每个事项给稳定 key（如 `order-list`），重试必须沿用原草稿、快照和 key。不要为了绕过重复检测更换 key。
所有暂存文件都必须被工单覆盖，或在 excluded_paths 中明确排除并说明原因。

## 交接说明模板

按实际场景填写，不适用的部分明确写“不适用”，无法确认的写“待确认”。影响契约或验收的关键缺口先问操作者。

```text
业务目的与适用页面／模块：
已完成的改动：
接口契约：
  方法、路径、鉴权、环境与可用状态
  请求参数／请求体：字段、类型、必填项、单位及枚举
  响应结构、分页、错误码与异常处理
  可运行的请求／响应示例（使用虚构数据，不含凭据）
接手方需要做什么：按步骤说明调用时机、字段映射和页面状态
兼容与迁移影响：旧接口是否保留，哪些调用方需要调整
验收方式：可观察的行为、成功路径与必要异常路径
来源与限制：暂存／已推送／已部署，资料所在位置
相关事项：明确的关联，或等待人工确认的关系建议
```

后端代码能证明实现时才写“已实现”；前端提出的后端接口需求必须标注“待后端实现”。
本地分支和路径只是来源线索，不能声称同事已能拉取。联调地址和部署状态只使用真实可确认的信息。
不把完整 diff、环境变量、令牌或不相关源码直接上传表格。

## 发布草稿 JSON

将 JSON 写到仓库外的个人临时目录，避免草稿污染开发范围。以下 snapshot 替换为 capture 返回的 fingerprint：

```json
{
  "snapshot": "capture 返回的 fingerprint",
  "tickets": [
    {
      "key": "order-list",
      "title": "订单列表接口接入",
      "description": "订单页使用新接口展示订单并处理空列表。",
      "change_types": ["feat"],
      "paths": ["src/orders/controller.ts"],
      "handoff": "完整接口契约、调用示例、接入步骤和可用性说明。",
      "acceptance": "正常返回展示列表；空数据展示空状态；请求失败提供重试入口。",
      "critical_unknowns": [],
      "priority": "普通",
      "related": [],
      "dependencies": []
    }
  ],
  "excluded_paths": [],
  "exclusion_reason": ""
}
```

`change_types` 支持 feat、fix、change、remove、refactor，可多选；删除用 remove。
`related`／`dependencies` 只接收本表已有的真实 recordId。没有确证的依赖只写入 handoff 建议，不能凭相似标题自动设定。
同批新工单之间需要依赖时，发布后展示真实 ID，让操作者在表格确认并设置关联。

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" --table 订单 publish /临时目录/draft.json
```

发布自动填写发布人姓名和稳定账号 ID、状态及来源，跟进人默认留空，可由已登记成员本人领取，或由发布方分配给已登记的成员。
结束时返回真实表格 URL、工单 ID 和标题。发布不会执行 git add／commit／push，也不会发送消息。

## 重试

快照不同就重新分析，不能继续提交旧草稿。相同快照的重复调用沿用检查点和工单键，只回读，不覆盖手工修改。
部分成功保留已发布 ID；使用同一草稿恢复。未知结果先按工单键回读，找不到也不能立即重复创建。
若需要修改已发布需求，由操作者在表格调整；不要通过更换 key 重复发布同一事项。

## 分配与领取

发布后跟进人默认留空。接手方用 `queue` 查看 `claimable`，`show 工单ID` 读取 requirement_version，再 `claim 工单ID --requirement 需求指纹`。仅依赖已满足且无其他跟进人的工单可领取。

发布方也可先 `members` 获取已登记成员同事的唯一账号 ID，再 `assign 工单ID --user-id 真实账号ID --requirement 需求指纹`。已分配给其他账号或已经开始的工单不能被覆盖。表格成员不自动等同于平台编辑权限，指定同事分享见 setup.md。
