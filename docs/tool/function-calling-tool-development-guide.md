# AI Agent Function Calling 工具开发指南

> 面向 Agent 框架与工具链设计者
> 不含权限与沙箱(另文讨论)
> 聚焦架构、模块、原理、思想 — 不贴项目源码

---


## 1. 引言
LLM 不会"调用函数"。它只能生成文本。所谓 function calling,本质是 LLM 在文本中输出**结构化的 JSON 片段**(一个叫 `tool_call` 的对象),由客户端系统解析这段 JSON,代为执行真正的操作,然后把结果回灌给 LLM。

理解这一点,是设计工具系统的起点。

### 1.1 Function Calling 解决了什么问题

LLM 本身是个**纯文本生成器**。给它一个问题,它给你一段回答。这在聊天场景下够用,但在 Agent 场景下远远不够:

- 你希望 LLM 能**读文件** — 但 LLM 只能给你"读文件"的文本描述
- 你希望 LLM 能**执行命令** — 但 LLM 只能描述怎么执行
- 你希望 LLM 能**查数据库** — 但 LLM 不知道你的数据库在哪

传统做法是"LLM 生成一段文本,人工或脚本解析后执行",这极不可靠 — LLM 输出的格式飘忽不定,解析失败率高。

Function calling 改变了游戏规则:**LLM 被训练成在特定触发下,直接输出符合预定义 schema 的结构化 JSON**。这个 JSON 描述"我想调什么工具、参数是什么"。客户端拿到 JSON 后,做参数校验、执行、回填结果。

带来的好处:
- **可靠性**:JSON schema 是机器可校验的契约
- **可观测性**:调用被结构化记录,可重放、可审计
- **可组合**:多个工具调用可被批量解析、并行执行

### 1.2 LLM 的"工具调用"本质

LLM 内部没有"调用函数"这一动作。它仍然是**预测下一个 token**,只是被训练成在"该调工具"的触发下,按特定格式输出一个 JSON 块:

```
用户: 帮我看下 /tmp 有什么文件
LLM 输出: 
  ...
  <tool_call>
  {"name": "list_dir", "arguments": {"path": "/tmp"}}
  </tool_call>
```

**`tool_call` 不是 LLM 的动作,而是 LLM 的语言**。客户端系统是这个语言的解释者与执行者。

这个本质决定了所有 Agent 工具系统的设计原则:
- **LLM 是不可信的**:它的 tool_call 可能语法对、但语义错(参数错、工具选错)
- **校验是必须的**:每次 tool_call 都要 schema 校验 + 业务校验
- **执行是隔离的**:tool 执行环境与 LLM 推理环境应分离
- **错误是 LLM 的反馈**:tool 执行错误应该以 LLM 能理解的格式返回,让 LLM 在下一轮修正

### 1.3 Agent 系统的核心三角

任何 Agent 系统都围绕三个要素循环:

```
        ┌──────────────┐
        │     LLM      │
        │  (推理/决策)  │
        └──────┬───────┘
               │ 输出 tool_call
               ▼
        ┌──────────────┐         ┌──────────────┐
        │  Tool 工具   │◄────────│   Schema    │
        │  (执行/IO)   │  返回结果│   协议层     │
        └──────────────┘         └──────┬───────┘
               ▲                       │
               └───────────────────────┘
                  Schema 校验 tool_call
```

- **LLM**:负责推理、决策、生成 tool_call
- **Tool**:负责执行、I/O、与外部世界交互
- **Schema / Protocol**:负责 LLM 与 Tool 之间的"语言"翻译 — 既是 LLM 看的描述,也是机器校验的契约

工具系统设计的所有问题,都可以归到这三个要素的边界与协作上。

---

## 2. 核心概念与原理

上一章我们建立了最重要的认识:**tool_call 是 LLM 的语言,不是 LLM 的动作**。本章把这句抽象落到具体 — 工具在系统里到底是什么、tool_call 在协议层面长什么样、一次完整调用走过哪些环节。

### 2.1 工具的三个要素

无论哪个 LLM 厂商、哪种协议,一个工具在系统里被描述时,都离不开三个要素:

1. **name** — 工具的标识符,LLM 用它来"点名"。`read_file`、`Bash`、`mcp__fs__write` 都是合法 name。
2. **description** — 给 LLM 看的一段自然语言说明,描述这个工具**做什么、什么时候用、怎么用**。这是 LLM 决策"要不要调它"的唯一依据。
3. **input_schema** — 用 JSON Schema 描述工具接受的参数结构(类型、必填、嵌套)。LLM 根据这个 schema 生成合法的 arguments。

这三要素共同构成工具的**契约**:
- name 是身份(LLM 通过 name 引用)
- description 是语义(LLM 通过它理解用途)
- input_schema 是语法(LLM 必须按 schema 输出参数)

**这三要素中,description 是最容易被低估的**。开发者往往觉得"写段文档就行",但 description 实际上是 LLM 决策能力的瓶颈 — 写得模糊,LLM 就乱选工具;写得冗长,LLM 又抓不住重点。第 4 章会专门讨论怎么写好 description。

### 2.2 JSON Schema 在工具描述中的角色

为什么工具参数用 JSON Schema 描述?因为它是目前事实上的"机器可读 API 文档"标准:

- **声明式**:用嵌套对象描述结构,不需要写代码生成器
- **可校验**:`jsonschema` 库能直接校验 LLM 输出是否合规
- **可扩展**:支持 `$ref`、`oneOf`、`anyOf` 等高级结构,够描述复杂 API
- **生态广**:OpenAPI、JSON Schema、LLM 工具描述,共享同一套词汇

