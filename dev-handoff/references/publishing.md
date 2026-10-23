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

一张工单对应一份范围明确、可以独立验收的交付。多应用能独立交付、独立退回就拆单；必须共同修改和验证才保留一单，不机械规定一应用一单。多个接口共同组成一个功能时可以保持一条；同一来源文件可以参与多个事项。
公共字典、接口契约等共享修改要尽早明确归属；有独立检查方式时可以拆为前置事项。暂不增加“部分完成”状态，以关联工单分别验收表达进度；不能自动拆分、复制或转移旧结果证据。
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
交付边界：影响哪些应用；本单交付什么；哪些工作由其他事项承接
验证与责任：本单必要验证、证据提供方、验收责任；外部验证是否阻塞开始或验收
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
      "delivery": {
        "apps": ["orders-app"],
        "scope": "订单列表页",
        "deliverable": "接入订单列表接口并处理加载、空列表和失败重试",
        "acceptance_owner": "指定成员确认前端交付；处理方在原工作区复核代码后回写",
        "reviewer_id": "144115200000000001",
        "checks": [
          {"id": "ui-states", "description": "列表与异常状态测试", "kind": "mock", "owner": "接手方", "required": true}
        ],
        "external_checks": [
          {"description": "数据库、Redis 与后端并发回归", "owner": "后端", "gate": "none"}
        ],
        "criteria": [
          {
            "id": "list-and-errors",
            "description": "订单列表及异常状态符合约定",
            "steps": ["正常响应时打开订单列表", "切换空响应", "模拟请求失败并点击重试"],
            "expected": "展示订单；空数据展示空态；失败提供可用的重试入口",
            "check_ids": ["ui-states"]
          }
        ],
        "shared_contracts": ["order-status-dictionary"]
      },
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
`delivery` 是新工单的交付约定，必填。`apps` 填接手方应用标识，多个应用填数组；所有应用都适用的共享事项用 `["*"]`。`scope` 写页面／模块边界，`deliverable` 写可检查的交付结果。`checks` 的每项有唯一 `id`、描述、证据提供方 `owner`、类型 `local/mock/integration/manual` 和 `required`；至少一项必要验证，提交结果时必要项必须通过。

`acceptance_owner` 描述验收责任；实际确认权限由 `reviewer_id` 绑定的唯一已登记账号决定，先 members，不从昵称或分工猜 ID。`criteria` 逐项覆盖原有 acceptance 中的全部标准，每项有唯一 id、description、操作步骤 steps、预期 expected 和关联检查 check_ids；标准与说明必须一致，不把后端回归来源直接照抄成前端的全部验收责任。实际验收人可以是处理人，但必须明确选择。

新发布草稿必须有 reviewer_id 和 criteria。示例账号是虚构值，发布前须替换为 members 返回的真实账号。交付后按每项标准提供实际结果和证据，指定验收人确认同一版本后，再由原处理账号在原工作区复核代码并回写；详见 [acceptance.md](acceptance.md)。

`external_checks` 明确外部验证的责任与门槛，没有外部验证也要填写 `[]`。`gate: none` 仅披露，不阻塞本单；`start` 要求其验收后才能开始本单；`acceptance` 允许开发并提交待验收，但验收前必须完成。后两者必须填写已存在于本表的 `ticket_id`，找不到承接事项时先问清，不能用“未执行”绕过门槛。若真实联调本身属于本单必要验证，就在本单 checks 中声明 `integration` 且 required，不能仅为提前完成把它划为外部事项。

`shared_contracts` 填公共字典、接口契约或配置的稳定标识，供发布时交叉提示；它不是锁，也不自动生成依赖。`paths` 仍是发布方暂存来源，接手方实际路径要在自己仓库用 preflight 声明。
`related`／`dependencies` 只接收本表已有的真实 recordId。没有确证的依赖只写入 handoff 建议，不能凭相似标题自动设定。
同批新工单之间需要依赖时，发布后展示真实 ID，让操作者在表格确认并设置关联。

```sh
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" --table 订单 publish /临时目录/draft.json --check
python3 "$SKILL_DIR/scripts/handoff.py" --repo "$PROJECT_DIR" --table 订单 publish /临时目录/draft.json
```

先执行 `--check`，根据应用与共享契约重叠提示核对拆单及归属；它不创建工单或发布检查点。提示不是已证实的文件冲突，不要求额外逐条确认；按已授权的发布范围完成草稿后发布。实际 publish 也再次检查并返回提示。

发布自动填写发布人姓名和稳定账号 ID、状态及来源，跟进人默认留空，可由已登记成员本人领取，或由发布方分配给已登记的成员。
结束时返回真实表格 URL、工单 ID 和标题。发布不会执行 git add／commit／push，也不会发送消息。

## 重试

快照不同就重新分析，不能继续提交旧草稿。相同快照的重复调用沿用检查点和工单键，只回读，不覆盖手工修改。
部分成功保留已发布 ID；使用同一草稿恢复。未知结果先按工单键回读，找不到也不能立即重复创建。
若需要修改已发布需求，由操作者在表格调整；不要通过更换 key 重复发布同一事项。
delivery 保存于现有“来源”JSON，由 show／review 解码展示，参与原有需求指纹；不新增必需表列。旧工单缺少 delivery 仍可读取、处理和验收；已有旧版发布检查点只允许用原草稿恢复，不要求更改草稿。给旧工单补交付约定属于需求变化，会使旧结果失效，不能静默补写。

## 分配与领取

发布后跟进人默认留空。接手方用 `queue` 查看 `claimable`，`show 工单ID` 读取 requirement_version，再 `claim 工单ID --requirement 需求指纹`。仅依赖已满足且无其他跟进人的工单可领取。

发布方也可先 `members` 获取已登记成员同事的唯一账号 ID，再 `assign 工单ID --user-id 真实账号ID --requirement 需求指纹`。已分配给其他账号或已经开始的工单不能被覆盖。表格成员不自动等同于平台编辑权限，指定同事分享见 setup.md。
