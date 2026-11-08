# 专有名词人工维护与入库校对

## 当前用途

标准名称通过读取用户指定的米游社原神Wiki对应图鉴页面维护；开发者维护别名，或登记测试中发现且核实的误识别案例。校对发生在资料入库的解析/分块前，运行时只读本地已审核词表，不依赖模型、OCR、网络或付费服务。开发阶段读取Wiki与应用运行时联网抓取是不同能力，本轮没有新增抓取依赖或自动定时更新。

本次用户确认“奥萝塔”应为“奥黛塔”，Wiki角色目录同样显示该标准名。2026-10-04.1曾仅含两个确认角色；2026-10-04.2扩充为当日读取目录的444个标准名称，保留唯一既有误识别别名。这是当前读取快照，不保证永久完整、所有详情内容审核通过、游戏事实或配队强度正确。

## Wiki来源和两条维护路径

| 类别 | 已读取目录 | 详情链接数 / 去重标准名数 |
| --- | --- | --- |
| 角色 | [角色图鉴](https://baike.mihoyo.com/ys/obc/channel/map/189/25?bbs_presentation_style=no_header&visit_device=pc) | 136 / 129 |
| 武器 | [武器图鉴](https://baike.mihoyo.com/ys/obc/channel/map/189/5?bbs_presentation_style=no_header&visit_device=pc) | 252 / 252 |
| 圣遗物套装 | [圣遗物图鉴](https://baike.mihoyo.com/ys/obc/channel/map/189/218?bbs_presentation_style=no_header&visit_device=pc) | 63 / 63 |

提取范围只包含对应目录中指向 `/ys/obc/content/<ID>/detail` 的图鉴条目标题，不包括导航、筛选项或页脚。七种元素的旅行者男女条目同名，按类别/名称归并并保留两个详情URL；因此129不是独立可玩角色数量。圣遗物这里只覆盖套装名，尚未收录所有单件名称。每条confirmation记录读取日期及详情链接，明确是目录标题核对，不声称已逐页阅读全文。

- **标准名更新**：重新读取对应Wiki页面，核对类别、条目标题与详情URL；只把实际读到的名称作为新增或变更候选。保留读取日期和来源、核对后更新revision。网页只有“数据加载中”、空列表、分类混杂或链接数对不上时不更新；Wiki未出现旧条目不意味着可以删除。名称变更应参考详情ID人工核对，不按相似度自动合并。
- **别名更新**：开发者确认具体错误写法，或测试中收集“原资料/OCR结果 → Wiki标准名”的可复核案例，再登记alias和确认依据；未知案例保留为待审，不把近似候选、昵称或缺字自动变成规则。刷新Wiki标准名时，保留已有aliases和确认历史。

例如“冰主”当前仍未登记为“旅行者·冰”的别名；目录标准名不自动授权我们推导习惯简称。正式自动刷新器、后台抓取任务、OCR识别器和管理页面都未在本轮实现，当前由开发维护流程读取和审核文件。

本次原始抓取结果保存在教学目录已忽略的`.firecrawl/`，未把整页攻略效果、图片或抓取工具写入运行时包。可复核快照SHA-256：

```text
角色 .firecrawl/mihoyo-wiki-189-25-20261004.json
a1745389d74d179c9d3e765ea99c0a5ab59f5ed644ef2185401b2f264c8bd4ec
武器 .firecrawl/mihoyo-wiki-weapons-rendered-20261004.json
fd2c9eef796e85527d4ce3771dfff79607f13f60d9c90e9f1c8dcf45212a5177
圣遗物 .firecrawl/mihoyo-wiki-artifacts-20261004.json
8d957a569610f69df69d45029512cdfa5c6108b39fb68adca0d689653c54f72c
```

## 维护 JSON

默认文件：`src/teyvatnotes/knowledge/data/terminology.json`。UTF-8 编码；每次审核更新建议递增 `revision`。下面是一条演示结构，不是完整词表或当前全部来源信息。

```json
{
  "revision": "2026-10-04.1",
  "terms": [
    {
      "category": "character",
      "name": "奥黛塔",
      "aliases": ["奥萝塔"],
      "confirmation": "用户确认：先前图片转录错误，标准名为奥黛塔。"
    }
  ]
}
```

类别使用 `character`（角色）、`weapon`（武器）、`artifact`（圣遗物）。新增条目时填写完整标准名、已经确认且唯一指向此名的误识别写法、确认依据。没有依据时不添加 alias；不要把常见短词、泛称或可能对应多个角色的简称登记为自动替换规则。标准名允许单字，alias 至少两字。

同类别内，标准名与所有 alias 必须唯一；跨类别重名允许，但不带类别的正文出现歧义时必须人工审核。没有已确认别名时写`[]`，也可省略，默认空列表；`null`或`[""]`非法。非法类别、重复 JSON 键、未知字段、坏编码和过大文件均拒绝，不回退空表。

上限为 1 MiB 文件、2,000 条标准名、5,000 个类别/词形身份、每条 8 个 alias；名称最长 80 字、确认依据最长 400 字、revision 为 1～40 个 ASCII 字母/数字/下划线/点/连字符。

## 校对规则和限制

| 输入 | 处理 |
| --- | --- |
| 角色名称字段 `奥萝塔` | 精确命中已确认 alias，更正为 `奥黛塔` |
| 正文 `奥萝塔，冰主` | 名称有分隔边界，可以更正 |
| 正文 `奥萝塔配队`、`奥萝塔尔` | 不能确认完整词边界，保持原文、要求人工审核 |
| 名称字段 `奥戴塔` | 保持原名，给出相似候选，不自动替换 |
| 显式标注但未登记的名称 | 保持原名，要求人工审核 |

正文单次最长 20,000 字，匹配已登记标准名和 alias，最长优先、单遍替换；不会级联替换。ASCII 名称有词边界保护；alias 邻接非 ASCII 字母（包括中文扩展字）时保守审核，不试图分词判断。相似度比较最多 256 个所选类别词形，超过则不生成有偏的前缀候选，返回 `suggestions_limited=true`；精确查找不受此预算影响。

正文没有任意未知实体自动抽取能力。人工整理资料或后续识别器应把识别到的名称放到快照可选字段中：

```json
"recognized_terms": [
  {"category": "character", "name": "奥黛塔"}
]
```

此处应列出实际识别的名称，不要为了通过检查隐去未确认项。列表最多 200 条。未知或歧义的显式名词、正文中待审核的 alias 都产生 `terminology_review_required`，资料仍可保存为草稿，但阻断发布。未登记的主角色 `entities` 仅产生 `entity_name_not_in_catalog` 告警，这是支持不完整词表的渐进策略，不等于验证成功。未标注的未知正文名词无法保证被发现。

中文连写的确认为真实 alias 后，应人工修正整理快照的整句并保存新版本，或在明确保留同样语义时给名称添加标点分隔；不要修改原始来源历史，也不要用空词表绕过待审核项。问题JSON与JSON会话补丁中的角色/装备库存仍需调用方使用标准名称，不自动更正。自然语言 `--text` 规则入口只在支持的明确字段或问法中规范化已登记别名；未知名仅供确认，已确认会话范围不能隐式改变，见[自然语言入口](conversations.md#自然语言规则入口)。可显式开启模型辅助候选提取，仍由同一本地有限语法与词表验收，不代表已支持通用口语。

## 离线检查和导入

```powershell
uv run python -m teyvatnotes.knowledge.terminology_cli check-name 奥萝塔 --category character
uv run python -m teyvatnotes.knowledge.terminology_cli check-name 奥戴塔 --category character
uv run python -m teyvatnotes.knowledge.terminology_cli check-snapshot <快照JSON路径>
```

检查命令只输出 JSON：名称结果、候选、词表版本/哈希或资料分块审计；不改文件、不入库，`model_called=false`。不等于完整发布校验。自定义词表用 `--catalog <词表路径>`。

```powershell
uv run python -m teyvatnotes.knowledge.cli import-snapshot <快照JSON路径> --memory --terms <词表JSON路径>
# 审核完成后，可显式请求发布；待审核名称仍会阻断。
uv run python -m teyvatnotes.knowledge.cli import-snapshot <快照JSON路径> --memory --terms <词表JSON路径> --publish
```

`--terms` 省略则用包内词表。`--memory` 只在当前命令进程内有效，不持久化；不带它时使用已配置且已迁移的业务 PostgreSQL。命令不自动迁移/启动服务。用户于2026-10-05启动服务后，已在独立验证库补完46项真实PG测试，其中新增词表跨连接持久化用例；这不是业务库升级或真实模型回答验收，详见 [数据库补验](postgres-wiki-acceptance.md)。

## 版本、审计与旧错误的处理

默认处理身份升级为 `offline-v3:chars-600`。入库保存完整词表、内容哈希、原实体和名称解析结果；词表内容或 revision 变化会改变文档身份，即使开发者忘记递增 revision，也不会复用旧纠错检查点。正文原数据保存在快照中，纠正后的证据分块保存更正前后名称及坐标；变长替换的分块偏移明确标为 `normalized_text`，不冒充原文坐标。资料标题、作者、URL、版本和玩家条件不在自动改写范围。

更新词表后须重新导入相关资料，再用新证据准备问答。已发布旧文档和既有会话轮次不被批量重写；是否切换至新资料需由使用者审核。此前真实模型答案中的错误、引用 ID、调用次数、Token 和费用仍保留为历史；校正资料和当前测试库存不能被描述成已经重新生成了一份真实答案。

本次实现不修改 `.env` 或模型服务商配置、不调用问答模型、不提交 Git，也不新增管理 HTTP 接口。数据库补验只在新建验证库及随机schema运行真实迁移；不自动迁移原有测试库或业务库。