一个典型的 input_schema:

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "description": "文件绝对路径"},
    "max_lines": {"type": "integer", "minimum": 1, "default": 100}
  },
  "required": ["path"]
}
```

这个 schema 同时承担三个角色:
1. **给 LLM 看的说明书** — 让 LLM 知道要传什么
2. **给校验器用的契约** — 客户端用 jsonschema 库严格校验 LLM 输出
3. **给开发者看的接口定义** — 单一来源,无需另写文档

**Schema 是工具设计的"宪法"**。一旦定下,所有调用都必须遵守;反之,schema 也要给 LLM 留足描述空间(只描述结构不限制语义),否则 LLM 会"不敢"传某些合理但 schema 没明示的参数。

### 2.3 tool_call 的生命周期

一次完整的 tool 调用,经历五个阶段。理解这个生命周期,是设计执行器、错误处理、超时机制的基础。

```
┌──────────┐    ┌────────────┐    ┌──────────────┐    ┌──────────┐    ┌──────────┐
│ 1. 决策  │───▶│ 2. 解析校验 │───▶│ 3. 执行      │───▶│ 4. 结果   │───▶│ 5. 回灌   │
│ LLM 输出 │    │ JSON 解析   │    │ 真实业务     │    │ 标准化   │    │ 拼回     │
│ tool_call│    │ schema 校验 │    │ 读文件/调API │    │ 格式     │    │ LLM context│
└──────────┘    └────────────┘    └──────────────┘    └──────────┘    └──────────┘
     ▲                                                                 │
     └─────────────────────────────────────────────────────────────────┘
                              LLM 看结果决定下一步
```

**阶段 1:决策** — LLM 在推理时,基于用户 prompt + 系统 prompt(包含工具列表 + description)+ 历史 context,决定"现在要不要调工具、调哪个、参数是什么"。这一步完全发生在 LLM 内部,客户端只能"看到结果"(tool_call 文本),无法干预推理过程。

**阶段 2:解析校验** — 客户端拿到 LLM 输出,做三件事:
- 用正则或厂商 SDK 解析出 tool_call 块
- 用 input_schema 校验参数结构(类型、必填、嵌套)
- 业务校验(可选 — 比如路径是否在允许目录)

校验失败的 tool_call 应该**原样返回给 LLM**,让它在下一轮修正(类似函数式编程的"把错误当返回值")。

**阶段 3:执行** — 调用真实业务逻辑。这一阶段最复杂,涉及:
- 同步 vs 异步的选择
- 并行 vs 顺序
- 超时与取消
- 副作用管理(写文件、调用 API)
- 错误捕获与分类

**阶段 4:结果标准化** — tool 执行结果必须**标准化**为 LLM 能理解的格式(通常是 JSON 字符串或结构化块)。这一步把"二进制图片"、"长文本"、"复杂对象"统一转成 LLM 能消化的形式。

**阶段 5:回灌** — 把标准化结果附加到 LLM 的对话历史里,LLM 在下一轮能看到结果并决定下一步。

**回灌的格式直接影响 LLM 的后续决策**。比如 tool 返回了一个错误,如果错误信息不够结构化,LLM 可能误以为是工具的正常使用结果。

### 2.4 术语统一

这个领域术语混乱,不同厂商不同文章叫法不一。我用下面的统一表,后文都按这个走:

| 我用的术语 | 同义说法 | 含义 |
|----------|---------|------|
| **tool** | function / tool use | 系统可调用的能力单元 |
| **tool_call** | function call / tool use block | LLM 输出的"我要调工具"结构化指令 |
| **tool_result** | function result / tool response | tool 执行后的标准化返回 |
| **input_schema** | parameters / function schema | 工具参数的结构定义 |
| **tool description** | tool definition | 工具的自然语言说明 |
| **registry** | tool registry / tool store | 工具的注册中心 |
| **executor** | tool runtime / dispatcher | 工具的执行器 |

**用"tool"而不是"function"**:虽然业界叫"function calling",但一旦进入代码层,"function"会和编程语言的函数混淆。统一用 tool,既符合 OpenAI/Anthropic 的现代叫法,也减少心智负担。

### 2.5 一次完整调用的数据流

把上面的概念落到具体数据流。假设 LLM 决定调用 `read_file` 读一个文件:

```
[用户 prompt]
  "看看 /tmp/server.log 最近 10 行"
       │
       ▼
[LLM 推理] — 系统 prompt 包含 tool 列表 + description
       │
       ▼
[LLM 输出]
  assistant: 我读一下。
  <tool_call>
  {"name": "read_file", "arguments": {"path": "/tmp/server.log", "max_lines": 10}}
  </tool_call>
       │
       ▼ (客户端解析 + schema 校验)
  tool_call = {name: "read_file", args: {path: "/tmp/server.log", max_lines: 10}}
       │
       ▼ (registry 查找 tool)
  tool_def = registry.get("read_file")  # 找到 tool 定义
       │
       ▼ (执行器调用)
  result = tool_def.execute(tool_call.arguments)  # {"content": "...", "lines": 10}
       │
       ▼ (结果标准化)
  tool_result = {
    "content": "[2024-01-15 10:23:45] Server started\n...",
    "truncated": false
  }
       │
       ▼ (回灌到 LLM context)
  tool message:
    {
      "tool_call_id": "abc123",
      "content": json.dumps(tool_result)
    }
       │
       ▼
[LLM 第二轮推理]
  看 tool_result 后回答:
  "server.log 显示 ... 看起来服务已启动。"
```

**几个值得注意的点**:

1. **tool_call_id 必须回传** — LLM 在一次输出里可能调多个工具,客户端回灌时必须用 id 标识"这个 result 对应哪个 call"
2. **错误也是 result** — tool 执行失败时,客户端不应抛异常中断,而是把错误信息作为 tool_result 回灌(格式可能是 `{"error": "...", "code": "ENOENT"}`),让 LLM 决定下一步
3. **第二轮 LLM 看的是 tool_result,不是工具本身** — LLM 永远不会"调用"工具,它只能"看到"工具的返回 — 这点决定了工具的 result 设计必须 self-explanatory

---

## 3. LLM 协议适配

工具是你定义的,LLM 是你选的,但两者之间的"语言"由 LLM 厂商规定。OpenAI 用一种格式描述工具,Anthropic 用另一种,Google 又不同。本章讨论:这些差异在哪、如何用适配器抹平、哪些设计选择会影响你的工具接口。

设计一个跨厂商的工具系统,适配层是必做的;设计一个只跑单厂商的系统,适配层也很值得做 — 它把你的业务逻辑与厂商协议解耦,未来换厂商成本最低。

### 3.1 主流厂商的 tool calling 格式差异

虽然核心概念一致(name / description / schema),各家在字段命名、调用位置、嵌套结构上各有习惯:

**OpenAI 的风格** — `tools` 是顶层数组,每个 tool 内部用 `function` 嵌套:

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"]
        }
      }
    }
  ]
}
```

tool_call 在响应里也类似嵌套:

```json
{
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\": \"/tmp/x\"}"
      }
    }
  ]
}
```

注意 `arguments` 是**字符串化的 JSON**,需要二次解析。

**Anthropic 的风格** — 工具列表是顶层数组,每个 tool 平铺(没有 `function` 嵌套),用 `input_schema` 命名参数 schema:

```json
{
  "tools": [
    {
      "name": "read_file",
      "description": "读取文件内容",
      "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
      }
    }
  ]
}
```

tool_call 在响应里更简洁:

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_abc123",
      "name": "read_file",
      "input": {"path": "/tmp/x"}
    }
  ]
}
```

`input` 是**对象**,不用二次解析。

**Google Gemini 的风格** — 工具用 `functionDeclarations` 嵌套,且每个 tool 是一个独立的 declaration:

```json
{
  "tools": [{
    "functionDeclarations": [
      {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": { /* same as OpenAI */ }
      }
    ]
  }]
}
```

tool_call 在响应里也是嵌套结构。

**对比表**:

| 维度 | OpenAI | Anthropic | Google |
|------|--------|-----------|--------|
| 工具列表字段 | `tools[].function` | `tools[]` | `tools[].functionDeclarations[]` |
| 参数 schema 字段 | `parameters` | `input_schema` | `parameters` |
| tool_call 位置 | 顶层 `tool_calls[]` | 嵌套在 `content[]` | 顶层 `functionCall` |
| 调用 id | `id` 字段 | `id` 字段 | 隐含(无独立 id) |
| arguments 格式 | 字符串 JSON | 对象 | 对象 |
| 多模态 | 通过 `type` 字段 | `content[]` 多 type | `parts[]` 多 type |

**这些差异看似琐碎,实际影响三层设计**:
1. 工具注册中心用内部统一表示,适配器负责对外转换
2. 解析层要做"格式归一化"(字符串 → 对象、嵌套 → 平铺)
3. 错误处理要考虑每家对 tool 错误的回传格式

### 3.2 适配器模式

跨厂商工具系统的标准做法是**适配器模式**:把"内部统一表示"和"外部厂商格式"隔离开。

```
┌─────────────────┐
│  你的内部 Tool    │  ← 注册中心存这个
│  {name, desc,    │
│   schema, exec}  │
└────────┬────────┘
         │ 转换
         ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ OpenAI Adapter  │    │Anthropic Adapter│    │ Google Adapter  │
│  to_openai()    │    │ to_anthropic()  │    │ to_google()     │
│  from_openai()  │    │ from_anthropic()│    │ from_google()   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

每个 adapter 实现两个方向的转换:
- **to_X()**:把你的内部 Tool 转换为厂商格式(用于拼请求)
- **from_X()**:把厂商响应里的 tool_call 转换为内部标准格式(用于统一执行)

**内部标准的最小设计**:

```python
@dataclass
class ToolCall:
    id: str                # 调用 id,用于回灌时匹配
    name: str              # tool name
    arguments: dict        # 已经 parse 好的 dict(不是字符串)

@dataclass
class ToolResult:
    tool_call_id: str      # 对应的 call id
    content: Any           # 标准化的返回内容
    is_error: bool = False # 是否错误(LLM 也需要看错误信息)
```

注意 `arguments` 一定要在 adapter 层 parse 好 — 业务代码永远不接触字符串化的 JSON。

**adapter 实现的常见模式**:

```python
class OpenAIAdapter:
    def to_tools(self, tools: list[Tool]) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            }}
            for t in tools
        ]
    
    def from_response(self, response) -> list[ToolCall]:
        return [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=json.loads(tc["function"]["arguments"]),
            )
            for tc in response.tool_calls
        ]
```

### 3.3 Schema 转换关键点

跨厂商的 schema 转换有几个**实际坑**:

**坑 1:JSON Schema 子集差异**

OpenAI 支持 `enum`、`anyOf`、`$ref`,但历史上对 `oneOf` 的支持有 quirk;Anthropic 支持完整 JSON Schema;Google 的函数声明 schema 有自己的方言(类似 OpenAPI 3.0)。

**实用策略**:
- 设计内部 schema 时,**只用三家都支持的子集**(type / properties / required / enum / description / items)
- 高级表达($ref、复杂 anyOf)在 adapter 层做降级转换
- 记录"不支持的 schema 特性"清单,工具设计时避免

**坑 2:必填字段的隐性约束**

OpenAI 和 Anthropic 都遵循 JSON Schema 标准的 `required` 字段语义(properties 列在 `required` 里才视为必填)。表面看没有差异,但实际差异在 **schema 校验严格度**:Anthropic 历史上对 schema 字段缺失更宽容,某些 LLM 省略的字段会被自动填默认值;OpenAI 严格校验,省略必填字段直接报错。Adapter 层要根据厂商语义做对齐。

**坑 3:description 字段的字符限制**

各厂商对 description 长度有不同容忍度,且会随版本变化,具体限值以各厂商最新文档为准。**通用的实践原则**:description 写短写精(几百字最佳),长 description 不仅消耗 token,还会让 LLM 注意力分散。需要详细文档时放在"工具描述里嵌长文"或"独立文档链接"。

**坑 4:tool 数量上限**

各家对单次请求的工具数量有上限(几十到几百),超出会被截断或报错。adapter 层应该:
- 跟踪可用工具数
- 按"使用频率"或"相关性"排序后截断
- 提供"工具太多"的可观测指标(让运营决策)

### 3.4 多模态工具

工具不只返回文本。常见多模态:

- **图像输入/输出**:LLM 传入图片 path,工具返回 base64 / URL
- **文件附件**:返回文件下载链接或 base64 块
- **音频/视频**:返回时长、缩略图、转录文本
- **结构化数据**:返回 DataFrame / 表格

**跨厂商的多模态差异巨大**:

- OpenAI 用 `type: "image_url"` / `type: "image"` 等显式字段
- Anthropic 把图像作为 `content[]` 里的一种 type
- Google 用 `inlineData` / `fileData` 等

**adapter 层的统一表示建议**:

```python
@dataclass
class ToolResult:
    tool_call_id: str
    content: str | list[ContentBlock]  # 文本 or 多模态块
    is_error: bool = False

@dataclass
class ContentBlock:
    type: str   # "text" / "image" / "file" 等
    data: Any   # 文本字符串 / URL / base64 bytes
    mime: Optional[str] = None
```

工具实现返回 `list[ContentBlock]`,adapter 负责把这种统一表示转成各家格式。

### 3.5 streaming vs 非 streaming

tool_call 也分 streaming 和非 streaming:

**非 streaming**(主流):
- LLM 一次返回完整响应,客户端解析所有 tool_calls 后批量执行
- 简单、可靠、易调试
- 适合工具数量少、参数简单的场景

**streaming**:
- LLM 流式返回 token,tool_call 也是流式构造的(参数可能分多次到达)
- 客户端需要"边收边解析",看到完整 tool_call 就开始执行
- 复杂,但**降低首 token 延迟**(不用等所有 tool_call 参数都生成完才执行第一个)

**streaming tool_call 的难点**:

1. **JSON 不完整**:LLM 流式生成的 arguments 是 JSON 片段,需要 incremental parse(json-repair / streaming JSON parser)
2. **决策时点**:看到 `name` 就能定 tool,但要等 `arguments` 完整才能校验和执行 — 如何决策"何时开跑"?
3. **错误处理**:stream 中途取消的 tool_call 怎么办?

### 3.6 工具调用与文本输出的混合解析

LLM 一次输出可能同时包含**自然语言回复 + tool_call**。例如:

```
assistant: 我先看下文件。
<tool_call>
{"name": "read_file", "arguments": {"path": "/tmp/x"}}
</tool_call>
```

这种混合输出给解析带来挑战:
- 不能用整段 JSON parse(有自然语言干扰)
- 必须用正则或厂商 SDK 提取 `<tool_call>...</tool_call>` 块
- 提取后,自然语言部分和 tool_call 部分分别处理

**常见解析策略**:

1. **正则提取**(通用):
   ```
   从 LLM 输出匹配 `<tool_call>(.*?)</tool_call>` 块
   ```
   简单但脆弱 — LLM 输出格式漂移时正则可能漏。

2. **厂商 SDK**(推荐):
   各家 SDK 通常有 `extract_tool_calls(text)` 方法,处理了边界情况。

3. **流式 chunk 累积**(streaming):
   维护一个 buffer,跨 chunk 累积直到检测到完整 tool_call 块才解析。

**实践建议**:用厂商 SDK,不要自己写正则。各家 SDK 的 parser 都经历过生产打磨,比自己写稳。

---

## 4. 工具定义

协议层把 LLM 与你的工具连起来 — 但工具本身的设计,完全在你手上。本章讨论工具的元数据怎么写、怎么分类、怎么管理副作用,以及怎么决定"哪些工具暴露给 LLM"。

### 4.1 工具的元数据建模

每个工具在系统里被描述时,核心字段就三个:name / description / input_schema。这三个字段共同构成"LLM 眼中的工具"。

#### 4.1.1 name 命名原则

name 是 LLM 点名调用你的工具的唯一标识。命名看似小事,实则影响 LLM 的选择行为:

- **动词或动词短语优先**:`read_file` 比 `file_reader` 更直接,LLM 更容易把"读文件"意图映射到 `read_file`
- **避免含糊词**:`do_thing`、`handle`、`process` 这类词让 LLM 无法判断它具体做什么
- **命名空间化(可选)**:如果你的系统可能接入多个来源的工具,加前缀能避免冲突,例如 `fs__read_file`、`shell__bash`

#### 4.1.2 description 写法

description 是 LLM 决策"要不要调这个工具、什么时候调"的唯一依据。它**不是给开发者看的 API 文档**,而是给 LLM 看的"使用说明书"。

好的 description 通常包含三层信息:

```
[做什么] — 一句话描述核心功能
[什么时候用] — 触发场景、使用前提、不该用的场景
[怎么用] — 关键参数的语义、典型用法、与其他工具的关系
```

举例对比:

```
❌ 差的 description:
"文件工具"
"用于处理文件"

✅ 好的 description:
"读取文件内容并返回文本。
 用于:读取源代码、配置文件、日志等文本文件的内容。
 不要用于:读取二进制文件(图片、视频)、大文件(>1MB,会截断)。
 参数 path:绝对路径或相对于工作目录的路径。"
```

写 description 的几个原则:

- **第一句最重要**:LLM 决策时间短,核心功能必须一句话讲清
- **说"什么时候不用"**:这比"什么时候用"更稀缺,帮 LLM 避免错选
- **给参数加注释**:JSON schema 里的 `description` 字段也要写,LLM 看 schema 时会一并读
- **避免冗长**:200-500 字符最佳;超过 1000 字符 LLM 容易抓不住重点

#### 4.1.3 input_schema 严格度

input_schema 既是 LLM 的"参数说明",也是校验器的"契约"。它的严格度是个权衡:

- **过严**:所有 properties 都列在 `required` 里 → LLM 不敢传可选参数,工具表达力受限
- **过松**:`required: []`,任何字段都可省略 → 校验器需要在业务层补大量默认值

**实践经验**:

- **必填项**用 `required` 明确列出
- **可选项**不列 `required`,但在 `description` 里说明默认值
- **复杂结构**用 `$ref` / `oneOf` / `anyOf` 提高表达力,但要注意 LLM 是否真的理解(多数 LLM 对 `oneOf` 支持不完善)
- **保留语义空间**:schema 只约束**结构**,不约束**值** — 比如 `path` 是 string,不限制"必须以 / 开头";这种限制放在业务校验里更灵活


### 4.2 工具的分类维度

工具多了之后,需要分类管理。常见分类维度:

**按能力**:
- 读取类:`read_file`、`list_dir`、`search_code`
- 写入类:`write_file`、`edit_file`
- 执行类:`bash`、`run_python`
- 通信类:`http_request`、`send_email`
- 复合类(高阶工具):`analyze_log`、`summarize_doc`

**按来源**:
- 内置(builtin):系统自带,稳定可靠
- 远程(MCP):跨进程,延迟更高、可能离线
- 第三方插件:第三方提供,质量参差

**按生命周期**:
- 同步短任务:毫秒级返回,简单 await
- 异步长任务:需要状态查询,可能跨多次对话
- 流式任务:边执行边返回 progress

**分类的目的是管理,不是约束** — 同一工具可以同时属于多个分类(一个 builtin 工具既"读取类"也"短任务")。关键是**在你的注册中心里有元数据记录这些维度**,便于运营、权限、限流等横切关注点。

### 4.3 副作用与幂等性

工具执行通常会改变外部状态(写文件、调 API、删数据)。设计工具时,**副作用的明确性**是核心考量:

- **纯函数工具**:输入相同 → 输出相同,无副作用。`get_current_time`、`compute_hash`。这类工具最安全,可以无脑重试。
- **幂等工具**:重复执行效果相同。`write_file(path, content)`(覆盖写)、`set_config(key, value)`。可以重试,但**仍会真实执行**(有 I/O 开销)。
- **非幂等工具**:重复执行会产生不同效果。`append_log(line)`(每次追加)、`send_email(to, body)`(每次发送)、`increment_counter()`。**这类工具必须谨慎**:
  - 必须在 description 里**明确警告**"每次调用都会真实发送邮件"
  - 必须在执行器层**避免自动重试**(除非调用方明确要求)
  - 最好提供 **dry-run 模式**(`send_email(dry_run=True)` 返回"将要发送"但不真发)

**设计原则**:

- 默认工具是**幂等**或**纯函数**,只把实在无法幂等的操作做成非幂等工具
- 非幂等工具在 description 里写**首次警告**,让 LLM 决策时就知道
- 必要时给工具加 `confirm: bool` 参数,要求 LLM 显式确认才能执行

LLM 不会"理解"你的副作用 — 它只会照 description 的字面意思调。**description 是 LLM 理解副作用的唯一窗口**。

### 4.4 工具的可发现性

LLM 怎么知道有哪些工具可用?答案是**系统 prompt 注入工具列表**。这意味着每次 LLM 调用,工具列表都会作为 context 的一部分发给 LLM。这带来几个权衡:

**全部暴露**:
- 简单:所有工具都在 prompt 里,LLM 知道所有能力
- 代价:工具多时 prompt 变长,消耗 token,且 LLM 选择困难

**按需暴露**:
- 动态:根据用户 prompt 或上下文,只把相关工具发给 LLM
- 代价:实现复杂(需要"相关性判断"逻辑),且 LLM 不知道"还有什么工具可用",可能错失能力

**分层暴露**:
- 核心工具始终在场(读、写、bash)
- 专业工具按需加载(数据库查询、特定 API 调用)
- 平衡了完整性与 token 成本

无论用哪种,**给工具加 category / tag 元数据**便于后续做相关性判断。

---

## 5. 工具注册中心

工具定义是静态的 — 写在代码里、写完不改。但 Agent 系统运行时要回答"现在有哪些工具可用、怎么查、怎么加、怎么删",这些动态问题由**工具注册中心(Tool Registry)**负责。

### 5.1 设计动机

为什么不直接 import 一个工具集合来用?—— 因为生产环境的工具系统面临几个动态需求:

- **动态添加/移除工具**:用户装了新插件,工具集要更新;某个工具废弃,要下线
- **跨来源聚合**:内置工具 + MCP 远程工具,要在同一个系统里被查询
- **元数据查询**:列出"所有读取类工具"、"所有 builtin 工具"用于权限决策或 UI 展示
- **生命周期管理**:工具的加载时机、刷新、缓存、版本

这些需求靠 import 一个 dict 解决不了,需要一个**注册中心抽象**。

### 5.2 核心 API

注册中心的 API 极简,核心就四个动作:

```
register(tool)           # 注册一个新工具
unregister(name)        # 移除一个工具
get(name) -> Tool        # 按名字查找
list() -> list[Tool]     # 列出所有工具(可带过滤条件)
```

每个工具在注册时,会带一个**完整的 Tool 对象**(含 name / description / input_schema / 执行函数 / 元数据)。注册中心不只存"工具的引用",还存"工具的全部信息" — 这让查询、列出、分类都能在一个中心点完成。

**几个 API 设计要点**:

- `register` 要**幂等**:同名重复注册应覆盖,而非抛错(支持热更新)
- `unregister` 要**静默**:不存在的 name 应返回 None / False,而非抛错
- `list` 要支持**过滤**:传 category / tag / 来源 等条件,返回子集
- 内部状态对**调用方只读**:不要让调用方直接修改注册中心内部 dict

**最小骨架**:

```python
class ToolRegistry:
    def register(self, tool): ...
    def unregister(self, name): ...
    def get(self, name): ...
    def list(self, category=None, source=None): ...
```

### 5.3 命名空间与冲突解决

工具多了之后,同名冲突是必然。常见冲突来源:

- 不同来源的内置工具重名(两个模块都提供 `read_file`)
- 用户扩展工具与内置工具重名
- 远程 MCP 工具与本地工具重名

**解决方案**:

**1. 强制命名空间**:`fs__read_file`、`shell__bash`、`mcp__github__create_issue`
- 工具注册时强制带前缀,前缀即命名空间
- 优点:零冲突,一眼看出来源
- 缺点:LLM 调用时 name 变长

**2. 软命名空间 + 优先级**:允许同名,注册时按"优先级"决定暴露哪个
- 用户扩展覆盖内置?内置覆盖用户?取决于策略
- 优点:LLM 仍能用短名
- 缺点:用户难以预测实际调哪个


**实践建议**:**强制命名空间(方案 1)** 是最稳的。LLM 看到 `mcp__github__create_issue` 一眼就懂,不会出现"我调的是哪个 read_file"的歧义。

### 5.4 动态注册与远程工具

注册中心不只是"启动时注册一次"。生产中常见:

- **热加载**:工具配置文件变更,自动重新注册
- **MCP 远程工具**:MCP server 连接后,其工具**动态**注入注册中心
- **插件系统**:第三方插件按需加载到注册中心

MCP 提供了客户端-服务器协议,服务器通过 `tools/list` 声明其工具,客户端通过 `tools/call` 调用。MCP 解决的是**跨进程的工具发现与调用**——服务器启动时静态声明工具列表,客户端无需"注册"这些工具,只要知道服务器地址就能用。本节讨论的 register/unregister/list 是**进程内**注册中心的抽象,适合单进程工具聚合;如果你的工具分布在多个进程或机器,直接对接 MCP。

**热加载的实现要点**:

- 配置文件变更触发注册中心的批量更新(register 新工具 + unregister 失效工具)
- 批量操作要原子:要么全成功,要么全回滚
- 失效工具正在被调用时,不能立即 unregister,等调用完成或超时

### 5.5 元数据暴露与生命周期

注册中心**存储**什么、**暴露**什么、**何时失效**,决定了上层能力(权限、UI、调用统计)的边界。

**元数据维度**(每个 tool 至少携带):

- 身份:name / 别名 / 命名空间
- 行为:description / input_schema / 副作用等级(纯 / 幂等 / 非幂等)
- 来源:builtin / MCP / 用户扩展
- 分类:category / tags(用于过滤和分组)

**运行期统计**(由执行器/监控系统在调用时记录,不是 Tool 自带的):
- 调用次数 / 平均耗时 / 错误率(用于监控和优化)

**生命周期管理**:

- **加载**:启动时从配置 / MCP 同步加载
- **刷新**:配置文件变更或 MCP server 断开重连时,触发 list 刷新
- **降级**:远程工具断连时,标记为 unavailable,LLM 调用时返回明确错误
- **卸载**:工具废弃时,unregister + 等待在途调用结束

工具注册中心作为系统的"工具目录",**对外的可观测性**(列出所有工具、统计每个工具的调用)是横切关注点(权限、审计、UI 展示)的基础。

---

## 6. 工具执行器

工具注册中心负责"有哪些工具",执行器负责"怎么调用工具"。执行器是工具系统的**运行时核心** — tool_call 在这里变成真实的 I/O、文件操作、API 调用。

执行器的设计决定系统能跑多快、能撑多少并发、错了怎么恢复。本章讨论四个核心问题:输入怎么校验、怎么调度执行、错了怎么办、结果怎么回给 LLM。

### 6.1 输入校验

执行器拿到 tool_call 后,不能直接调工具 — 必须先校验。两层校验:

**Schema 校验**(必做):
- 用 jsonschema 库对照工具的 input_schema 校验 arguments
- 失败 → 返回结构化错误给 LLM(`{"error": "Missing required field: path"}`),让 LLM 在下一轮修正

**业务校验**(按需):
- 路径是否在允许范围
- 用户是否有权限
- 参数值是否在合理区间(如 `max_lines > 10000` 应警告)
- 业务校验失败同样返回结构化错误

**Schema 校验的常见坑**:

- **类型对了值错了**:`{"max_lines": "10"}`(字符串)通过 schema 校验,但工具期望 int
- 解决:加 `strict` 模式(jsonschema 支持),或自定义 validator 做严格类型检查
- **默认值被吞**:schema 里写了 `default`,但 jsonschema 默认不填默认值 — 需要显式调用 `apply_defaults` 之类
- **嵌套对象没校验**:`{"user": {"id": 123}}`,只校验外层,内层通不过 — schema 必须递归完整

### 6.2 执行策略

工具怎么跑,**同步还是异步、并行还是顺序**,是性能与正确性的权衡。

**同步 vs 异步**:

- **同步工具**:大多数工具适用(读文件、查数据库)— 简单、可预测
- **异步工具**:长任务适用(爬网页、跑大模型)— 不阻塞 LLM 主流程,通常配合"任务 ID + 状态查询"模式

**并行 vs 顺序**:

LLM 一次可能返回多个 tool_call(并行执行)。常见模式:

- **无依赖**:并行执行,所有结果一起回灌
- **有依赖**:顺序执行,前一个结果作为后一个输入(LLM 自己会处理,通常分成多轮调用)

并行执行的实现要点:

- 用线程池 / 进程池 / async gather
- 限制最大并发数(避免资源耗尽)
- 每个 tool_call 独立捕获异常,不互相影响

**超时**:

- 每个工具有自己的合理超时(读文件 5s,API 调用 30s,LLM 调用 60s)
- 超时后取消执行,返回 `{"error": "timeout"}`
- 超时不应**冒泡到整个 Agent 循环** — 一个工具超时不影响其他工具和 LLM 后续轮次

### 6.3 错误处理与重试

工具执行**几乎一定会失败** — 网络超时、文件不存在、参数错误、权限不足。错误处理的目标是:**让 LLM 能从错误中恢复,而不是让整个对话崩掉**。

**错误分类**:

- **可重试错误**:网络超时、临时 5xx、资源暂时不可用 → 自动重试 1-2 次
- **不可重试错误**:参数错误、文件不存在、权限拒绝 → 不重试,直接返回错误信息

**错误返回格式**(给 LLM 看):

```json
{
  "error": "FileNotFound",
  "message": "/tmp/x 不存在",
  "code": "ENOENT",
  "retryable": false
}
```

关键字段:
- `error`:错误类型(枚举或字符串),LLM 能据此决策
- `message`:人/ LLM 都能读的具体说明
- `retryable**:LLM 能判断"要不要换个参数重试"

**重试策略**:

- 简单重试:同参数立即重试 N 次 — 适合瞬时错误
- 退避重试:每次间隔指数增长 — 避免雪崩
- 不重试:错误明确告诉 LLM "我错了",让 LLM 自己改 — 这是**最优雅的方式**,把决策权交给 LLM

**原则**:

- **副作用工具默认不重试**(`send_email` 不能因为超时重发一封)
- **错误信息要具体** — "File not found" 比 "Error" 有用一万倍
- **绝不静默吞错** — 任何错误都要返回结构化结果,让 LLM 看到

### 6.4 结果标准化

工具执行结果是五花八门的(字符串、字典、二进制、文件路径、网络响应),LLM 只能消化**结构化文本**。执行器的最后一步是把这些归一化。

**标准化原则**:

- **永远返回 JSON 可序列化的对象**(dict / list / str / number / bool / None)
- **二进制转 base64 或 URL** — 不在 LLM context 里塞二进制
- **超长文本截断** — 默认截断前 N 行 / 前 M 字符,避免 LLM context 爆炸
- **截断要明确标记** — 返回 `{"content": "...", "truncated": true, "total_lines": 10000}` 让 LLM 知道

**结构推荐**:

```python
# 成功结果
{
  "content": "...",           # 主内容(LLM 主要看的)
  "metadata": {...},          # 辅助信息(行数、字节数、耗时)
  "truncated": false          # 是否截断
}

# 错误结果
{
  "error": "type",
  "message": "...",
  "retryable": false
}
```

**流式结果**:对于长任务,可以在执行过程中**边执行边推 progress** — 用 SSE / WebSocket 把进度推给 UI,LLM 上下文则用"任务已启动,ID 是 xxx,稍后查询"等紧凑形式。最终结果查询时再用上面结构返回。

---

## 7. 工具设计模式

前面六章讲了"怎么造工具系统的零件"(定义 / 注册 / 执行 / 协议 / 思想)。本章上升到"怎么设计工具本身" — 不是单个工具怎么写,而是**一类工具的通用设计模式**。

工具设计模式解决的是**重复出现的工具类型**:长任务怎么暴露给 LLM、非幂等操作怎么表达、复合操作怎么拆解。这些模式不来自某个项目,而是从大量工具设计实践中沉淀出来的。

### 7.1 原子工具 vs 复合工具

**原子工具**:做一件最小的事。`read_file(path)`、`bash(command)`、`http_get(url)`。每个都不可再拆。

**复合工具**:把多个原子工具串成一个业务操作。`git_commit(message, files)` 内部是 `git add` + `git commit` + 输出 commit hash。

**两者的权衡**:

| 维度 | 原子工具 | 复合工具 |
|------|---------|---------|
| LLM 选择准确性 | 高(功能单一明确) | 低(LLM 难以预知内部副作用) |
| 表达力 | 弱(LLM 要串多次) | 强(一步完成业务) |
| 错误粒度 | 细(每步独立错误) | 粗(整个复合失败) |
| 调试 | 易(单步可重放) | 难(中间状态难捕获) |

**实践原则**:

- **默认原子工具** — 让 LLM 自己组合,避免工具集爆炸
- **复合工具用于高频场景** — "git commit" 调用频率高,值得一个复合工具
- **复合工具内部应可观测** — 输出每个子步骤的中间结果,方便调试
- **复合失败要细化** — 不要只返 "failed",而要告诉 LLM "git add 成功但 commit 失败:xxx"

### 7.2 长任务工具

短工具(读文件、查数据库)同步返回。但有些工具执行**秒级到分钟级**:跑模型训练、爬全站、上传大文件。同步等待会阻塞 LLM 推理,体验极差。

**长任务的标准模式**:**异步启动 + 状态查询**。

```
LLM: 调 train_model(dataset, params)
  → tool 立即返回 {"task_id": "abc123", "status": "started"}
  ↓ (LLM 继续思考或问用户下一步)
LLM: 调 query_task(task_id="abc123")
  → tool 返回 {"status": "running", "progress": "0.6"}
  ↓ (轮询或用户等)
LLM: 调 query_task(task_id="abc123")
  → tool 返回 {"status": "completed", "result": {...}}
```

**长任务工具的设计要点**:

- **接受 `task_id`** 作为可空参数,None 表示"启动新任务",有值表示"查询已有任务"
- **状态机**(理想化,实际可能更复杂):

  ```
  pending → running → (completed | failed | cancelled)
                  ↑↓
                 retry (failed 可重试转回 running)
  ```

  实际生产还需要考虑:`pending`(排队,资源池满时)、`failed → running` 重试转移、`cancelled` 可在任意中间态触发。这里给出的是**最小可用状态机**,作为设计起点。

- **进度信息**:返回 `progress` 字段(0-1 或百分比),LLM 能向用户报告
- **超时与取消**:查询时支持 `cancel: true` 参数
- **持久化**:任务状态存到数据库(不能只存内存,服务重启会丢)

**陷阱**:

- LLM 不知道什么时候该轮询 — 在 description 里说明"启动后请稍候几秒再查询"
- 长任务不应阻塞 LLM 主循环 — 注册中心要标记"async",Agent 循环不应 await

### 7.3 幂等包装模式

**非幂等操作**(`send_email`、`append_log`)重复执行会出问题。但很多场景下 LLM 真的会重复调(网络重试、LLM 自己想确认)。**幂等包装**是给非幂等操作加防护。

**常见模式**:

- **idempotency_key**:LLM 传一个唯一 ID,服务端记录已处理的 ID,重复请求直接返回上次结果
- **dedup window**:短时间内同参数的请求只执行一次
- **状态检查**:执行前检查"是否已经做过",做过则跳过

```
send_email(to, body, idempotency_key="req_001")
  → 第一次:真发送,返回 {"sent": true, "message_id": "..."}
  → 第二次(同 key):跳过发送,返回上次结果 {"sent": true, "message_id": "...", "deduplicated": true}
```

**实践建议**:

- 幂等 key 由 LLM 生成(每次新调用都用 UUID)
- 缓存窗口至少 10分钟
- description 里明确说明"提供 idempotency_key 可防重复发送"

### 7.4 工具内省工具

LLM 看不到工具系统的"代码",但有时它需要**发现自己有哪些工具、每个工具的详细参数**。**工具内省工具**让 LLM 自助查询:

- `list_tools(category="file")` → 返回所有文件类工具的 name + 一句话描述
- `get_tool_schema(name="bash")` → 返回 bash 工具的完整 schema + 详细参数说明

**何时有用**:

- 工具很多时,LLM 可能不确定某个细节
- LLM 想确认参数的具体含义
- 调试 / 用户问"你能做什么"时,LLM 列出可用工具

**实践建议**:

- 内省工具本身也要走注册中心(不要特殊处理)
- 返回信息要精简(避免 LLM context 爆炸)
- 默认不暴露内部细节(版本号、统计)— 这些不该让 LLM 看到

---

## 8. 可观测性

工具系统跑生产后,出问题时最常听到的就是"为什么 LLM 调错了"—— 没有可观测性,定位就是盲猜。

本章讲三件事:怎么追踪一次完整调用、关注哪些关键指标、出问题时怎么调试和重放。

### 8.1 调用链追踪

一次 Agent 循环里可能调多个工具,**每次调用都要打点**,才能事后看清完整链路。

**核心要素** — 每次调用至少记这些:

- **时间戳**(开始 / 结束 / 持续时长)
- **trace_id** — 一次 Agent 循环的所有调用共享一个 ID,方便聚合
- **span_id** — 单次调用的唯一标识,可串联父子关系(并行工具调用、嵌套工具)
- **tool name + arguments + result + error**(结构化,不要散装字符串)

**结构** — 用 OpenTelemetry 风格的 trace:

```
trace_id=abc123
├─ span: read_file (12ms)         args={path: "/tmp/x"}  result={...}
├─ span: bash (230ms)            args={command: "ls"}   result={...}
└─ span: read_file (8ms)         args={path: "/tmp/y"}  result={...}
```

**实现** — 在执行器层**统一埋点**,不让每个工具自己打:

- 进入 execute 时记录开始时间 + 参数
- 退出时记录结束时间 + 结果(或异常)
- 异常也要捕获,不能因日志失败影响主流程

**关联 LLM 调用** — 把工具 trace_id 与上游 LLM 调用 trace_id 关联,这样能从一次用户提问追到所有工具调用、最终答案。

### 8.2 关键指标

光看单次日志不够,**聚合指标**才能发现问题。关注这四类:

**调用量**:

- 总调用次数 + 按工具拆分
- 时间分布(每秒调用、峰值时段)

**性能**:

- 平均 / p50 / p95 / p99 延迟
- 按工具拆分(找出最慢的工具)
- 长尾(超 1s 的调用占比)

**质量**:

- 成功率(成功 / 总数)
- 按工具拆分的错误率
- 错误类型分布(FileNotFound vs Timeout vs PermissionDenied)

**成本**(如果接 LLM):

- 工具调用消耗的 token(主要是 description 和 result)
- 每次 Agent 循环的工具调用次数(衡量 Agent 效率)

**指标实现** — 用 Prometheus / OpenTelemetry Metrics:

- Counter:调用次数(单调递增)
- Histogram:延迟分布(自动算 p50/p95/p99)
- Gauge:当前并发数(瞬时值)

**告警** — 给关键指标设阈值:

- 错误率 > 5% → 告警
- p95 延迟 > 5s → 告警
- 某工具 5 分钟无调用 → 可能已坏

### 8.3 调试与重放

出问题时,定位路径是:

1. **用户报告"这个回答不对"**
2. **找到那次对话的 trace_id**(通过用户标识 / 时间)
3. **回放 trace 看所有工具调用**——哪个参数错了?哪个超时了?
4. **必要时重放** — 用同样的输入重跑 LLM + 工具,看结果

**重放能力的最低要求**:

- **保存完整 trace**:tool name + args + result + 时间 + 上下文(用户 prompt、LLM 响应)
- **保存输入快照**:不仅是 args,还有触发这次调用的完整 LLM context(用于"为什么 LLM 选了这个工具")
- **工具可替换**:重放时,危险工具(发邮件、删文件)用 mock 替换

**实践技巧**:

- **加 trace_id 到 UI**:用户在界面看到"这次对话"时,旁边显示 trace_id,报问题时直接给这个 ID
- **加 replay 入口**:调试时能输入 trace_id,系统重放并显示每步结果
- **定期清理 trace**:全量保存成本高,按"重要性 + 时间"采样保留(典型:全量保留 7 天,采样保留 30 天)

**反模式**:

- 出问题时只能看日志文件 grep "ERROR"(没 trace_id 关联)
- 重放时直接真调危险工具(`send_email` 把邮件发出去)
- 工具 trace 与 LLM trace 各自独立、无法关联

---

## 9. 总结

### 当前生态全景

工具调用这个领域,过去两年变化很快:

- **协议标准化**:JSON Schema 几乎一统工具参数描述;OpenAI / Anthropic / Google 三家虽有差异,但都围绕同一套概念
- **MCP 协议成熟**:工具动态注册和跨进程发现有了标准化方案,生态正在收敛
- **多模态扩展**:图像、音频、视频工具逐步接入,工具系统从纯文本走向富媒体
- **Agent 框架百花齐放**:LangChain、AutoGen、CrewAI 等提供不同抽象层级的工具集成,但底层概念一致

### 几个问题需要关注

几个领域公认还没解:

**工具检索 / 选择** — 工具超过几十个时,LLM 难以选择。动态注入、按场景过滤是工程实践,但**没有通用方案**。类似 RAG 的"工具检索"子系统值得探索。

**工具组合推理** — 多步工具调用时,LLM 缺乏"我先做 A 拿结果,再用结果做 B"的稳定推理能力。链式调用能力不稳定。

**安全性边界** — 工具能读写文件、调 API、发邮件,**LLM 决策的可靠性决定了风险大小**。权限、沙箱、审计是工程可以缓解。

**成本控制** — 上下文税、LLM 调用成本、工具执行成本三者在大型 Agent 系统里相互影响,**没有标准的成本模型**指导架构选择。


工具系统是 Agent 时代的 API 设计。和传统 API 不同,工具的消费者是 LLM,不是开发者 — 这改变了所有设计直觉。**但底层原则没变**:清晰的契约、可靠的执行、可观测的运行。