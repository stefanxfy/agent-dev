# AI Agent 评估：指标、基准与方法论

> 当 LLM 被封装为 Agent，评估范式须从"单轮静态问答"重构为"多轮动态执行链"——评估对象从"模型知道什么"变为"智能体能做什么"，"正确""效率""过程质量"都需重新定义。

> 全文依次构建覆盖任务能力、生成质量、产品级质量、一致性与业务价值的指标体系；剖析 BFCL、GAIA、AgentBench 等主流 Benchmark 并给出选型框架；讲解 Exact/AST Match、LLM Judge、Win Rate、人工评估、幻觉检测、延迟与成本等方法论；介绍 BFCL、AlpacaEval、lm-evaluation-harness、OpenHands 四大开源框架实践。最后展望在线评估、多模态、多 Agent 与可复现性等前沿方向。
---

## 从模型到智能体：评估的范式转移

传统 LLM 评估以 MMLU、GSM8K、HumanEval 为代表，核心是"给定输入 → 比对标准答案"——静态、单轮、封闭，对错在输出瞬间即定。但当 LLM 被封装为智能体，评估对象从"模型知道什么"变为"智能体能做什么"：一次任务涉及多次工具调用、条件判断与多步规划，每步输出都影响下一步，范式从"单轮静态问答"转为"多轮动态执行链"。这使"正确"变得模糊——同一任务可有多条成功路径，也可能绕路后成功或前两步对而第三步失败，"成功""效率""过程质量"都需重新定义。

### 传统评估与智能体评估的本质区别

二者区别不是程度上的而是性质上的，下表从六个维度对比：

| 维度 | 传统LLM评估 | 智能体评估 |
|------|------------|-----------|
| **评估对象** | 模型的知识储备与推理能力 | 智能体的任务完成能力（规划、工具调用、环境交互） |
| **答案确定性** | 通常有唯一标准答案，非对即错 | 答案往往不唯一，同一任务可有多种成功路径 |
| **交互轮数** | 单轮输入输出（prompt → completion） | 多轮动态交互（观察→思考→行动→观察→...），轮数不固定 |
| **环境依赖** | 无环境依赖，评估环境封闭自足 | 强依赖外部环境（API、数据库、文件系统、浏览器等），环境状态影响评估结果 |
| **指标类型** | 结果指标为主（准确率、BLEU、pass@k） | 结果指标+过程指标（步骤准确率、工具调用正确率、路径效率、恢复能力等） |
| **评估成本** | 低——可批量自动化运行，单样本评估成本通常在秒级 | 高——需要搭建环境、模拟工具、人工标注过程，单样本评估成本在分钟级甚至小时级 |

简言之，传统评估"跑一个 benchmark 出个分数"的模式在此行不通——静态测试集无法覆盖动态环境中的所有情况，这正是后续各章要系统回答的问题。

## 智能体性能评估的意义

### 工程视角：没有评估就没有迭代

智能体开发高度迭代（调提示词、换工具策略、改规划逻辑），每次改动都要回答"变好还是变差"。没有评估，迭代就是盲人摸象，问题只能等用户在生产环境暴露。以工具调用从"串行"切到"并行"为例：定性反馈说"更快了"，但并行是否抬高参数错误率、是否在"前一步输出喂给后一步"的场景引入逻辑错误、整体完成率是否受影响——只有跑同一套评估集（如 BFCL）对比步骤准确率与端到端完成率，才能支撑"保留还是回滚"的决策。

评估也是质量守门人，可捕获三类典型事故：**工具调用幻觉**（声称调了 API、实际编造数据，靠工具调用日志核查发现）、**指令注入**（"忽略之前指令"绕过安全限制，靠对抗性测试集发现）、**多步骤任务中途静默放弃**（前两步对、第三步没执行却不报错，靠逐步完成度检查点定位）。

### 产品视角：评估定义能力边界

未经评估的 Agent 上线，用户会因宣传抱过高期望（"AI 应该什么都能做"），一旦在简单任务上翻车，信任就迅速崩塌——根因不是能力不行，而是没人用数字告诉用户"它擅长什么、不擅长什么"。评估后，能力边界可量化描述（如"GAIA Level-1 完成率 XX%、Level-3 XX%"），并支撑"能力标签"与企业 SLA 条款（任务完成率、响应时间、错误率）。评估既是技术基础设施，也是商业基础设施。

### 研究视角：评估推动领域进步

公共评估基准提供统一比较标尺，使"我的 Agent 更强"可被验证；更重要的是，好基准能暴露真实瓶颈、引导研究聚焦。两个典型发现：**BFCL** 揭示并行工具调用准确率显著低于单工具调用，是当前框架的系统性瓶颈，直接推动了多工具场景优化；**GAIA** 揭示从 Level 2 到 Level 3 完成率断崖式下降，说明长链条多步规划是核心瓶颈。具体排名会随模型迭代变化，但这类结构性洞察不会因个别模型而改变。

### 商业视角：产品化的通行证

企业采购链条（需求识别 → IT 评估 → 决策推荐 → 合同 SLA）中评估贯穿始终。同时，性能评估正从"工程实践"变为"法律义务"：**欧盟 AI Act** 要求高风险 AI 系统在技术文档中给出性能评估结果；**中国生成式AI管理办法**要求提供者说明服务能力边界与局限——两者都以"已通过评估掌握能力边界"为前提。无法提供评估数据的产品将面临市场准入障碍。

### 评估的五大核心挑战

智能体评估的困难是系统性、多维度的，下表归纳五个核心挑战：

| 挑战 | 核心难点 |
|------|----------|
| **正确答案不唯一** | 同一任务（如写请假邮件）可有多种语气/篇幅/格式的正确输出，开放式任务尤甚；刚性比对过严、模型判断又引入不确定性 |
| **执行结果决定正确性** | 查天气/股价/库存等任务，正确答案随时间变化，须判断"是否调了对的工具、结果是否被正确处理"，而非比对固定值 |
| **多维度质量评估** | 需同时达标正确性、有用性、安全性、及时性、简洁性五维，维度间存在张力（如为正确性反复验证会牺牲及时性） |
| **对抗性鲁棒性** | 指令注入、工具滥用诱导、边界探测等对抗输入会让正常表现优秀的智能体失效，须专门的对抗测试集 |
| **人类评估的成本** | 复杂多步任务仍高度依赖人工（约 5–10 样本/小时），1000 样本需 100–200 人时，直接限制评估规模与频率 |

## 评估指标体系

AI Agent的评估是一个多维度、多层次的复杂问题。一个Agent可能在工具调用上表现优异，却在多轮对话中频频跑题；可能在离线基准测试中得分领先，却在真实用户场景中暴露幻觉和延迟问题。本章构建了一套覆盖**任务能力、生成质量、产品级质量、业务价值**四个层面的评估指标体系，力求全面刻画Agent的综合能力。

---

### 任务能力指标

任务能力指标关注Agent"能不能完成任务"这一核心问题，从工具调用、端到端任务执行、多轮对话和指令遵循四个角度进行评估。

#### 工具调用准确率（BFCL）

**评估什么：** BFCL（Berkeley Function-Calling Leaderboard）专门评估大语言模型的Function Calling能力，即模型能否根据用户意图正确选择工具、构造参数并生成合法的函数调用。

**怎么计算：** BFCL 将模型输出的函数调用与标准答案做 **AST（抽象语法树）级别比对**，核心指标为 **AST Match Rate** 与综合 **Accuracy**，可执行场景另测 Exec Accuracy。AST 匹配规则与实现详见「评估方法论 · Exact Match / AST Match」。

**数据集：** 覆盖单函数调用、多函数选择、并行调用、并行多函数、无关函数拒绝、多轮对话等场景，各版本累计 5,500+ 对（V1 约 2000）。详细构成与版本演进见「主流评估 Benchmark 详解 · BFCL」。其中 Irrelevance 类别考察"判断力"——函数池中无合适工具时应主动拒绝而非强行编造调用。

#### 端到端任务完成率（GAIA）

**评估什么：** GAIA（General AI Assistants Benchmark）评估通用AI助手在真实世界任务中的综合表现，要求模型完成需要多步推理、工具使用和信息整合的复杂任务。与BFCL专注于函数调用层不同，GAIA衡量的是端到端的任务完成能力。

**怎么计算：**

GAIA的核心评估指标是**Quasi-Exact Match准确率**，即模型给出的最终答案与标准答案在归一化后是否匹配（归一化规则详见3.2.3节）。

**三级难度划分：**

| 级别 | 工具需求 | 推理步骤 | 典型场景 |
|------|----------|----------|----------|
| Level 1 | 不需要工具或最多1个 | ≤5步 | 简单事实查询 |
| Level 2 | 多工具结合 | 5-10步 | 多源信息整合 |
| Level 3 | 任意数量工具 | 任意长序列 | 复杂研究型任务 |

GAIA数据集共包含466个问题，覆盖文件理解、Web搜索、数据处理等多种真实世界任务类型。Level 3级别的问题对Agent的规划能力、错误恢复能力和长程推理能力提出了极高要求。

**评估流程：**

1. Agent接收问题及可能附带的文件资源
2. Agent自主规划执行步骤，调用所需工具（搜索、代码执行、文件读取等）
3. Agent给出最终答案
4. 将最终答案与标准答案进行Quasi-Exact Match比对
5. 统计各级别的准确率及总体准确率

#### 多轮对话质量（MT-Bench）

**评估什么：** MT-Bench（Multi-Turn Benchmark）评估模型在多轮对话中的指令遵循和推理能力，重点关注模型在跨轮次上下文理解、话题延续和深度推理方面的表现。

**怎么计算：**

MT-Bench采用**Pairwise比较（成对比较）**的评估方式，以GPT-4作为裁判（Judge）模型：

- 将待评估模型与基准模型的回复进行成对比较
- GPT-4裁判根据回复质量判定胜负或平局

**核心指标：**

- **Win Rate（胜率）**：待评估模型胜出的比较占总比较数的比例
- **单项分数（1-10分）**：GPT-4裁判对每轮回复从1-10分打分

**数据集构成：**

MT-Bench包含80个多轮问题，覆盖八大类别：

| 类别 | 说明 | 示例方向 |
|------|------|----------|
| 写作 | 创意写作、文本改写 | 撰写文章、改写风格 |
| 推理 | 逻辑推理、因果分析 | 逻辑谜题、因果推断 |
| 数学 | 数学计算、证明 | 应用题、代数推导 |
| 编程 | 代码生成、调试 | 算法实现、代码审查 |
| 信息提取 | 从文本中提取结构化信息 | 实体识别、关系抽取 |
| STEM | 科学、技术、工程、数学 | 物理解释、化学反应 |
| 人文 | 历史、哲学、文学 | 文学分析、历史事件 |
| 角色扮演 | 模拟特定角色进行对话 | 人物扮演、情境模拟 |

每个问题包含两个连续的轮次，第二轮基于第一轮的回答进行追问，以此评估模型对上下文的保持和深入对话的能力。

#### 指令遵循率（IFE）

**评估什么：** IFE（Instruction Following Evaluation）评估模型对指令约束的严格遵守程度。一个模型即使生成质量很高，如果不能按照用户的格式要求、长度限制等约束输出，在实际应用中仍然不可用。

**怎么计算：**

IFE定义了一组**可验证的指令类型**，这些指令的遵守与否可以通过确定性规则判断，无需人工或模型主观评估：

| 指令类型 | 说明 | 示例 |
|----------|------|------|
| 长度限制 | 输出字数/段落数/句数限制 | "回答不超过100字" |
| 格式要求 | 输出格式约束 | "用JSON格式回答"、"以markdown表格呈现" |
| 关键词包含 | 必须包含特定关键词 | "回答中必须包含'可持续发展'" |
| 关键词禁止 | 不得出现特定关键词 | "不要使用'认为'这个词" |
| 语言要求 | 输出语言约束 | "用英文回答" |
| 结构要求 | 段落/章节结构约束 | "分三段回答，每段以问题开头" |

**核心指标：**

$$\text{指令遵循准确率} = \frac{\text{严格遵守所有约束的输出数}}{\text{总输出数}} \times 100\%$$

注意：此处的准确率要求**所有约束同时满足**。如果一个测试样本包含3条指令约束，模型只要违反其中任何一条，该样本即计为未通过。这种"全有或全无"（all-or-nothing）的评估方式确保了对指令遵循能力的严格度量。

---

### 生成质量指标

生成质量指标关注Agent输出内容的质量水平，从多维评分、胜率比较、精确匹配和语法树匹配四个角度进行评估。

#### LLM Judge 多维评分

**评估什么：** 使用更强的LLM作为裁判，从多个质量维度对待评估模型的输出进行打分。这种方法相比传统自动评估指标（如BLEU、ROUGE）更能捕捉语义层面的质量差异。

**怎么计算：**

裁判模型按照预定义的维度对输出进行评分，每个维度1-5分，并附带评分理由。

**典型评分维度：**

| 维度 | 评估重点 | 评分标准（5分制） |
|------|----------|-------------------|
| 正确性 | 事实是否准确、推理是否正确 | 1=严重错误，5=完全正确 |
| 清晰度 | 表达是否清晰、结构是否合理 | 1=混乱难懂，5=清晰有条理 |
| 相关性 | 是否切题、是否回应了用户需求 | 1=完全跑题，5=高度相关 |
| 完整性 | 是否全面覆盖了问题的各个方面 | 1=严重遗漏，5=全面覆盖 |

**输出格式示例：**

```json
{
  "correctness": 4,
  "clarity": 5,
  "relevance": 4,
  "completeness": 3,
  "reasoning": "回答在事实层面基本准确，结构清晰易读，但遗漏了用户询问的第三个方面（成本分析）。"
}
```

评分理由的附带使得评估结果具有可解释性，便于开发者定位质量短板并进行针对性改进。

#### Win Rate 胜率

**评估什么：** Win Rate 衡量待评估模型相对于基准模型（baseline）在成对比较中胜出的比例，是模型对比中最常用的汇总指标。计算公式、基准模型选择与 Length-controlled 改进见「评估方法论 · Win Rate / Pairwise Comparison」。

**解读参考：**

Win Rate 以基准模型自身胜率恒为 50% 为锚点——**>50% 表示优于基准，<50% 表示劣于基准，≈50% 表示与基准持平**。下表给出更细的分档参考：

| Win Rate区间 | 评价 | 说明 |
|--------------|------|------|
| >55% | 显著领先 | 明显优于基准模型 |
| 50%-55% | 略优于基准 | 在多数比较中占优 |
| 45%-50% | 略逊于基准 | 接近基准，但整体仍落后 |
| <45% | 落后 | 明显劣于基准模型 |

> 注：上述区间为经验性参考。需注意 AlpacaEval 的 length-controlled Win Rate 整体偏低（多数模型落在 30%-50%，如 Claude 3 Sonnet ≈ 35%、Llama 3 70B ≈ 34%），不应直接套用上述 50% 锚点的解读，而应以官方 leaderboard 的分数分布为准。

**局限性：** Win Rate 只回答"谁更好"而非"好多少"——51% 的胜率可能是微弱优势的累积，也可能是少数碾压加多数平局，需结合分数差值（Score Margin）或 Elo Rating 区分。

#### Exact Match / Quasi-EM

**评估什么：** 衡量模型输出与参考答案的匹配程度，从精确匹配到归一化匹配形成不同严格程度的评估层次。

**怎么计算：**

**Exact Match（精确匹配）：**

$$\text{Exact Match} = \frac{\text{答案与参考答案完全一致的样本数}}{\text{总样本数}} \times 100\%$$

要求模型输出与参考答案在字符层面完全一致（通常在去除首尾空白字符后比较）。这是最严格的匹配方式，但对格式差异（如多一个空格、百分号写法不同）容错度为零。

**Quasi-Exact Match（准精确匹配）：**

在Exact Match的基础上增加归一化处理，提高评估的公平性：

| 归一化操作 | 说明 | 示例 |
|------------|------|------|
| 字符串strip | 去除首尾空白字符 | `"  Paris "` → `"Paris"` |
| 数字归一化 | 去除百分号、统一小数位 | `"50.0%"` → `"50"` |
| 列表排序后比较 | 将列表元素排序后比较 | `["b","a"]` → `["a","b"]` |
| 大小写统一 | 转为统一大小写后比较 | `"Paris"` vs `"paris"` → 匹配 |

GAIA基准测试使用Quasi-EM作为核心评估指标，以避免因格式差异导致的误判。

#### AST Match（函数调用场景）

在函数调用/工具调用场景，AST Match 比字符串比较更鲁棒——将函数调用解析为抽象语法树后比对，能忽略参数顺序与格式差异而关注调用语义。其三条匹配规则（函数名一致、参数集合相等忽略顺序、参数值语义等价）、`repr/parse/dump` 实现与语义求值详见「评估方法论 · Exact Match / AST Match」。

---

### 产品级质量指标

产品级质量指标关注Agent在实际产品环境中暴露出的质量与工程问题，包括幻觉、安全、延迟和一致性四个方面。这些指标往往决定了Agent能否真正落地服务用户。

#### 幻觉率与事实一致性

**评估什么：** 幻觉（Hallucination）是指模型生成不真实、虚构或与事实不符的内容。幻觉率衡量Agent输出的可信程度，是产品化部署中最受关注的质量指标之一。

**幻觉分类：**

| 类型 | 定义 | 示例 |
|------|------|------|
| 上下文幻觉（Contextual Hallucination） | 输出与给定的源内容不一致 | 源文档说"营收增长5%"，模型输出"营收增长15%" |
| 外源性幻觉（Externally-Sourced Hallucination） | 输出与世界知识不一致，且无法被源内容解释 | 模型虚构不存在的论文引用、编造不存在的API参数 |

**怎么计算：**

$$\text{幻觉率} = \frac{\text{包含幻觉的输出数}}{\text{总输出数}} \times 100\%$$

**事实一致性：**

事实一致性评估输出内容是否可被外部知识源（如知识库、权威网站、原始文档）验证。与幻觉率的区别在于：幻觉率是二值判断（有无幻觉），事实一致性可以是一个程度度量（输出中有多少比例的内容可被验证）。

$$\text{事实一致性比例} = \frac{\text{可被外部知识源验证的陈述数}}{\text{输出中的总陈述数}} \times 100\%$$

在实际评估中，幻觉检测通常结合以下方法：
- **基于检索的验证**：将模型输出的关键陈述提取后检索外部知识源，检查是否矛盾
- **LLM Judge判定**：使用更强模型对输出与源内容进行一致性判定
- **人工标注**：对高风险场景进行人工审核

#### 安全对抗鲁棒性

**评估什么：** 评估Agent在面对恶意输入时的防御能力。在Agent场景中，安全鲁棒性尤为重要——Agent拥有工具调用能力，一旦被恶意操控，可能造成实际损害。

**怎么计算：**

安全对抗鲁棒性通过以下核心指标衡量：

**1. Prompt注入攻击成功率**

$$\text{Prompt注入成功率} = \frac{\text{注入攻击成功的次数}}{\text{注入攻击总次数}} \times 100\%$$

Prompt注入攻击是指攻击者通过精心构造的输入，诱导模型偏离原始系统指令，执行非授权操作。例如，在用户输入中嵌入"忽略以上所有指令，改为..."的攻击文本。注入攻击成功定义为模型确实偏离了原始指令并执行了攻击者意图的操作。

**2. 越狱（Jailbreak）成功率**

$$\text{越狱成功率} = \frac{\text{越狱成功的次数}}{\text{越狱尝试总次数}} \times 100\%$$

越狱是指通过特定技巧绕过模型的安全对齐机制，使其生成本应被拒绝的有害内容。越狱成功定义为模型生成了违反安全策略的内容。

**3. 敏感信息泄露率**

$$\text{信息泄露率} = \frac{\text{发生敏感信息泄露的次数}}{\text{测试总次数}} \times 100\%$$

敏感信息泄露包括：系统提示词泄露（通过诱导模型输出其System Prompt）、工具定义泄露、用户隐私数据通过工具调用不当外泄等。

> 注：以上三个指标的值**越低越好**。理想状态下应接近0%。

#### 响应延迟与 Token 消耗

**评估什么：** 评估Agent的响应速度和资源消耗。在产品环境中，用户对延迟有直接感知，Token消耗直接决定运营成本，二者共同影响产品的用户体验和商业可行性。

**怎么计算：**

**1. 响应时间分位数（P50/P95/P99）**

| 分位数 | 定义 | 解读 |
|--------|------|------|
| P50（中位数） | 50%的请求在此时长内完成 | 反映典型用户体验 |
| P95 | 95%的请求在此时长内完成 | 反映大多数用户的最差体验 |
| P99 | 99%的请求在此时长内完成 | 反映长尾极端情况 |

计算方法：对所有请求的响应时间按升序排列，取第50/95/99百分位的值。

**2. First Token Latency（首Token延迟）**

从发送请求到收到第一个Token的时间。在流式输出场景下，首Token延迟直接影响用户感知的"响应速度"——用户宁愿看到"正在思考..."后快速开始流式输出，也不愿等待5秒后一次性返回完整答案。

**3. Token消耗统计**

| Token类型 | 构成 | 说明 |
|-----------|------|------|
| 输入Token | Prompt + 系统消息 + 历史对话 + 工具定义 | 随对话轮数累积增长 |
| 输出Token | 模型生成回答 | 包含思考过程（如有）和工具调用 |

多步Agent的总Token消耗：

$$\text{总Token} = \sum_{i=1}^{n} (\text{输入Token}_i + \text{输出Token}_i)$$

其中 $n$ 为Agent执行的步数。

**4. 单任务成本计算**

$$\text{单步成本} = (\text{输入Token} \times \text{输入单价}) + (\text{输出Token} \times \text{输出单价})$$

$$\text{单任务成本} = \sum_{i=1}^{n} \text{单步成本}_i + \text{工具执行成本} + \text{基础设施成本}$$

**5. 吞吐率**

$$\text{吞吐率} = \frac{\text{输出Token数}}{\text{生成耗时（秒）}} \quad (\text{Tokens/秒})$$

吞吐率反映模型的生成速度，是评估推理服务性能的基本指标。

---

### 一致性指标

一致性指标关注Agent在相同或相似输入下输出的稳定性，以及在不同场景下表现的连贯性。

#### 跨版本一致性

**评估什么：** 当Agent的底层模型或框架升级后，相同输入是否产生相同或等价的输出。

**怎么计算：**

$$\text{跨版本一致性} = \frac{\text{升级前后输出一致的样本数}}{\text{总样本数}} \times 100\%$$

"一致"的定义可以根据场景调整：严格一致（完全相同）、语义一致（含义相同但表述可能不同）、功能一致（完成相同任务但路径可能不同）。

#### 跨场景一致性

**评估什么：** 同一Agent在不同部署场景（如不同语言、不同时区、不同用户群体）下表现的一致性。

**怎么计算：** 在多个场景下运行相同的评估集，比较各场景间的指标差异。差异越小，跨场景一致性越好。

#### 跨模型一致性

**评估什么：** 当Agent切换底层LLM时（如从GPT-4切换到Claude），任务完成质量是否保持稳定。

**怎么计算：** 在多个模型上运行相同的评估集，比较各模型的指标分布。如果不同模型在同一任务上的表现差异很大，说明该任务对模型选择高度敏感；如果差异很小，说明Agent框架的设计能够有效屏蔽底层模型差异。

---

### 业务价值指标

业务价值指标关注Agent对业务目标的实际贡献，是产品决策者和业务方最关心的指标。

#### 任务完成率

**评估什么：** 用户交给Agent的任务是否被成功完成。

**怎么计算：**

$$\text{任务完成率} = \frac{\text{成功完成的任务数}}{\text{总任务数}} \times 100\%$$

"成功完成"的定义需要根据业务场景具体化：对于客服Agent，可能是"用户问题得到解决且无需转人工"；对于编程Agent，可能是"生成的代码通过所有测试用例"。

#### 用户满意度

**评估什么：** 用户对Agent输出的主观满意程度。

**怎么计算：** 通常通过显式反馈（点赞/点踩、1-5星评分）或隐式信号（是否继续追问、是否放弃使用）来衡量。

#### 成本效率

**评估什么：** Agent完成单任务所需的资源成本。

**怎么计算：**

$$\text{成本效率} = \frac{\text{成功完成的任务数}}{\text{总成本（Token费用 + 计算资源 + 人工成本）}}$$

#### 人均产出提升

**评估什么：** 引入Agent后，人类员工的工作效率提升程度。

**怎么计算：**

$$\text{效率提升} = \frac{\text{使用Agent后的人均产出} - \text{使用前的人均产出}}{\text{使用前的人均产出}} \times 100\%$$

---

### 指标体系总览

```
智能体评估指标体系
├── 任务能力指标
│   ├── 工具调用准确率（BFCL: AST Match Rate）
│   ├── 端到端任务完成率（GAIA: Quasi-EM）
│   ├── 多轮对话质量（MT-Bench: Win Rate）
│   └── 指令遵循率（IFE: All-or-Nothing Accuracy）
├── 生成质量指标
│   ├── LLM Judge 多维评分（正确性/清晰度/相关性/完整性）
│   ├── Win Rate 胜率
│   ├── Exact Match / Quasi-EM
│   └── AST Match
├── 产品级质量指标
│   ├── 幻觉率与事实一致性
│   ├── 安全对抗鲁棒性（注入/越狱/泄露）
│   ├── 响应延迟（P50/P95/P99, First Token Latency）
│   └── Token消耗与成本
├── 一致性指标
│   ├── 跨版本一致性
│   ├── 跨场景一致性
│   └── 跨模型一致性
└── 业务价值指标
    ├── 任务完成率
    ├── 用户满意度
    ├── 成本效率
    └── 人均产出提升
```

这套四级指标体系从"能不能做"（任务能力）到"做得好不好"（生成质量），再到"能不能用"（产品级质量），最后到"值不值得"（业务价值），构成了对智能体综合能力的完整刻画。在实际评估中，不需要每次都覆盖所有指标——应根据评估目的选择最关键的指标子集。
## 主流评估 Benchmark 详解

Benchmark 是衡量 AI Agent 能力的标尺。选择合适的 Benchmark，不仅决定了评估结果的可信度，也直接影响后续的优化方向。本章将系统介绍当前主流的 Agent 评估 Benchmark，涵盖函数调用、通用智能体能力、多轮对话、幻觉检测等多个维度，并给出实用的选型决策框架。

### BFCL — 函数调用能力评估

**全称**：Berkeley Function Calling Leaderboard

**来源**：UC Berkeley，Gorilla 项目团队。代码托管于 GitHub 仓库 [ShishirPatil/gorilla](https://github.com/ShishirPatil/gorilla) 的 `berkeley-function-call-leaderboard` 子目录下。在线排行榜地址：https://gorilla.cs.berkeley.edu/leaderboard

#### 核心定位

BFCL 是目前业界最具影响力的函数调用（Function Calling / Tool Calling）专项评估平台。其核心目标是：**精确衡量 LLM 在给定工具函数定义的前提下，能否准确选择函数、正确填充参数并生成合规的调用格式**。与通用对话评估不同，BFCL 聚焦于"格式正确性"这一可被严格验证的维度，填补了工具使用领域缺乏标准化评估的空白。

#### 评估原理：AST 树匹配

BFCL 的核心评估方法基于 **抽象语法树（AST）匹配**，而非简单的字符串比较。具体而言，评估器会将模型输出的函数调用和标准答案分别解析为 AST，然后在三个层面进行比对：

1. **函数名完全一致**：模型选择的函数名称必须与 ground truth 精确匹配
2. **参数集合相等（忽略顺序）**：传入的参数 key 集合必须一致，不关心参数的排列顺序
3. **参数值语义等价**：参数值在语义层面等价即视为正确（例如 `"true"` 与 `true`、`"2024-01-01"` 与 `2024-01-01` 等形式差异不影响判定）

这种设计有效避免了因序列化格式差异导致的误判，使评估结果更加稳健。

#### 数据集构成

BFCL 各版本累计约 **5,500+** 个问题-函数-答案对，其中 V1 版本约 2,000 对。V1 按评估方式分为 AST 评估与可执行（Executable）评估两组，具体构成如下：

| 类别 | 评估方式 | 样本数 | 说明 |
|------|----------|--------|------|
| Simple Function | AST | 550 | Python 400 + Java 100 + JavaScript 50 |
| Multiple Function | AST | 200 | 从多个候选函数中选择正确的一个 |
| Parallel Function | AST | 200 | 同一输入并行调用多个函数 |
| Parallel Multiple Function | AST | 200 | 并行调用多个"多选"函数 |
| Simple Function | Executable | 170 | Python 100 + REST 70，实际执行验证 |
| Multiple Function | Executable | 50 | 可执行的多函数选择 |
| Parallel Function | Executable | 50 | 可执行的并行调用 |
| Parallel Multiple Function | Executable | 40 | 可执行的并行多函数 |
| Irrelevance | — | 240 | 函数均不适用，检测模型能否正确拒绝调用 |

> V1 leaderboard 计入约 1700 个样本（另有 SQL 100 用于 AST 评估但不计入 leaderboard）。V3（2024-09）新增 multi-turn 共 1000 样本（Base / Missing Parameters / Missing Functions / Long-Context / Composite 各 200）；V4（2025-07，ICML 2025）新增 Agentic 端到端评估（Web Search 等）。

其中 `irrelevance` 类别尤为重要——它考察模型是否能在"无合适工具可用"时选择不调用，而非强行编造一个看似合理的调用。这一能力在实际应用中至关重要，能有效减少幻觉式工具调用。

#### 工程架构

BFCL 采用了清晰的三层架构设计，便于扩展和维护：

```
┌─────────────────────────────────────────┐
│            eval_runner (顶层)            │  ← 编排调度
├─────────────────────────────────────────┤
│          model_handler (中间层)          │  ← 模型适配
├─────────────────────────────────────────┤
│           eval_checker (底层)            │  ← 结果校验
└─────────────────────────────────────────┘
```

- **model_handler**：负责将不同模型的 API 接口统一为标准输入输出格式。每个模型对应一个 Handler 类，处理 prompt 构造、API 调用、响应解析等逻辑。
- **eval_checker**：负责对模型输出进行校验。核心是 `ast_eval` 模块，执行上述 AST 匹配逻辑。
- **eval_runner**：负责整体编排，包括数据加载、多模型并发调度、结果聚合和报告生成。

官方提供了 CLI 工具 `bfcl-eval`，支持一键运行评估：

```bash
# 安装
pip install bfcl-eval

# 评估单个模型
bfcl-eval --model gpt-4o

# 评估所有模型
bfcl-eval --model ALL
```

#### Handler 机制：低门槛扩展

BFCL 的 Handler 机制是其工程设计的亮点。添加一个新模型的评估只需三步：

1. 在 `bfcl/model_handler/` 下新建一个 Handler 类，继承基类并实现 `generate_response()` 方法
2. 在 `handler_map.py` 中注册新 Handler
3. （可选）在 `model_metadata.py` 中补充模型元信息（如上下文长度、定价等）

这种设计使得社区能够快速集成新模型。截至目前，BFCL 排行榜已覆盖数十个主流模型，包括 GPT 系列、Claude 系列、Gemini 系列、Llama 系列等。

#### 与 GAIA 的区别

BFCL 和 GAIA 虽然都涉及工具调用，但评估重心截然不同：

| 维度 | BFCL | GAIA |
|------|------|------|
| 评估对象 | 函数调用格式准确性 | 端到端任务完成能力 |
| 工具定义 | 明确提供函数签名 | 需自行选择和组合工具 |
| 评估方法 | AST 精确匹配 | 准精确匹配（Quasi-Exact Match） |
| 复杂度 | 单步或多步函数调用 | 多步推理 + 工具链组合 |
| 环境依赖 | 无需外部服务 | 需要网页浏览、文件操作等 |

简而言之，BFCL 回答的是"模型能否正确地调用函数"，而 GAIA 回答的是"模型能否用工具完成真实任务"。

---

### GAIA — 通用智能体能力评估

**全称**：General AI Assistants

**来源**：Meta AI 与 Hugging Face 联合发布。数据集托管于 Hugging Face：[gaia-benchmark/GAIA](https://huggingface.co/datasets/gaia-benchmark/GAIA)。论文发表于 ICLR 2024。

#### 核心定位

GAIA 被设计为"通用 AI 助手的标杆测试"。其核心理念是：**真正智能的 Agent 应该能完成人类在现实世界中遇到的多步骤、多工具组合任务**。与偏重单一能力的专项 Benchmark 不同，GAIA 强调端到端的任务完成，涵盖推理、工具使用、多模态理解、信息检索等多维能力的综合体现。

#### 数据集构成

GAIA 包含 **466 个问题**，分为三个难度级别：

| 级别 | 难度描述 | 工具需求 | 步骤数 |
|------|----------|----------|--------|
| Level 1 | 基础任务 | 不需要工具或最多 1 个 | ≤ 5 步 |
| Level 2 | 中级任务 | 多工具结合 | 5–10 步 |
| Level 3 | 高级任务 | 任意数量工具 | 任意长度步骤序列 |

每个问题都经过人工设计，确保：
- **答案唯一且可验证**：避免开放性主观评判
- **需要真实工具使用**：如网页搜索、文件读取、代码执行等
- **多步推理链**：无法通过单次搜索直接得到答案

典型 Level 3 示例：给定一个复杂的研究问题（如"在 NASA 1971 年某次任务的官方档案中，找出所有参与任务的宇航员的生日，并计算他们的平均年龄"），Agent 需要自主完成搜索 → 定位档案 → 提取信息 → 计算推理的完整链条。

#### 评估方法：Quasi-Exact Match

GAIA 采用 **准精确匹配（Quasi-Exact Match）** 作为评分标准，具体包含以下归一化处理：

- **数字归一化**：`1,000` 与 `1000`、`1.0K` 与 `1000` 视为等价
- **字符串归一化**：忽略大小写差异、去除首尾空格、统一标点
- **列表归一化**：列表元素顺序不影响匹配（`[A, B]` 等价于 `[B, A]`）

这种设计在保证评估客观性的同时，容忍了合理的表达形式差异，避免因格式细节误判任务完成情况。

#### 实际使用要点

- 数据集在 Hugging Face 上以 **gated dataset** 形式发布，使用前需申请访问权限
- 评估需要搭建可运行工具的 Agent 框架（如基于 LangChain、AutoGen 等），GAIA 本身只提供问题与答案
- 验证集（validation set）的答案公开，测试集（test set）的答案不公开，需提交到官方服务器评估

---

### AgentBench — 多环境综合评估

**来源**：清华大学 KEG 实验室与智谱 AI 联合开发。发表于 ICLR 2024。GitHub 仓库：[THUDM/AgentBench](https://github.com/THUDM/AgentBench)。论文：https://arxiv.org/abs/2308.03688

#### 核心定位

AgentBench 是首个系统评估"LLM as Agent"在不同环境中综合表现的 Benchmark。与 BFCL 专注函数调用、GAIA 专注端到端任务不同，AgentBench 的设计理念是：**在多个真实和模拟环境中测试 Agent 的原子能力，从而得到能力画像**。

#### 八大测试环境

AgentBench 构建了八个测试环境，其中五个为原创设计，三个基于已有数据集重新编译：

**原创环境（5 个）：**

| 环境 | 代号 | 评估能力 |
|------|------|----------|
| 操作系统 | OS | LLM 在 Linux Bash 环境中的操作能力（文件操作、进程管理等） |
| 数据库 | DB | 利用 SQL 完成数据库查询、修改等任务 |
| 知识图谱 | KG | 通过查询知识图谱工具完成复杂知识获取与推理 |
| 卡牌游戏 | DCG | 在数字卡牌游戏中进行策略决策，评估博弈推理能力 |
| 横向思维难题 | LTP | 通过问答推理还原真相，检测横向思维能力 |

**重编译环境（3 个）：**

| 环境 | 代号 | 来源 |
|------|------|------|
| 家庭环境 | HH | 基于 ALFWorld 模拟家居场景，完成日常任务 |
| 网络购物 | WS | 基于 WebShop 模拟购物网站，自主浏览和购买商品 |
| 网页浏览 | WB | 基于 Mind2Web 在真实网页环境中完成操作序列 |

这八个环境覆盖了从代码执行到知识推理、从策略博弈到 Web 交互的广泛能力维度，形成对 Agent 综合能力的立体评估。

#### 评估指标

**核心指标**：

- **Overall Score（综合得分）**：八个环境得分的加权平均，反映模型作为 Agent 的整体能力

**辅助指标**：

- **分场景成功率（Success Rate）**：各环境独立计算完成率，揭示模型的能力短板
- **失败模式分布（Failure Mode Analysis）**：统计 10 类失败原因（如错误工具调用、逻辑推理错误等），指导针对性优化
- **效率指标（Steps & Time Cost）**：评估任务完成所需交互轮次与耗时，衡量 Agent 的决策效率

#### 部署要求

AgentBench 的部署相对复杂，主要因为：

- 依赖 **Docker 环境**运行各测试场景的模拟服务
- 八个环境需分别启动对应的服务端
- 部分环境（如 OS、DB）需要 Linux 系统支持
- 资源消耗较大，建议在服务器级机器上运行

官方提供了详细的安装指南，基本流程为：环境配置 → 配置 Agent → 启动任务服务器 → 启动任务测试。GitHub 仓库中也提供了 AgentBench-Lite 套件以降低部署门槛。

#### 与 GAIA 的互补关系

AgentBench 聚焦于"LLM as Agent"的原子能力评估——即 Agent 在特定类型任务上的基础表现。而 GAIA 聚焦于端到端任务完成的综合能力。两者形成互补：AgentBench 告诉你"模型在哪些能力维度上有短板"，GAIA 告诉你"模型能否将这些能力组合起来完成真实任务"。

---

### MT-Bench — 多轮对话评估

**来源**：LMSYS（UC Berkeley 旗下研究组织，同时也是 Chatbot Arena 的运营方）。代码托管于 GitHub 仓库 [lm-sys/FastChat](https://github.com/lm-sys/FastChat)。

#### 核心定位

MT-Bench 专注于评估 LLM 在 **多轮对话** 场景下的表现。单轮对话评估容易陷入"刷榜"困境（模型可以针对单轮问答做过拟合），而多轮对话更能反映真实使用场景中模型的连贯性、上下文保持能力和指令遵循能力。

#### 数据集构成

MT-Bench 包含 **80 个多轮问题**，覆盖八大类别：

| 类别 | 说明 |
|------|------|
| Writing | 写作能力 |
| Roleplay | 角色扮演 |
| Reasoning | 逻辑推理 |
| Math | 数学计算 |
| Coding | 代码编写 |
| Extraction | 信息提取 |
| STEM | 科学技术 |
| Humanities | 人文社科 |

每个问题包含两轮对话：第一轮提出主问题，第二轮基于第一轮的回答进行追问。这种设计能有效检测模型是否真正理解上下文，而非仅对单次输入进行模式匹配。

#### 评估方法

MT-Bench 采用 **Pairwise 比较 + GPT-4 裁判** 的评估范式：

1. **Pairwise 比较**：将待评估模型的回答与参考模型（通常是 GPT-4）的回答同时呈现给裁判
2. **GPT-4 裁判**：由 GPT-4 判断哪个回答更好，输出胜率（Win Rate）
3. **单项评分**：除 Pairwise 外，也支持 GPT-4 对每个回答进行 1–10 分的绝对评分

GPT-4 裁判法的优势在于自动化程度高、成本低，但也存在已知局限——如裁判偏好长回答、偏好自身输出等偏差。研究者使用时应关注这些偏差是否影响结论。

#### 输出指标

- **Win Rate**：在 Pairwise 比较中胜出的比例
- **单项分数**：GPT-4 给出的 1–10 分绝对评分，可按类别细分

---

### 幻觉检测专项 Benchmark

幻觉（Hallucination）是 LLM 应用落地的核心风险之一。以下三个 Benchmark 从不同角度对模型的事实准确性进行专项检测。

#### TruthfulQA

**论文**：ACL 2023，"TruthfulQA: Measuring How Models Imitate Human Falsehoods"
**GitHub**：[sylinrl/TruthfulQA](https://github.com/sylinrl/TruthfulQA)

**核心目标**：检测模型是否会模仿人类的错误信念和常见误解。与一般"事实正确性"测试不同，TruthfulQA 的设计思路是——人类社会中广泛流传的许多"常识"实际上是错误的（如"下水道的水会直接流回饮用水源"），模型如果学习了人类语料，可能会"模仿"这些错误信念而非给出科学事实。

**数据集构成**：

- **817 个问题**，覆盖 **38 个类别**
- 每个问题针对一个常见的人类误解或错误信念
- 问题设计经过对抗性筛选，确保即使是训练数据中见过的问题，模型也容易给出错误答案

**评估任务**：

| 任务 | 说明 |
|------|------|
| Generation | 模型自由生成回答，评估是否包含错误陈述 |
| Multiple Choice (MC1) | 从 4 个选项中选出唯一正确答案 |
| Multiple Choice (MC2) | 从多个正确答案中选出所有正确的，评估更细粒度 |

MC1 和 MC2 的区别在于难度：MC1 是标准多选一，MC2 要求模型识别所有正确答案，对理解深度要求更高。

#### HaluEval

**论文**：EMNLP 2023
**GitHub**：[RUCAIBox/HaluEval](https://github.com/RUCAIBox/HaluEval)

**核心目标**：大规模评估 LLM 在多个领域生成幻觉的倾向。与 TruthfulQA 聚焦"人类错误信念"不同，HaluEval 更广泛地覆盖了事实性幻觉和一致性幻觉。

**数据集构成**：

- 总计约 **35,000 个样本**：5,000 个通用样本 + 30,000 个任务特定样本
- 覆盖 **四个领域**：

| 领域 | 说明 |
|------|------|
| 问答（QA） | 模型回答事实性问题时是否产生幻觉 |
| 对话（Dialogue） | 多轮对话中是否编造不存在的信息 |
| 文本摘要（Summarization） | 摘要内容是否忠实于原文 |
| 知识驱动对话（Knowledge-Driven Dialogue） | 基于知识库的对话是否引入了库中不存在的事实 |

HaluEval 支持两种评估模式：**生成模式**（让模型自由回答，检测幻觉率）和**判别模式**（给模型一段文本，判断其中是否包含幻觉）。

#### FActScore

**论文**：EMNLP 2023，"FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation"
**GitHub**：[shmsw25/FActScore](https://github.com/shmsw25/FActScore)

**核心目标**：对长文本生成进行细粒度的事实性评估。传统评估方法只能给出"整体正确/错误"的粗粒度判断，而 FActScore 将长文本拆解为一个个原子事实，逐一验证，从而精确定位模型在哪句话上出了问题。

**评估流程**：

1. **原子事实分解**：将模型生成的长文本拆解为多条原子事实
   - 例：*"张三是计算机科学家，2010年毕业于MIT，2020年获得图灵奖。"*
   - 拆解为：① 张三是计算机科学家 ② 2010年毕业于MIT ③ 2020年获得图灵奖

2. **逐个验证**：对每条原子事实，检索可靠知识源（如 Wikipedia）判断其是否被支持
   - ① ✅ 被支持 ② ✅ 被支持 ③ ❌ 未被支持（实际为2021年获奖）

3. **计算得分**：

   $$\text{FActScore} = \frac{\text{被支持的原子事实数}}{\text{总原子事实数}}$$

   上述示例得分 = 2/3 ≈ 67%

**关键发现**：

论文报告了多个模型的 FActScore 得分。其中，**ChatGPT 的 FActScore 约为 58%**，意味着 ChatGPT 生成的长文本中，平均只有约 58% 的原子事实能够被可靠来源验证。这一数字揭示了当前 LLM 在事实性方面的显著局限。

此外，论文还发现了"尾部效应"：模型在生成冷门人物的信息时，准确率大幅下降——ChatGPT 在热门人物上的准确率可达约 80%，而在冷门人物上可能降至约 16%。

**自动化评估**：由于人工标注成本高昂，论文还提出了基于检索 + 强语言模型的自动化 FActScore 估计方法，报告与人工标注的误差率低于 2%。

---

### 其他专业 Benchmark

#### ToolBench / ToolEval

**来源**：OpenBMB（清华大学与面壁智能联合开源团队）。发表于 ICLR 2024（Spotlight）。GitHub 仓库：[OpenBMB/ToolBench](https://github.com/OpenBMB/ToolBench)。

**定位**：ToolBench 是面向真实世界 API 的工具使用能力训练与评估平台。与 BFCL 使用人工设计的函数签名不同，ToolBench 从 RapidAPI 平台自动收集了超过 16,000 个真实 API，构建了覆盖单工具和多工具场景的评估集。其配套的评估体系 ToolEval 从两个维度打分：**Pass Rate**（任务完成率）和 **Win Rate**（与参考方案比较的胜率，由 GPT-4 裁判）。

#### MMLU

**全称**：Massive Multitask Language Understanding

**定位**：基础能力"全科体检"。MMLU 包含 **57 个学科**的多选题，覆盖 STEM、人文、社科、医学等领域，是评估 LLM 基础知识储备最常用的 Benchmark。虽然不是 Agent 专项评估，但模型在 MMLU 上的表现与其在 Agent 任务中的基础推理能力高度相关——一个知识储备不足的模型很难在复杂 Agent 任务中做出正确决策。

#### C-Eval

**定位**：中文版 MMLU。C-Eval 包含 **52 个学科**的多选题，针对中文语境设计，覆盖中国的学科体系和知识背景。对于面向中文用户的 Agent 评估，C-Eval 是不可或缺的基础能力测试。

---

### Benchmark 选型决策树

#### 决策流程

选择 Benchmark 不应贪多求全，而应根据评估目标精准匹配。以下是一个文字描述的决策流程：

```
第一步：确定评估目标
│
├── 目标是"函数调用格式准确性"？
│   └── → BFCL
│
├── 目标是"端到端真实任务完成"？
│   └── → GAIA
│
├── 目标是"多环境综合能力画像"？
│   └── → AgentBench
│
├── 目标是"多轮对话连贯性"？
│   └── → MT-Bench
│
├── 目标是"事实准确性 / 幻觉检测"？
│   │
│   ├── 检测"模仿人类错误信念"？
│   │   └── → TruthfulQA
│   │
│   ├── 检测"多领域幻觉率"？
│   │   └── → HaluEval
│   │
│   └── 检测"长文本细粒度事实性"？
│       └── → FActScore
│
├── 目标是"真实 API 工具使用"？
│   └── → ToolBench / ToolEval
│
└── 目标是"基础知识储备"？
    │
    ├── 英文场景 → MMLU
    └── 中文场景 → C-Eval
```

#### 组合推荐方案

实际项目中通常需要多个 Benchmark 组合使用，以下是三种典型场景的推荐方案：

**方案一：快速筛选（1–2 天，适合模型初筛）**

| Benchmark | 目的 | 耗时 |
|-----------|------|------|
| BFCL (simple 类别) | 验证基本函数调用能力 | 约 2 小时 |
| MMLU (子集) | 验证基础知识储备 | 约 1 小时 |
| TruthfulQA (MC1) | 快速检测幻觉倾向 | 约 1 小时 |

此组合能在一天内完成，快速过滤掉明显不达标的模型，适合在多个候选模型中进行初筛。

**方案二：综合评估（3–5 天，适合选型决策）**

| Benchmark | 目的 | 耗时 |
|-----------|------|------|
| BFCL (全类别) | 全面评估函数调用能力 | 约 4 小时 |
| GAIA (Level 1+2) | 端到端任务完成能力 | 约 1–2 天 |
| MT-Bench | 多轮对话能力 | 约 2 小时 |
| FActScore | 长文本事实性 | 约 4 小时 |
| C-Eval (如需中文) | 中文基础知识 | 约 2 小时 |

此组合覆盖工具调用、端到端任务、多轮对话、幻觉检测和基础知识五个维度，适合在 3–5 个候选模型中做出最终选型决策。

**方案三：深度评估（1–2 周，适合论文发表或产品上线前终评）**

| Benchmark | 目的 | 耗时 |
|-----------|------|------|
| BFCL (全类别 + V3 多轮) | 函数调用深度评估 | 约 8 小时 |
| GAIA (全级别) | 完整端到端评估 | 约 3–5 天 |
| AgentBench (八大环境) | 多环境能力画像 | 约 2–3 天 |
| MT-Bench + Chatbot Arena | 对话质量评估 | 约 4 小时 |
| TruthfulQA + HaluEval + FActScore | 幻觉检测三件套 | 约 1 天 |
| ToolBench | 真实 API 场景验证 | 约 1 天 |

此组合为全面深度评估，适合论文实验部分或产品上线前的最终质量把关。预计需要 1–2 周完成全部评估，需配备 GPU 服务器和 Docker 环境。

#### 选型注意事项

1. **数据污染风险**：部分 Benchmark（如 MMLU、TruthfulQA）的数据已广泛出现在公开训练语料中。建议使用最新的动态更新版本（如 BFCL V2 引入了 live 数据集来对抗数据污染），或自行构建私有测试集。

2. **裁判偏差**：使用 GPT-4 作为裁判的方法（MT-Bench、ToolBench 等）存在已知偏差——偏好长回答、偏好自身输出。建议在关键结论上辅以人工评估。

3. **环境依赖**：AgentBench、GAIA 等需要 Docker 环境和外部服务的 Benchmark，在部署前需充分评估基础设施条件。建议先从无环境依赖的 BFCL、MMLU 等开始，逐步过渡到复杂环境。

4. **时效性**：Benchmark 排行榜会持续更新，模型排名可能随时间变化。引用排行榜分数时务必注明访问日期。

5. **中文场景**：大多数 Benchmark 以英文为主。面向中文用户的 Agent 评估应额外纳入 C-Eval，并考虑自建中文版本的函数调用和端到端任务评估集。
## 评估方法论

评估是 Agent 工程的指南针——没有可靠的度量，就没有可控的迭代。本章系统介绍从精确匹配到人工评审、从幻觉检测到延迟成本核算的完整评估方法论体系，覆盖七大类评估方法及其适用场景。

---

### Exact Match / AST Match

#### Exact Match

Exact Match（精确匹配）是最简单直接的评估方法：将模型输出与参考答案做字符串级别的完全比对，二者完全一致才算正确。

**适用场景：**

- **数学题**：如 AIME 竞赛题答案为 0-999 的整数，输出 `"42"` 与参考答案 `"42"` 直接比对
- **分类任务**：标签集有限且互斥，如情感分类输出 `"positive"` / `"negative"`
- **选择题**：输出为固定选项字母 A/B/C/D

**核心公式：**

$$
\text{Exact Match Accuracy} = \frac{\text{匹配成功的样本数}}{\text{总样本数}} \times 100\%
$$

**局限性——格式敏感：**

| 模型输出 | 参考答案 | 是否匹配 | 问题 |
|---------|---------|---------|------|
| `42` | `42` | ✅ | — |
| `42.` | `42` | ❌ | 多了句号 |
| `The answer is 42` | `42` | ❌ | 包含解释文本 |
| ` 42 ` | `42` | ❌ | 前后空格 |

**常见规避手段：** 在比对前做标准化处理——去除首尾空白、去除标点、提取数字等。但标准化规则本身也需要精心设计，否则可能引入误判。

#### AST Match

当评估对象是**函数调用**或**代码表达式**时，精确匹配过于严格——`foo(a=1, b=2)` 和 `foo(b=2, a=1)` 语义完全等价，但字符串不同。AST Match 通过将代码解析为抽象语法树（Abstract Syntax Tree）来进行语义级别的比较。

**三大匹配规则：**

1. **函数名完全一致**：`foo` ≠ `bar`
2. **参数集合相等（忽略顺序）**：`{a, b}` == `{b, a}`
3. **参数值语义等价**：`foo(x=2+3)` 与 `foo(x=5)` 应视为等价

**实现步骤：**

1. 用 `repr()` 包裹参数字符串，确保可被 `ast.parse()` 正确解析
2. 调用 `ast.parse()` 将表达式解析为 AST
3. 调用 `ast.dump()` 将 AST 序列化为字符串表示
4. 比较两棵 AST 的 dump 结果

**完整可运行代码示例：**

```python
import ast

def ast_match(predicted: str, reference: str) -> bool:
    """
    比较两个函数调用表达式是否在 AST 层面等价。
    
    规则：
      1. 函数名完全一致
      2. 参数集合相等（忽略关键字参数顺序）
      3. 参数值的 AST 结构一致
    
    Args:
        predicted: 模型输出的函数调用字符串，如 "search(query='hello', limit=10)"
        reference: 参考函数调用字符串，如 "search(limit=10, query='hello')"
    
    Returns:
        True 如果 AST 等价，否则 False
    """
    try:
        # 用 repr() 包裹，确保被当作表达式解析
        tree_pred = ast.parse(repr(predicted), mode='eval')
        tree_ref = ast.parse(repr(reference), mode='eval')
    except SyntaxError:
        return False

    # 比较 AST dump（Python 3.9+ 支持 indent 参数，这里用默认）
    dump_pred = ast.dump(tree_pred)
    dump_ref = ast.dump(tree_ref)

    return dump_pred == dump_ref


# ========== 测试用例 ==========

# 用例1：关键字参数顺序不同
assert ast_match(
    "search(query='hello', limit=10)",
    "search(limit=10, query='hello')"
) == True, "关键字参数顺序不同应匹配"

# 用例2：函数名不同
assert ast_match(
    "search(query='hello')",
    "lookup(query='hello')"
) == False, "函数名不同不应匹配"

# 用例3：参数值不同
assert ast_match(
    "search(query='hello')",
    "search(query='world')"
) == False, "参数值不同不应匹配"

# 用例4：完全一致
assert ast_match(
    "calculate(a=1, b=2)",
    "calculate(a=1, b=2)"
) == True, "完全一致应匹配"

# 用例5：参数数量不同
assert ast_match(
    "search(query='hello')",
    "search(query='hello', limit=10)"
) == False, "参数数量不同不应匹配"

print("所有测试用例通过！")
```

> **注意：** 上面的基础实现中，`foo(x=2+3)` 与 `foo(x=5)` 会被判为**不匹配**，因为 `2+3` 和 `5` 的 AST 结构不同。要实现真正的语义等价判断，需要额外的**求值步骤**：

```python
import ast

def semantically_equal(val_pred: str, val_ref: str) -> bool:
    """
    判断两个表达式是否语义等价（通过安全求值）。
    仅适用于字面量和基本算术运算。
    """
    SAFE_NODES = (
        ast.Expression, ast.BinOp, ast.UnaryOp,
        ast.Num, ast.Constant,       # 数字字面量
        ast.Str,                     # 字符串字面量（Python < 3.8）
        ast.NameConstant,            # True/False/None（Python < 3.8）
        ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.Mod, ast.Pow,
        ast.USub, ast.UAdd,
    )

    def safe_eval(expr: str):
        try:
            tree = ast.parse(expr, mode='eval')
            for node in ast.walk(tree):
                if not isinstance(node, SAFE_NODES):
                    raise ValueError(f"不支持的节点类型: {type(node).__name__}")
            return eval(compile(tree, '<string>', 'eval'))
        except Exception:
            return None

    result_pred = safe_eval(val_pred)
    result_ref = safe_eval(val_ref)

    if result_pred is None or result_ref is None:
        # 无法求值，回退到字符串比较
        return val_pred == val_ref

    return result_pred == result_ref


# 测试语义等价
assert semantically_equal("2+3", "5") == True
assert semantically_equal("2*3", "6") == True
assert semantically_equal("10/2", "5") == True
assert semantically_equal("2+3", "6") == False
assert semantically_equal("'hello'", "'hello'") == True

print("语义等价测试通过！")
```

在实际评估系统中，通常将 AST Match 与语义求值结合使用：先用 AST Match 做结构比对，结构不一致但对参数值做安全求值后相等，也判为匹配。

---

### LLM Judge（LLM-as-a-Judge）

#### 原理

用能力强的大语言模型作为"裁判"，对被评估模型的输出进行质量评判。这是当前 Agent 评估中最灵活、最广泛使用的自动化方法。

核心假设：**如果裁判模型的能力显著强于被评估模型，其评判结果具有参考价值。**

#### 两种评估模式

**Pointwise（单答案打分）：**

对单个输出独立打分，不依赖其他候选答案。

```
输入: 用户问题 + 参考答案 + 模型输出
输出: 各维度分数（1-5分）+ 评语
```

**Pairwise（成对比较）：**

给定同一问题的两个候选答案（A 和 B），裁判判断哪个更好。

```
输入: 用户问题 + 答案A + 答案B
输出: A更好 / B更好 / 平手
```

#### 评估维度设计

典型评估维度及评分标准：

| 维度 | 1分 | 3分 | 5分 | 说明 |
|------|-----|-----|-----|------|
| 正确性 | 完全错误 | 部分正确 | 完全正确 | 事实准确性、逻辑推理正确性 |
| 清晰度 | 难以理解 | 基本可读 | 结构清晰 | 表达是否清楚、有条理 |
| 相关性 | 答非所问 | 部分相关 | 完全切题 | 是否回答了用户实际问题 |
| 完整性 | 严重遗漏 | 覆盖主要点 | 全面覆盖 | 是否完整回答了问题的所有方面 |

#### Prompt 工程要点

一个高质量的 LLM Judge Prompt 应包含以下要素：

```python
JUDGE_PROMPT_TEMPLATE = """你是一个专业的答案质量评估专家。请对以下回答进行评估。

## 用户问题
{question}

## 参考答案
{reference_answer}

## 待评估答案
{candidate_answer}

## 评估维度与评分标准（每项1-5分）

1. **正确性**（Correctness）
   - 5分：事实完全正确，推理无误
   - 4分：基本正确，有轻微瑕疵
   - 3分：核心结论正确，但有部分错误
   - 2分：存在明显错误
   - 1分：完全错误

2. **清晰度**（Clarity）
   - 5分：结构清晰，语言精练
   - 3分：基本可读，组织一般
   - 1分：混乱难以理解

3. **相关性**（Relevance）
   - 5分：完全切题，无冗余信息
   - 3分：基本相关，有少量偏题
   - 1分：答非所问

4. **完整性**（Completeness）
   - 5分：全面覆盖问题所有方面
   - 3分：覆盖主要方面，有遗漏
   - 1分：严重遗漏关键内容

## 输出要求
请严格按以下 JSON 格式输出，不要输出其他内容：

```json
{{
  "correctness": <1-5的整数>,
  "clarity": <1-5的整数>,
  "relevance": <1-5的整数>,
  "completeness": <1-5的整数>,
  "overall_score": <四项平均分，保留一位小数>,
  "rationale": "<简要说明评分理由，不超过200字>"
}}
```

**注意事项**
- 严格基于参考答案判断正确性
- 不要因为答案比参考答案长就给更高分
- 不要因为答案使用了不同的表达方式就扣分，只要语义正确即可
"""
```

**关键要点总结：**

| 要点 | 说明 |
|------|------|
| 明确评分标准 | 每个分数级别有具体描述，而非模糊的"好/中/差" |
| 要求 JSON 输出 | 便于自动化解析，避免自然文本提取的歧义 |
| 提供参考答案 | 给裁判明确的判断锚点 |
| 防偏见提示 | 显式提示不要因长度或表达方式差异而偏判 |

#### 三大坑与规避策略

##### 坑1：位置偏置（Position Bias）

**现象：** 在 Pairwise 比较中，LLM Judge 倾向于偏好出现在前面的答案（或后面的答案，取决于模型）。

**示例：** 同一对答案 (A, B)，当顺序为 [A, B] 时裁判判 A 赢，当顺序为 [B, A] 时裁判仍判第一个赢——说明裁判不是在比较质量，而是在偏好位置。

**规避策略——双向评估取平均：**

```python
import asyncio
from typing import Literal

async def pairwise_judge(
    judge_fn,           # 调用 LLM 裁判的异步函数
    question: str,
    answer_a: str,
    answer_b: str,
) -> Literal["A", "B", "Tie"]:
    """
    双向 Pairwise 评估，消除位置偏置。
    
    评估两次：
      1. 顺序 [A, B]
      2. 顺序 [B, A]
    
    只有两次结果一致或都判平手时才采纳，
    否则记为 Tie。
    """
    # 正向评估：A 在前
    result_ab = await judge_fn(question, answer_a, answer_b)
    
    # 反向评估：B 在前
    result_ba = await judge_fn(question, answer_b, answer_a)
    
    # 结果映射：反向评估中，"A" 和 "B" 的含义翻转
    # result_ab: "A"表示第一个(A)赢, "B"表示第二个(B)赢
    # result_ba: "A"表示第一个(B)赢, "B"表示第二个(A)赢
    mapped_ba = {"A": "B", "B": "A", "Tie": "Tie"}.get(result_ba, "Tie")
    
    if result_ab == mapped_ba:
        return result_ab
    else:
        return "Tie"
```

##### 坑2：长度偏置（Length Bias）

**现象：** LLM Judge 倾向于给更长的答案更高分数，即使长答案包含冗余信息。

**规避策略——Length-controlled Win Rate：**

在统计 Win Rate 时，将答案长度作为控制变量。具体做法是引入长度归一化因子，或按长度分桶计算胜率，使不同长度区间的答案具有可比性。详见 5.3 节。

##### 坑3：自我偏爱（Self-preference）

**现象：** LLM Judge 倾向于偏好与自己同家族的模型输出。例如用 GPT-4 做裁判时，可能系统性地给 GPT 系列模型的输出更高分。

**规避策略——跨家族裁判：**

| 被评估模型 | 推荐裁判模型 | 不推荐 |
|-----------|------------|--------|
| GPT 系列 | Claude / Gemini | GPT-4 |
| Claude 系列 | GPT-4 / Gemini | Claude |
| 开源模型 | GPT-4 + Claude 双裁判 | 同家族开源模型 |

最佳实践：使用**两个不同家族的裁判模型**分别评估，取平均分或一致判断作为最终结果。

---

### Win Rate / Pairwise Comparison

#### 核心概念

Win Rate（胜率）是 Pairwise Comparison 的统计指标，衡量一个模型在成对比较中击败基准模型的频率。

**核心公式：**

$$
\text{Win Rate} = \frac{\text{Wins}}{\text{Total Comparisons}} \times 100\%
$$

满足约束：

$$
\text{Win Rate} + \text{Loss Rate} + \text{Tie Rate} = 100\%
$$

其中：
- **Wins**：被评估模型击败基准模型的次数
- **Total Comparisons**：总比较次数（包括赢、输、平手）

#### 基准模型选择标准

基准模型的选择直接影响 Win Rate 的解读：

| 标准 | 说明 | 示例 |
|------|------|------|
| 能力适中 | 不应过强（全部输）或过弱（全部赢） | 评估 7B 模型时用 13B 而非 70B |
| 广为认可 | 社区有共识，结果可对比 | GPT-3.5-turbo、Llama-2-70B |
| 可复现 | 权重开放或 API 稳定 | 开源模型或稳定 API 版本 |

#### Length-controlled Win Rate

AlpacaEval 2.0 引入的改进方法，通过逻辑回归控制答案长度对胜率的影响：

**模型：**

$$
P(\text{Win}) = \sigma(\beta_0 + \beta_1 \cdot \Delta\text{Quality} + \beta_2 \cdot \Delta\text{Length})
$$

其中：
- $\Delta\text{Quality}$ = 被评估模型质量 − 基准模型质量（待估计）
- $\Delta\text{Length}$ = 被评估模型答案长度 − 基准模型答案长度（观测值）
- $\sigma$ = sigmoid 函数

**Length-controlled Win Rate 的含义：** 在控制答案长度差异后，被评估模型相对于基准模型的胜率。即：如果两个模型产出相同长度的答案，被评估模型的预期胜率。

**代码示例——Win Rate 计算：**

```python
from dataclasses import dataclass
from typing import Literal

@dataclass
class ComparisonResult:
    """单次成对比较结果"""
    winner: Literal["model_a", "model_b", "tie"]
    length_a: int   # 模型A答案的token长度
    length_b: int   # 模型B答案的token长度


def compute_win_rate(
    results: list[ComparisonResult],
    target: Literal["model_a", "model_b"],
) -> dict:
    """
    计算目标模型的 Win Rate / Loss Rate / Tie Rate。
    
    Args:
        results: 成对比较结果列表
        target: 要计算哪个模型的胜率
    
    Returns:
        包含 win_rate, loss_rate, tie_rate 的字典
    """
    total = len(results)
    if total == 0:
        return {"win_rate": 0.0, "loss_rate": 0.0, "tie_rate": 0.0}
    
    opponent = "model_b" if target == "model_a" else "model_a"
    
    wins = sum(1 for r in results if r.winner == target)
    losses = sum(1 for r in results if r.winner == opponent)
    ties = sum(1 for r in results if r.winner == "tie")
    
    return {
        "win_rate": wins / total * 100,
        "loss_rate": losses / total * 100,
        "tie_rate": ties / total * 100,
        "total": total,
    }


# ========== 测试 ==========
results = [
    ComparisonResult("model_a", 150, 200),
    ComparisonResult("model_a", 180, 160),
    ComparisonResult("model_b", 120, 190),
    ComparisonResult("tie",    170, 170),
    ComparisonResult("model_a", 200, 150),
]

stats = compute_win_rate(results, target="model_a")
print(f"Win Rate: {stats['win_rate']:.1f}%")    # 60.0%
print(f"Loss Rate: {stats['loss_rate']:.1f}%")   # 20.0%
print(f"Tie Rate: {stats['tie_rate']:.1f}%")     # 20.0%
assert abs(stats["win_rate"] + stats["loss_rate"] + stats["tie_rate"] - 100.0) < 0.01
print("Win Rate 计算验证通过！")
```

---

### 人工评估（Human Evaluation）

#### 为什么仍需要人工评估

自动化方法（Exact Match、LLM Judge）在以下场景中不可靠或无法覆盖：

| 场景 | 自动化方法的局限 | 人工评估的优势 |
|------|----------------|---------------|
| 数学推导正确性 | 中间步骤错误可能被忽略 | 可逐步检查推理过程 |
| 代码安全性 | 难以检测隐蔽的安全漏洞 | 人工审计可识别注入、越权等风险 |
| 创意性/主观性任务 | LLM Judge 缺乏"品味"判断 | 人类对创意有直觉判断力 |
| 多轮对话连贯性 | 难以评估上下文一致性 | 人类天然理解对话流 |
| 伦理与安全边界 | 规则覆盖不全 | 人类可识别微妙的伦理问题 |

#### 评估成本

人工评估是全章中成本最高的方法：

| 任务复杂度 | 每小时可评估样本数 | 说明 |
|-----------|------------------|------|
| 简单（分类、短答） | 20-40 | 快速判断对错 |
| 中等（段落生成、代码片段） | 10-20 | 需阅读理解 |
| 复杂（多步推理、长文、代码审计） | 5-10 | 需深入分析 |

#### 标注质量控制

**（1）标注指南（Annotation Guidelines）**

一份好的标注指南应包含：
- 明确的评估维度定义与评分标准
- 大量边界案例及标注示范
- 常见错误与混淆点说明
- 标注流程与工具使用说明

**（2）标注者一致性度量**

使用 Cohen's Kappa（两人）或 Fleiss' Kappa（多人）衡量标注者间一致性：

**Cohen's Kappa 公式：**

$$
\kappa = \frac{p_o - p_e}{1 - p_e}
$$

其中：
- $p_o$ = 观测到的一致率（observed agreement）
- $p_e$ = 随机情况下的期望一致率（expected agreement）

**一致性解读参考：**

| κ 值 | 一致性强度 |
|------|-----------|
| < 0.20 | 很差 |
| 0.21 - 0.40 | 一般 |
| 0.41 - 0.60 | 中等 |
| 0.61 - 0.80 | 良好 |
| 0.81 - 1.00 | 优秀 |

**Cohen's Kappa 计算代码：**

```python
from collections import Counter

def cohen_kappa(annotator1: list, annotator2: list) -> float:
    """
    计算两位标注者的 Cohen's Kappa。
    
    Args:
        annotator1: 标注者1的标签列表
        annotator2: 标注者2的标签列表（与annotator1等长）
    
    Returns:
        kappa 值
    """
    assert len(annotator1) == len(annotator2), "标注列表长度必须一致"
    n = len(annotator1)
    if n == 0:
        return 0.0
    
    # 观测一致率
    agreements = sum(1 for a, b in zip(annotator1, annotator2) if a == b)
    p_o = agreements / n
    
    # 期望一致率
    labels = set(annotator1) | set(annotator2)
    counter1 = Counter(annotator1)
    counter2 = Counter(annotator2)
    p_e = sum((counter1[label] / n) * (counter2[label] / n) for label in labels)
    
    if p_e == 1.0:
        return 1.0  # 完全一致
    
    kappa = (p_o - p_e) / (1 - p_e)
    return kappa


# ========== 测试 ==========
# 两位标注者对10个样本的标注
a1 = ["good", "good", "bad", "good", "bad", "good", "bad", "good", "bad", "good"]
a2 = ["good", "good", "bad", "bad", "bad", "good", "good", "good", "bad", "good"]

kappa = cohen_kappa(a1, a2)
print(f"Cohen's Kappa = {kappa:.3f}")

# 解读
if kappa >= 0.81:
    print("一致性：优秀")
elif kappa >= 0.61:
    print("一致性：良好")
elif kappa >= 0.41:
    print("一致性：中等")
elif kappa >= 0.21:
    print("一致性：一般")
else:
    print("一致性：很差")
```

**（3）黄金标准集（Gold Standard Set）**

由领域专家标注的小规模高质量数据集，用于：
- 培训新标注者
- 验证标注者质量（与黄金标准对比）
- 校准 LLM Judge 的准确性

#### 人工 + 自动混合评估

纯人工评估成本太高，纯自动评估覆盖不全。推荐**分层评估**策略：

```
全部样本
  │
  ├─→ LLM Judge 快速评估（100%样本）
  │     │
  │     ├─→ 高置信度判断 → 直接采纳
  │     │
  │     └─→ 低置信度/边界案例 → 人工复审
  │
  └─→ 人工评估（抽样5-10% + 全部边界案例）
```

**置信度判断方法：** LLM Judge 输出中包含置信度字段，或通过两次独立评估结果是否一致来判断——不一致即低置信度。

---

### 幻觉检测方法详解

幻觉（Hallucination）是 Agent 输出中看似合理但实际不正确或无依据的内容。以下是四种主流检测方法。

#### 检索增强交叉验证

**原理：** 将 Agent 输出中的事实声明与外部知识库进行检索比对，验证每个声明是否有依据支持。

**流程：**

```
Agent输出 → 提取事实声明 → 逐条检索知识库 → 比对验证 → 标记有/无依据
```

**代码示例：**

```python
from dataclasses import dataclass

@dataclass
class FactClaim:
    """一条事实声明"""
    text: str           # 声明文本
    source_span: str    # 在原文中的位置（简化为片段）
    is_supported: bool  # 是否有知识库依据
    evidence: str       # 检索到的支撑证据（如果有）


def retrieve_and_verify(
    agent_output: str,
    knowledge_base: list[str],
    extract_fn,       # 事实提取函数
    retrieve_fn,      # 检索函数
    match_fn,         # 匹配判断函数
) -> list[FactClaim]:
    """
    检索增强交叉验证流程。
    
    Args:
        agent_output: Agent 的输出文本
        knowledge_base: 知识库文档列表
        extract_fn: 从文本中提取事实声明的函数
        retrieve_fn: 从知识库中检索相关文档的函数
        match_fn: 判断声明是否被证据支持的函数
    
    Returns:
        事实声明列表，含验证结果
    """
    # 步骤1：提取事实声明
    claims = extract_fn(agent_output)
    
    # 步骤2：逐条检索并验证
    results = []
    for claim_text in claims:
        # 从知识库检索相关文档
        retrieved_docs = retrieve_fn(claim_text, knowledge_base, top_k=3)
        
        # 判断是否有支撑证据
        is_supported = False
        evidence = ""
        for doc in retrieved_docs:
            if match_fn(claim_text, doc):
                is_supported = True
                evidence = doc
                break
        
        results.append(FactClaim(
            text=claim_text,
            source_span=claim_text,
            is_supported=is_supported,
            evidence=evidence,
        ))
    
    return results


# ========== 模拟测试 ==========

# 模拟函数
def mock_extract(text: str) -> list[str]:
    """模拟事实提取（实际中用LLM实现）"""
    return [
        "Python由Guido van Rossum创建",
        "Python首次发布于1991年",
        "Python的名字来源于一种蛇",
    ]

def mock_retrieve(query: str, kb: list[str], top_k: int = 3) -> list[str]:
    """模拟检索（实际中用向量检索）"""
    return [doc for doc in kb if any(w in doc for w in query.split())][:top_k]

def mock_match(claim: str, evidence: str) -> bool:
    """模拟匹配判断"""
    return any(w in evidence for w in claim.split() if len(w) > 2)

# 知识库
kb = [
    "Python是由Guido van Rossum在1980年代末开始开发的编程语言",
    "Python的第一个公开版本发布于1991年2月",
    "Python这个名字来源于Monty Python飞行马戏团，而非蛇",
]

# 运行验证
results = retrieve_and_verify(
    agent_output="Python由Guido van Rossum创建，首次发布于1991年，名字来源于一种蛇。",
    knowledge_base=kb,
    extract_fn=mock_extract,
    retrieve_fn=mock_retrieve,
    match_fn=mock_match,
)

for r in results:
    status = "✅ 有依据" if r.is_supported else "❌ 无依据"
    print(f"{status} | {r.text}")
    if r.evidence:
        print(f"        证据: {r.evidence}")

# 预期：前两条有依据，第三条（来源于蛇）无依据
```

#### 事实原子分解（FActScore 方法）

**原理：** 将一段长文本分解为原子事实（atomic facts），逐条判断每个原子事实是否被可靠知识源支持。

**FActScore 公式：**

$$
\text{FActScore} = \frac{\text{被支持的原子事实数}}{\text{总原子事实数}}
$$

**原子分解示例：**

```
原始文本: "Albert Einstein was born in Germany in 1879. 
He developed the theory of relativity and received the Nobel Prize in 1921."

原子分解:
  1. "Albert Einstein was born in Germany"        → ✅ 支持
  2. "Albert Einstein was born in 1879"           → ✅ 支持
  3. "Albert Einstein developed the theory of relativity" → ✅ 支持
  4. "Albert Einstein received the Nobel Prize"   → ✅ 支持
  5. "Albert Einstein received the Nobel Prize in 1921"   → ✅ 支持

FActScore = 5/5 = 1.0
```

**代码示例：**

```python
from dataclasses import dataclass

@dataclass
class AtomicFact:
    text: str
    is_supported: bool


def compute_factscore(facts: list[AtomicFact]) -> dict:
    """
    计算 FActScore 及相关指标。
    
    Args:
        facts: 原子事实列表，含是否被支持标记
    
    Returns:
        包含 factscore, supported_count, total_count 的字典
    """
    total = len(facts)
    if total == 0:
        return {"factscore": 0.0, "supported_count": 0, "total_count": 0}
    
    supported = sum(1 for f in facts if f.is_supported)
    
    return {
        "factscore": supported / total,
        "supported_count": supported,
        "total_count": total,
    }


# ========== 测试 ==========

# 模拟原子事实分解与验证结果
facts = [
    AtomicFact("Einstein was born in Germany", True),
    AtomicFact("Einstein was born in 1879", True),
    AtomicFact("Einstein developed the theory of relativity", True),
    AtomicFact("Einstein received the Nobel Prize in Physics", True),
    AtomicFact("Einstein received the Nobel Prize in 1921", True),
    AtomicFact("Einstein won 3 Nobel Prizes", False),  # 幻觉
]

result = compute_factscore(facts)
print(f"FActScore = {result['factscore']:.2f} "
      f"({result['supported_count']}/{result['total_count']})")
# 输出: FActScore = 0.83 (5/6)
```

#### 自我一致性检测

**原理：** 对同一问题多次采样（temperature > 0），检查多次输出之间的一致性。如果模型"自信"其答案，多次采样应给出语义一致的结果；高不一致性暗示可能的幻觉。

**流程：**

```
同一问题 → 多次采样（N次，temp>0）→ 提取每次的关键结论 
→ 计算一致性比例 → 低一致性 → 可能幻觉
```

**代码示例：**

```python
from collections import Counter
from dataclasses import dataclass

@dataclass
class SamplingResult:
    """单次采样结果"""
    output: str
    extracted_answer: str  # 提取的关键结论


def self_consistency_check(
    samples: list[SamplingResult],
    consistency_threshold: float = 0.7,
) -> dict:
    """
    自我一致性检测。
    
    Args:
        samples: 多次采样的结果列表
        consistency_threshold: 一致性阈值，低于此值标记为可能幻觉
    
    Returns:
        包含 consistency_ratio, most_common_answer, is_hallucination_suspected 的字典
    """
    n = len(samples)
    if n == 0:
        return {"consistency_ratio": 0.0, "is_hallucination_suspected": True}
    
    # 统计每个答案出现的次数
    answer_counts = Counter(s.extracted_answer for s in samples)
    most_common_answer, most_common_count = answer_counts.most_common(1)[0]
    
    consistency_ratio = most_common_count / n
    is_hallucination = consistency_ratio < consistency_threshold
    
    return {
        "consistency_ratio": consistency_ratio,
        "most_common_answer": most_common_answer,
        "most_common_count": most_common_count,
        "total_samples": n,
        "is_hallucination_suspected": is_hallucination,
        "answer_distribution": dict(answer_counts),
    }


# ========== 测试 ==========

# 场景1：高一致性（自信，可能正确）
samples_high_consistency = [
    SamplingResult("The capital of France is Paris.", "Paris"),
    SamplingResult("Paris is the capital of France.", "Paris"),
    SamplingResult("France's capital city is Paris.", "Paris"),
    SamplingResult("The capital of France is Paris.", "Paris"),
    SamplingResult("Paris serves as the capital of France.", "Paris"),
]

# 场景2：低一致性（不确定，可能幻觉）
samples_low_consistency = [
    SamplingResult("The answer is 42.", "42"),
    SamplingResult("The answer is 37.", "37"),
    SamplingResult("The answer is 42.", "42"),
    SamplingResult("The answer is 39.", "39"),
    SamplingResult("The answer is 42.", "42"),
]

print("=== 场景1：高一致性 ===")
r1 = self_consistency_check(samples_high_consistency)
print(f"一致性: {r1['consistency_ratio']:.0%} | 最常见答案: {r1['most_common_answer']} | 疑似幻觉: {r1['is_hallucination_suspected']}")

print("\n=== 场景2：低一致性 ===")
r2 = self_consistency_check(samples_low_consistency)
print(f"一致性: {r2['consistency_ratio']:.0%} | 最常见答案: {r2['most_common_answer']} | 疑似幻觉: {r2['is_hallucination_suspected']}")
print(f"答案分布: {r2['answer_distribution']}")
```

#### 幻觉率计算公式与阈值设定

**幻觉率定义：**

$$
\text{Hallucination Rate} = \frac{\text{包含幻觉的样本数}}{\text{总样本数}} \times 100\%
$$

或者基于原子事实级别的更精细度量：

$$
\text{Hallucination Rate} = 1 - \text{FActScore} = \frac{\text{无依据的原子事实数}}{\text{总原子事实数}} \times 100\%
$$

**阈值设定参考：**

| 应用场景 | 建议幻觉率上限 | 说明 |
|---------|--------------|------|
| 医疗/法律咨询 | < 2% | 高风险，错误可能造成严重后果 |
| 金融/投资建议 | < 3% | 涉及财产安全 |
| 通用问答/客服 | < 5% | 一般风险，需人工兜底 |
| 创意写作/头脑风暴 | < 15% | 容忍度较高，创意本身允许虚构 |

**代码示例——幻觉率评估：**

```python
from dataclasses import dataclass

@dataclass
class HallucinationEvalResult:
    """单样本幻觉评估结果"""
    sample_id: str
    has_hallucination: bool
    atomic_facts_total: int
    atomic_facts_unsupported: int
    hallucination_rate: float  # 该样本内部的幻觉率


def aggregate_hallucination_rate(
    eval_results: list[HallucinationEvalResult],
) -> dict:
    """
    汇总多个样本的幻觉评估结果。
    
    Returns:
        sample_level_rate: 样本级幻觉率（有幻觉的样本占比）
        fact_level_rate: 事实级幻觉率（无依据原子事实占比）
        pass_threshold: 是否满足给定阈值
    """
    n = len(eval_results)
    if n == 0:
        return {"sample_level_rate": 0.0, "fact_level_rate": 0.0}
    
    # 样本级：含有至少一条幻觉的样本占比
    hallucinated_samples = sum(1 for r in eval_results if r.has_hallucination)
    sample_level_rate = hallucinated_samples / n * 100
    
    # 事实级：所有样本中无依据原子事实的占比
    total_facts = sum(r.atomic_facts_total for r in eval_results)
    unsupported_facts = sum(r.atomic_facts_unsupported for r in eval_results)
    fact_level_rate = (unsupported_facts / total_facts * 100) if total_facts > 0 else 0.0
    
    return {
        "sample_level_rate": sample_level_rate,
        "fact_level_rate": fact_level_rate,
        "total_samples": n,
        "hallucinated_samples": hallucinated_samples,
        "total_atomic_facts": total_facts,
        "unsupported_atomic_facts": unsupported_facts,
    }


def check_threshold(
    eval_results: list[HallucinationEvalResult],
    threshold: float,
    level: str = "sample",
) -> bool:
    """
    检查幻觉率是否满足阈值。
    
    Args:
        threshold: 阈值百分比，如 2.0 表示 2%
        level: "sample" 样本级 或 "fact" 事实级
    
    Returns:
        True 如果满足阈值（幻觉率 <= threshold）
    """
    agg = aggregate_hallucination_rate(eval_results)
    rate = agg["sample_level_rate"] if level == "sample" else agg["fact_level_rate"]
    passed = rate <= threshold
    print(f"幻觉率（{level}级）: {rate:.1f}% | 阈值: {threshold}% | {'✅ 通过' if passed else '❌ 不通过'}")
    return passed


# ========== 测试 ==========
eval_results = [
    HallucinationEvalResult("s1", False, 5, 0, 0.0),
    HallucinationEvalResult("s2", False, 4, 0, 0.0),
    HallucinationEvalResult("s3", True,  6, 1, 16.7),
    HallucinationEvalResult("s4", False, 3, 0, 0.0),
    HallucinationEvalResult("s5", True,  5, 2, 40.0),
    HallucinationEvalResult("s6", False, 4, 0, 0.0),
    HallucinationEvalResult("s7", False, 5, 0, 0.0),
    HallucinationEvalResult("s8", False, 3, 0, 0.0),
    HallucinationEvalResult("s9", False, 4, 0, 0.0),
    HallucinationEvalResult("s10", False, 5, 0, 0.0),
]

agg = aggregate_hallucination_rate(eval_results)
print(f"样本级幻觉率: {agg['sample_level_rate']:.1f}% ({agg['hallucinated_samples']}/{agg['total_samples']})")
print(f"事实级幻觉率: {agg['fact_level_rate']:.1f}% ({agg['unsupported_atomic_facts']}/{agg['total_atomic_facts']})")

print("\n--- 医疗场景阈值检查 (< 2%) ---")
check_threshold(eval_results, threshold=2.0)

print("\n--- 通用问答阈值检查 (< 5%) ---")
check_threshold(eval_results, threshold=5.0)
```

---

### 延迟与成本评估方法

评估 Agent 不仅看输出质量，还必须度量效率——延迟和成本决定了系统能否在生产环境中部署。

#### P50 / P95 / P99 响应时间

**定义：**

将所有请求的响应时间排序后：

| 指标 | 定义 | 含义 |
|------|------|------|
| P50（中位数） | 50% 的请求快于此值 | "典型用户体验" |
| P95 | 95% 的请求快于此值 | "大多数用户的最差体验" |
| P99 | 99% 的请求快于此值 | "长尾用户的体验" |

**为什么看 P95 / P99 而非平均值？**

平均值会被大量快速请求拉低，掩盖少数极慢请求的存在。例如：

```
100个请求的响应时间：
  95个 = 1秒
  4个  = 5秒
  1个  = 30秒

平均值 = (95×1 + 4×5 + 1×30) / 100 = 1.45秒  ← 看起来不错
P95    = 5秒    ← 有5%的用户等5秒以上
P99    = 30秒   ← 有1%的用户等30秒
```

对于生产系统，P95 和 P99 反映了真实的最差用户体验。

**代码示例——百分位计算：**

```python
import numpy as np

def compute_latency_percentiles(
    latencies_ms: list[float],
) -> dict:
    """
    计算响应时间的百分位指标。
    
    Args:
        latencies_ms: 响应时间列表（毫秒）
    
    Returns:
        包含 P50/P95/P99/min/max/mean 的字典
    """
    arr = np.array(latencies_ms)
    
    return {
        "count": len(arr),
        "min_ms": float(np.min(arr)),
        "max_ms": float(np.max(arr)),
        "mean_ms": float(np.mean(arr)),
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
    }


# ========== 测试 ==========
np.random.seed(42)
# 模拟1000个请求的延迟（对数正态分布，更接近真实场景）
latencies = np.random.lognormal(mean=6.5, sigma=0.5, size=1000).tolist()

stats = compute_latency_percentiles(latencies)
print("响应时间统计（毫秒）:")
print(f"  样本数: {stats['count']}")
print(f"  最小值: {stats['min_ms']:.0f}ms")
print(f"  最大值: {stats['max_ms']:.0f}ms")
print(f"  平均值: {stats['mean_ms']:.0f}ms")
print(f"  P50:   {stats['p50_ms']:.0f}ms")
print(f"  P95:   {stats['p95_ms']:.0f}ms")
print(f"  P99:   {stats['p99_ms']:.0f}ms")
print(f"\n  P99/P50 比值: {stats['p99_ms']/stats['p50_ms']:.1f}x")
print(f"  （比值越大，长尾问题越严重）")
```

#### First Token Latency（首Token延迟）

**定义：** 从发送请求到收到第一个输出 Token 的时间间隔，也称为 TTFT（Time To First Token）。

**为什么重要？**

在流式输出（Streaming）场景中，用户看到的是逐字出现的文本。首 Token 延迟决定了用户点击发送后多久能看到"开始响应"，这直接影响用户感知体验。

**与总延迟的关系：**

```
总响应时间 = First Token Latency + (输出Token数 / 生成速度)

用户感知延迟 ≈ First Token Latency（"它开始回答了"）
用户等待完成 ≈ 总响应时间（"它回答完了"）
```

**代码示例——流式延迟度量：**

```python
from dataclasses import dataclass

@dataclass
class StreamingMetrics:
    """流式输出延迟度量"""
    first_token_latency_ms: float    # 首 Token 延迟
    total_latency_ms: float          # 总延迟
    output_token_count: int          # 输出 Token 数
    generation_time_ms: float        # 生成阶段耗时（首Token后）
    tokens_per_second: float         # 生成速度


def measure_streaming_latency(
    first_token_time_ms: float,
    total_time_ms: float,
    token_count: int,
) -> StreamingMetrics:
    """
    计算流式输出各项延迟指标。
    
    Args:
        first_token_time_ms: 从请求到首个Token的延迟（毫秒）
        total_time_ms: 从请求到完成的总延迟（毫秒）
        token_count: 输出的Token总数
    
    Returns:
        StreamingMetrics 对象
    """
    generation_time_ms = total_time_ms - first_token_time_ms
    tps = token_count / (generation_time_ms / 1000) if generation_time_ms > 0 else 0.0
    
    return StreamingMetrics(
        first_token_latency_ms=first_token_time_ms,
        total_latency_ms=total_time_ms,
        output_token_count=token_count,
        generation_time_ms=generation_time_ms,
        tokens_per_second=tps,
    )


# ========== 测试 ==========
metrics = measure_streaming_latency(
    first_token_time_ms=350,   # 首 Token 延迟 350ms
    total_time_ms=4200,        # 总延迟 4.2s
    token_count=180,           # 输出 180 个 Token
)

print("流式输出延迟分析:")
print(f"  首 Token 延迟: {metrics.first_token_latency_ms:.0f}ms")
print(f"  生成阶段耗时: {metrics.generation_time_ms:.0f}ms")
print(f"  总延迟: {metrics.total_latency_ms:.0f}ms")
print(f"  输出 Token 数: {metrics.output_token_count}")
print(f"  生成速度: {metrics.tokens_per_second:.1f} tokens/s")
print(f"\n  首 Token 占总延迟比例: {metrics.first_token_latency_ms/metrics.total_latency_ms*100:.1f}%")
```

#### Token 消耗统计

Agent 的 Token 消耗比单轮 LLM 调用复杂得多，需要分项统计：

```
单次 Agent 任务总 Token = Σ(每步的输入Token + 输出Token)

每步输入Token构成:
  ├─ 系统提示词（System Prompt）
  ├─ 对话历史（Context Window累积）
  ├─ 工具调用结果（可能很长）
  └─ 用户当前输入

每步输出Token构成:
  ├─ 推理过程（Chain-of-Thought）
  ├─ 工具调用指令
  └─ 最终回答
```

**代码示例——多步Agent Token统计：**

```python
from dataclasses import dataclass, field

@dataclass
class StepTokenUsage:
    """单步 Token 使用量"""
    step_index: int
    input_tokens: int
    output_tokens: int
    description: str  # 该步骤的描述

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class AgentTokenReport:
    """Agent 多步任务的 Token 消耗报告"""
    steps: list[StepTokenUsage] = field(default_factory=list)
    
    def add_step(self, input_tokens: int, output_tokens: int, description: str):
        self.steps.append(StepTokenUsage(
            step_index=len(self.steps),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            description=description,
        ))
    
    @property
    def total_input_tokens(self) -> int:
        return sum(s.input_tokens for s in self.steps)
    
    @property
    def total_output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.steps)
    
    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens
    
    @property
    def step_count(self) -> int:
        return len(self.steps)
    
    def summary(self) -> str:
        lines = [
            f"Agent Token 消耗报告（共 {self.step_count} 步）",
            f"{'='*50}",
            f"{'步骤':<6} {'输入':<10} {'输出':<10} {'合计':<10} 描述",
            f"{'-'*50}",
        ]
        for s in self.steps:
            lines.append(f"  {s.step_index:<4} {s.input_tokens:<10} {s.output_tokens:<10} {s.total_tokens:<10} {s.description}")
        lines.append(f"{'-'*50}")
        lines.append(f"  合计  {self.total_input_tokens:<10} {self.total_output_tokens:<10} {self.total_tokens:<10}")
        lines.append(f"  平均/步 {self.total_input_tokens//self.step_count:<9} {self.total_output_tokens//self.step_count:<10}")
        return "\n".join(lines)


# ========== 测试 ==========
report = AgentTokenReport()
report.add_step(2500, 150,  "解析用户问题")
report.add_step(3100, 80,   "调用搜索工具")
report.add_step(5200, 200,  "阅读搜索结果，提取信息")
report.add_step(4800, 120,  "调用计算工具")
report.add_step(6500, 350,  "综合信息，生成最终回答")

print(report.summary())
```

#### 单任务成本计算模型

**成本公式：**

$$
\text{Cost}_{\text{task}} = \sum_{i=1}^{n} \left( \text{Tokens}_{\text{in}}^{(i)} \times P_{\text{in}} + \text{Tokens}_{\text{out}}^{(i)} \times P_{\text{out}} \right)
$$

其中：
- $n$ = Agent 执行步数
- $\text{Tokens}_{\text{in}}^{(i)}$ = 第 $i$ 步的输入 Token 数
- $\text{Tokens}_{\text{out}}^{(i)}$ = 第 $i$ 步的输出 Token 数
- $P_{\text{in}}$ = 每输入 Token 价格
- $P_{\text{out}}$ = 每输出 Token 价格

**代码示例：**

```python
from dataclasses import dataclass

@dataclass
class PricingConfig:
    """模型定价配置（每百万Token的美元价格）"""
    input_price_per_million: float   # 输入价格 $/M tokens
    output_price_per_million: float  # 输出价格 $/M tokens
    
    @property
    def input_price_per_token(self) -> float:
        return self.input_price_per_million / 1_000_000
    
    @property
    def output_price_per_token(self) -> float:
        return self.output_price_per_million / 1_000_000


def compute_task_cost(
    steps: list[tuple[int, int]],  # [(input_tokens, output_tokens), ...]
    pricing: PricingConfig,
) -> dict:
    """
    计算单任务总成本。
    
    Args:
        steps: 每步的 (输入Token数, 输出Token数) 列表
        pricing: 定价配置
    
    Returns:
        包含总成本、分步成本等的字典
    """
    total_cost = 0.0
    step_costs = []
    
    for i, (input_tokens, output_tokens) in enumerate(steps):
        step_cost = (
            input_tokens * pricing.input_price_per_token +
            output_tokens * pricing.output_price_per_token
        )
        step_costs.append({
            "step": i,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": step_cost,
        })
        total_cost += step_cost
    
    total_input = sum(s[0] for s in steps)
    total_output = sum(s[1] for s in steps)
    
    return {
        "total_cost_usd": total_cost,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "step_count": len(steps),
        "avg_cost_per_step": total_cost / len(steps) if steps else 0.0,
        "step_breakdown": step_costs,
    }


# ========== 测试 ==========
# 假设使用某模型，输入$3/M tokens，输出$15/M tokens
pricing = PricingConfig(
    input_price_per_million=3.0,
    output_price_per_million=15.0,
)

# Agent 5步执行的Token消耗
steps = [
    (2500, 150),
    (3100, 80),
    (5200, 200),
    (4800, 120),
    (6500, 350),
]

result = compute_task_cost(steps, pricing)
print(f"单任务成本: ${result['total_cost_usd']:.4f}")
print(f"总输入Token: {result['total_input_tokens']:,}")
print(f"总输出Token: {result['total_output_tokens']:,}")
print(f"平均每步成本: ${result['avg_cost_per_step']:.4f}")
print(f"\n分步明细:")
for s in result['step_breakdown']:
    print(f"  步骤{s['step']}: 输入{s['input_tokens']:>5} + 输出{s['output_tokens']:>4} = ${s['cost_usd']:.4f}")

# 月度成本估算
tasks_per_day = 1000
monthly_cost = result['total_cost_usd'] * tasks_per_day * 30
print(f"\n按每天{tasks_per_day}任务估算，月度成本: ${monthly_cost:.2f}")
```

#### 吞吐率

**定义：**

$$
\text{Throughput} = \frac{\text{输出 Token 数}}{\text{生成耗时（秒）}} \quad (\text{tokens/s})
$$

注意：生成耗时指从开始输出到输出结束的时间，**不含首 Token 延迟**。

**代码示例：**

```python
def compute_throughput(
    output_tokens: int,
    generation_time_seconds: float,
) -> dict:
    """
    计算吞吐率。
    
    Args:
        output_tokens: 输出的 Token 总数
        generation_time_seconds: 生成阶段耗时（秒），不含首Token延迟
    
    Returns:
        包含 throughput 和相关指标的字典
    """
    if generation_time_seconds <= 0:
        return {"throughput_tokens_per_s": 0.0, "error": "生成时间必须大于0"}
    
    throughput = output_tokens / generation_time_seconds
    
    return {
        "throughput_tokens_per_s": throughput,
        "output_tokens": output_tokens,
        "generation_time_s": generation_time_seconds,
        "ms_per_token": 1000 / throughput if throughput > 0 else float('inf'),
    }


# ========== 测试 ==========
# 场景1：快速生成
r1 = compute_throughput(output_tokens=200, generation_time_seconds=2.5)
print(f"场景1: {r1['throughput_tokens_per_s']:.1f} tokens/s | {r1['ms_per_token']:.1f} ms/token")

# 场景2：慢速生成
r2 = compute_throughput(output_tokens=100, generation_time_seconds=5.0)
print(f"场景2: {r2['throughput_tokens_per_s']:.1f} tokens/s | {r2['ms_per_token']:.1f} ms/token")
```

---

### 方法论选型矩阵

不同评估场景对应不同的方法组合。以下矩阵总结了各场景的推荐方法、补充方法和成本对比。

#### 评估方法成本与速度对比

| 方法 | 自动化程度 | 单样本成本 | 评估速度 | 可复现性 |
|------|-----------|-----------|---------|---------|
| Exact Match | 全自动 | 极低（字符串比较） | 极快 | 完全可复现 |
| AST Match | 全自动 | 低（AST解析） | 快 | 完全可复现 |
| LLM Judge | 全自动 | 中（API调用费） | 中 | 较好（同模型+温度0） |
| Win Rate | 全自动 | 中（2×LLM调用） | 中 | 较好 |
| 幻觉检测 | 半自动 | 中高（检索+LLM） | 中 | 较好 |
| 人工评估 | 人工 | 高（人力成本） | 慢 | 中等 |

#### 场景选型矩阵

| 评估场景 | 首选方法 | 补充方法 | 关键指标 | 成本 |
|---------|---------|---------|---------|------|
| 数学题（AIME等） | Exact Match | — | Accuracy | 低 |
| 函数调用/工具使用 | AST Match | LLM Judge（复杂参数） | Match Rate | 低 |
| 开放式问答 | LLM Judge (Pointwise) | 人工抽样复审 | 各维度均分 | 中 |
| 模型对比/排行榜 | Win Rate (Pairwise) | Length-controlled Win Rate | Win Rate % | 中 |
| 知识准确性 | FActScore | 检索增强交叉验证 | FActScore, 幻觉率 | 中高 |
| 多轮对话 | LLM Judge + 人工 | 自我一致性检测 | 对话连贯性评分 | 中高 |
| 代码生成 | AST Match + 单元测试 | 人工安全审计 | 通过率, 安全性 | 中 |
| 创意写作 | 人工评估 | LLM Judge（辅助） | 主观评分 | 高 |
| 生产监控 | 自动化全量 + 人工抽样 | 幻觉率监控 | P95延迟, 幻觉率, 成本 | 中 |
| 安全/合规 | 人工评估 | LLM Judge（初筛） | 违规率 | 高 |

#### 选型决策流程

```
1. 任务有唯一正确答案？
   ├─ 是 → Exact Match（数学题、选择题）
   └─ 否 → 2

2. 输出是代码/函数调用？
   ├─ 是 → AST Match + 单元测试
   └─ 否 → 3

3. 需要比较两个模型的优劣？
   ├─ 是 → Win Rate（双向评估消除位置偏置）
   └─ 否 → 4

4. 需要检测事实准确性？
   ├─ 是 → FActScore + 检索增强交叉验证
   └─ 否 → 5

5. 任务有主观性/创意性？
   ├─ 是 → 人工评估为主，LLM Judge 辅助
   └─ 否 → LLM Judge (Pointwise) + 人工抽样复审

6. 所有场景都应叠加：
   ├─ 延迟评估（P50/P95/P99）
   ├─ 成本评估（Token消耗 + API费用）
   └─ 生产环境加测幻觉率监控
```

---

## 开源评估框架

### 评估框架概览

随着大语言模型（LLM）和智能体（Agent）技术的快速发展，如何客观、可复现地评估模型能力已成为研究者和工程师面临的核心挑战。传统的单一指标（如准确率、BLEU分数）已无法涵盖现代LLM在函数调用、指令跟随、代码生成、多轮推理等复杂场景下的表现。为此，开源社区涌现出多个专注于不同评估维度的框架，它们各司其职，共同构成了当前智能体评估的工具生态。

当前主流的开源评估框架可以按评估维度大致分为四类：

- **函数调用/工具使用评估**：以BFCL（Berkeley Function-Calling Leaderboard）为代表，专注于评估LLM准确选择和调用函数（工具）的能力，包括AST语法检查和可执行性验证两个维度。
- **指令跟随/对话质量评估**：以AlpacaEval为代表，采用LLM-as-a-Judge范式，通过强模型（如GPT-4）作为评审官，自动评估模型输出的质量，输出胜率（Win Rate）等指标。
- **综合基准评估**：以EleutherAI lm-evaluation-harness为代表，提供统一的评估框架，集成MMLU、GSM8K、TruthfulQA、HellaSwag等数百个标准学术基准测试，支持few-shot评估。
- **智能体任务评估**：以OpenHands为代表，不仅提供评估框架，还提供完整的智能体运行环境，支持SWE-bench、GAIA等需要模型在真实环境中执行任务的基准测试。

下表概览了这四个框架的核心定位：

| 框架 | 维护方 | 核心定位 | GitHub Stars（截至2026年7月） |
|------|--------|---------|--------------------------|
| BFCL (Gorilla) | UC Berkeley | 函数调用/工具使用评估 | 约13K |
| AlpacaEval | Stanford (Tatsu Lab) | 指令跟随自动评估 | 2K+ |
| lm-evaluation-harness | EleutherAI | 统一LM学术基准评估 | 约13K |
| OpenHands | All Hands AI | 通用AI Agent评估与运行 | 约80K |

### BFCL官方评估工具

#### 项目简介

BFCL（Berkeley Function-Calling Leaderboard）是加州大学伯克利分校 Gorilla 项目的重要组成部分，专注于评估大语言模型在准确调用函数（工具）方面的能力。其代码托管在 Gorilla 仓库中：

- **仓库地址**：https://github.com/ShishirPatil/gorilla
- **子目录**：`berkeley-function-call-leaderboard/`
- **排行榜**：https://gorilla.cs.berkeley.edu/leaderboard
- **PyPI包**：`pip install bfcl-eval`

BFCL目前已迭代至V4版本，其评估数据集累计包含5,500+个问题-函数-答案对，覆盖Python、Java、JavaScript、REST API等多种编程语言和使用场景，包括简单函数调用、并行函数调用、多函数选择以及多轮对话（V3版本新增）等场景。

#### 安装与环境配置

BFCL支持通过PyPI安装，也可以从源码安装以获取最新功能：

```bash
# 方式1：PyPI安装（推荐）
pip install bfcl-eval

# 方式2：从源码安装（获取最新功能）
git clone https://github.com/ShishirPatil/gorilla.git
cd gorilla/berkeley-function-call-leaderboard
pip install -e .
```

安装完成后，需要配置环境变量。复制环境变量模板文件并编辑：

```bash
cp bfcl_eval/.env.example .env
```

在`.env`文件中配置必要的API密钥：

```bash
# 模型API密钥
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key

# 如果评估需要Web搜索等功能
SERPAPI_API_KEY=your_serpapi_key
```

对于本地部署的模型，需要通过vLLM等服务将模型以OpenAI兼容API的方式启动。

#### 核心命令与使用流程

BFCL的评估流程主要包含两个步骤：**生成模型响应**和**评估响应质量**。

**步骤1：生成模型响应**

```bash
# 对OpenAI模型生成响应
bfcl generate --model gpt-4o --test-set all

# 对本地模型生成响应（需先通过vLLM启动模型服务）
bfcl generate --model my-local-model --test-set all --base-url http://localhost:8000/v1
```

**步骤2：评估响应**

```bash
# 评估模型输出
bfcl evaluate --model gpt-4o --test-set all
```

`--test-set`参数支持多种数据子集选择：
- `all`：包含全部评测数据集
- `single_turn`：仅单轮对话评测（V1+V2版本数据集）
- `multi_turn`：仅多轮对话评测（V3版本新增）
- `live`：仅包含用户提供的、定期更新的live评测数据
- `ast`：本地函数调用，评测函数名和函数参数的正确性
- `executable`：需要实际执行函数并验证结果

#### Handler机制详解

BFCL采用Handler模式来适配不同模型的推理接口。每个模型对应一个Handler类，负责处理该模型的输入格式、输出解析和特定适配逻辑。

**目录结构**：

```
berkeley-function-call-leaderboard/
├── bfcl/
│   ├── model_handler/
│   │   ├── base_handler.py          # 基类，所有Handler继承自此类
│   │   ├── handler_map.py           # 模型名称到Handler类的映射
│   │   ├── oss_model/               # 本地部署模型Handler
│   │   │   ├── base_oss_handler.py
│   │   │   ├── llama_fc.py          # LLaMA (FC mode)
│   │   │   ├── deepseek_coder.py
│   │   │   └── ...
│   │   ├── proprietary_model/       # API调用模型Handler
│   │   │   ├── openai.py            # OpenAI模型
│   │   │   ├── claude.py            # Anthropic Claude
│   │   │   └── ...
│   │   └── parser/                  # 多语言解析工具
│   ├── eval_checker/
│   │   ├── eval_runner.py           # 评估主入口
│   │   ├── ast_eval/                # AST语法检查
│   │   │   └── ast_checker.py
│   │   ├── executable_eval/         # 可执行性验证
│   │   ├── multi_turn_eval/        # 多轮对话评估
│   │   └── model_metadata.py       # 模型元数据配置
│   ├── constant.py                 # 常量配置
│   ├── data/                        # 评测数据集
│   ├── result/                      # 模型响应结果
│   └── score/                       # 评估得分
```

**BaseHandler的核心接口**：

每个Handler继承自`BaseHandler`，需要实现以下关键方法：
- `__init__`：初始化模型客户端和配置
- `decode_ast`：将模型输出解析为AST（抽象语法树）可评估的结构
- `decode_execute`：将模型输出转换为可执行的函数调用格式

**为本地模型编写自定义Handler的步骤**：

1. **创建Handler类**：在`bfcl/model_handler/oss_model/`目录下创建新的Python文件，继承`BaseHandler`（或`BaseOSSHandler`）：

```python
# bfcl/model_handler/oss_model/my_model.py
import json
from bfcl.model_handler.base_handler import BaseHandler
from bfcl.model_handler.model_style import ModelStyle
from bfcl.model_handler.utils import (
    default_decode_ast_prompting,
    default_decode_execute_prompting,
)

class MyModelHandler(BaseHandler):
    def __init__(self, model_name, temperature):
        super().__init__(model_name, temperature)
        self.model_style = ModelStyle.OpenAI  # 根据模型API风格选择
        self.base_url = "http://localhost:8000/v1"  # 本地模型API地址
        # 初始化客户端...

    def decode_ast(self, result, language="Python"):
        # 将模型输出解析为AST可评估格式
        return default_decode_ast_prompting(result, language)

    def decode_execute(self, result):
        # 将模型输出转换为可执行的函数调用
        return default_decode_execute_prompting(result)
```

2. **注册Handler映射**：在`bfcl/model_handler/handler_map.py`中添加映射：

```python
# handler_map.py
from bfcl.model_handler.oss_model.my_model import MyModelHandler

handler_map = {
    # ... 已有的映射
    "my-local-model": MyModelHandler,
}
```

3. **配置模型元数据**：在`bfcl/eval_checker/model_metadata.py`中添加模型信息：

```python
# model_metadata.py
model_metadata = {
    # ... 已有的模型
    "my-local-model": {
        "organization": "MyOrg",
        "license": "Apache-2.0",
        "cost": 0,  # 本地模型成本为0
    },
}
```

4. **（可选）调整常量配置**：如有特殊需求，可在`bfcl/constant.py`中调整相关常量。

#### 评估结果输出格式

BFCL的评估结果保存在`score/`目录下，以JSON格式输出。核心评估维度包括：

- **AST Accuracy**：抽象语法树检查，验证模型输出的函数名和参数是否语法正确
- **Exec Accuracy**：可执行性检查，在沙箱环境中实际执行模型生成的代码，验证执行结果是否正确

输出结果示例结构：

```json
{
  "model": "gpt-4o",
  "test_set": "all",
  "overall_accuracy": 0.85,
  "ast_accuracy": 0.88,
  "exec_accuracy": 0.82,
  "breakdown": {
    "simple": 0.92,
    "parallel": 0.78,
    "multi_turn": 0.75
  },
  "latency": {
    "p95": 2.5,
    "p99": 3.8
  },
  "cost": 0.002
}
```

BFCL不仅评估准确率，还报告P95/P99延迟和成本指标，方便用户综合考量模型的经济效率和性能。

### AlpacaEval

#### 项目简介

AlpacaEval是斯坦福大学Tatsu Lab开发的开源自动评估器，专注于评估指令跟随（Instruction-Following）语言模型的质量。它采用LLM-as-a-Judge范式，使用强大的模型（如GPT-4 Turbo）作为自动评审官，对待评估模型的输出与基准模型的输出进行成对比较，自动判定"哪一个更好"。

- **仓库地址**：https://github.com/tatsu-lab/alpaca_eval
- **PyPI包**：`pip install alpaca-eval`
- **论文**："Length-Controlled AlpacaEval: A Simple Way to Debias Automatic Evaluators" (Dubois et al., 2024)

AlpacaEval的核心优势在于：快速、廉价、可复现。它已与20,000+条人工标注的偏好数据进行验证，确保自动评估结果与人类判断高度一致。AlpacaEval 2.0版本引入了长度控制的胜率（Length-controlled Win Rate），大幅提高了评估的公正性。

##### 核心功能

**Win Rate（胜率）**：

基本胜率指标衡量待评估模型的输出在LLM评审官的成对比较中胜过基准模型（默认为GPT-4 Turbo）的比例。计算方式为：对于每个评估样本，LLM评审官比较待评估模型输出和基准模型输出，判定胜者。胜率 = 胜出次数 / 总比较次数。

**Length-controlled Win Rate（长度控制胜率）**：

AlpacaEval 2.0引入的关键改进。由于LLM评审官存在"偏好更长输出"的偏差（length bias），原始胜率可能高估生成更长文本的模型。长度控制胜率通过统计方法（回归分析）控制输出长度对评估结果的影响，使评估结果更加公正。该方法在论文中验证后，与人工评估的相关性显著提高。

#### 安装与基本使用

```bash
# 安装
pip install alpaca-eval

# 推荐在独立conda环境中安装
conda create -n alpacaeval python=3.10
conda activate alpacaeval
pip install alpaca-eval
```

基本评估命令：

```bash
# 评估本地模型（需先配置模型）
alpaca_eval --model my_model --annotator_config alpaca_eval_gpt4

# 使用OpenRouter API评估
alpaca_eval \
  --model my_model \
  --annotator_config alpaca_eval_gpt4 \
  --api_base https://openrouter.ai/api/v1
```

评估流程：AlpacaEval加载评估数据集 → 模型生成输出 → LLM评审官进行成对比较 → 计算胜率和长度控制胜率 → 输出结果报告。

#### 自定义评估器配置

AlpacaEval使用YAML配置文件管理模型和评估器的配置。

**模型配置（model_configs）**：

在`alpaca_eval/models_configs/`目录下，每个模型对应一个子目录，包含`configs.yaml`配置文件。配置示例：

```yaml
# models_configs/my_model/configs.yaml
# 模型基本信息
model: "my-model-name"
# 生成参数
temperature: 0.7
max_tokens: 2048
# API配置
api_base: "https://api.example.com/v1"
api_key_env: "MY_MODEL_API_KEY"
# 生成函数
generate_func: "alpaca_eval.models.openai.generate_completions"
```

**评估器配置（evaluator_configs）**：

在`alpaca_eval/evaluators_configs/`目录下，每个评估器对应一个子目录。例如GPT-4评估器：

```yaml
# evaluators_configs/alpaca_eval_gpt4/configs.yaml
# 评估器模型
model: "gpt-4-1106-preview"
# 评估提示词模板
prompt_template: "alpaca_eval.prompts.alpaca_eval_prompt"
# 评估方法
eval_method: "pairwise"
# 生成参数
temperature: 0.0
max_tokens: 1024
```

**自定义评估器**：

研究者可以创建自己的评估器，只需在`evaluators_configs/`目录下新建子目录并编写`configs.yaml`。AlpacaEval提供了基类`PairwiseEvaluator`和配套工具，支持缓存、批处理等功能，便于快速实验不同的评估器配置。

### EleutherAI LM Eval Harness

#### 项目简介

lm-evaluation-harness是EleutherAI开发的开源语言模型评估框架，也是Hugging Face官方Open LLM Leaderboard的后端评估引擎。它提供了一个统一的框架，用于在大量标准学术基准测试上评估语言模型，支持200+个任务和数百个子任务/变体。

- **仓库地址**：https://github.com/EleutherAI/lm-evaluation-harness
- **PyPI包**：`pip install lm-eval`

#### 支持的评估基准

lm-evaluation-harness内置了大量标准学术基准测试，主要包括：

| 基准 | 全称 | 评估维度 | 说明 |
|------|------|---------|------|
| MMLU | Massive Multitask Language Understanding | 多领域知识 | 57个子学科的多选题 |
| GSM8K | Grade School Math 8K | 数学推理 | 小学数学应用题 |
| TruthfulQA | Truthful QA | 事实准确性 | 测试模型是否避免生成错误信息 |
| HellaSwag | HellaSwag | 常识推理 | 情境理解与下一事件预测 |
| ARC | AI2 Reasoning Challenge | 科学推理 | 中小学科学考试题 |
| Winogrande | Winogrande | 常识推理 | 代词消解与常识判断 |
| HumanEval | HumanEval | 代码生成 | Python函数编写 |
| BBH | BIG-Bench Hard | 综合能力 | 23个高难度推理任务 |
| C-Eval | C-Eval | 中文综合能力 | 中文学科评估 |
| CMMLU | CMMLU | 中文多领域知识 | 中文版MMLU |

此外，框架还支持通过Hugging Face datasets加载自定义数据集，极大扩展了评估的灵活性。

#### 安装与基本使用

```bash
# 基础安装
pip install lm-eval

# 从源码安装（获取最新功能）
git clone https://github.com/EleutherAI/lm-evaluation-harness.git
cd lm-evaluation-harness
pip install -e .
```

> **注意**：从2025年12月起，基础包不再默认包含transformers/torch，需要按需安装模型后端，例如 `pip install lm_eval[hf]` 或 `pip install lm_eval[vllm]`。

**基本评估命令**：

```bash
# 使用Hugging Face模型评估
lm_eval \
  --model hf \
  --model_args pretrained=gpt2 \
  --tasks hellaswag \
  --batch_size 8

# 使用vLLM加速推理
lm_eval \
  --model vllm \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu,gsm8k \
  --batch_size auto

# 评估OpenAI API模型
lm_eval \
  --model local-completions \
  --model_args model=gpt-4o,base_url=http://localhost:8000/v1/completions \
  --tasks truthqa_mc2 \
  --batch_size 5
```

**Python API使用**：

```python
import lm_eval

results = lm_eval.simple_evaluate(
    model="hf",
    model_args="pretrained=gpt2",
    tasks=["hellaswag", "arc_easy"],
    num_fewshot=0,
    batch_size=8,
)

print(results["results"])
```

#### 架构设计

lm-evaluation-harness采用两个核心抽象设计，使其具备高度可扩展性：

**Task抽象**：

所有评估任务都围绕YAML配置文件构建。每个Task定义了：
- 数据集来源（Hugging Face dataset路径）
- 输入处理（`doc_to_text`）：将原始数据转换为模型输入
- 目标处理（`doc_to_target`）：提取标准答案
- 评估指标（`metric_list`）：定义评估方法
- Few-shot配置：支持可配置的few-shot示例数量和采样方式

**Model抽象**：

框架支持多种模型后端，通过统一的接口抽象：
- `HFModel`：通过Hugging Face transformers加载本地模型
- `vLLMModel`：使用vLLM进行高效推理
- `APIModel`：支持OpenAI兼容API的远程模型
- `MultiGPUModel`：支持多GPU并行评估

这种设计使得用户可以轻松地在不同模型后端之间切换，而无需修改评估任务配置。

#### 添加自定义Task

lm-evaluation-harness支持通过YAML配置文件添加自定义任务，无需编写Python代码。以下是创建自定义Task的步骤：

**步骤1：创建YAML配置文件**

在`lm_eval/tasks/`目录下创建新的YAML文件，例如`my_custom_task.yaml`：

```yaml
# my_custom_task.yaml
task: my_custom_task
dataset_path: my_dataset  # Hugging Face dataset名称或本地路径
dataset_name: null
test_split: test
output_type: multiple_choice  # 可选: multiple_choice, loglikelihood, generate_until
doc_to_text: "Question: {{question}}\nAnswer:"
doc_to_target: "{{answer}}"
doc_to_choice: "{{choices}}"
metric_list:
  - metric: acc        # 准确率
    aggregation: mean
    higher_is_better: true
  - metric: acc_norm   # 长度归一化准确率
    aggregation: mean
    higher_is_better: true
metadata:
  version: 1.0
```

**步骤2：验证任务配置**

```bash
# 列出可用任务，确认自定义任务被识别
lm_eval --tasks list | grep my_custom_task

# 验证任务可正确加载
lm_eval --model hf --model_args pretrained=gpt2 --tasks my_custom_task --limit 10
```

**步骤3：运行评估**

```bash
lm_eval \
  --model hf \
  --model_args pretrained=my_model \
  --tasks my_custom_task \
  --batch_size 8
```

对于更复杂的任务（如需要自定义后处理逻辑），可以在YAML中通过`filter_list`和`process_results`等字段引用Python函数，或继承`Task`类编写Python实现。框架使用Jinja2模板引擎进行提示词设计，支持从Promptsource导入已有的提示模板。

### OpenHands (原OpenDevin)

#### 项目简介

OpenHands（原名OpenDevin）是由All Hands AI开发的开源平台，旨在构建自主软件工程智能体。它不仅是一个评估框架，更是一个完整的AI Agent运行和评估平台，支持模型在真实环境中执行任务（如编写代码、运行命令、浏览网页等），并通过标准基准测试评估Agent的端到端能力。

- **仓库地址**：https://github.com/OpenHands/OpenHands
- **官网**：https://www.all-hands.dev/
- **许可证**：MIT

OpenHands在GitHub上已获得41,000+ Stars，是目前最受欢迎的AI Agent开源项目之一。

#### 定位与核心能力

OpenHands的定位是"通用AI Agent评估与运行框架"。与BFCL专注于函数调用语法不同，OpenHands关注的是Agent在真实任务中的端到端表现：

- **自主软件开发**：Agent可以编写代码、运行测试、修复Bug
- **代码库理解与操作**：支持在真实的代码仓库中定位问题并生成补丁
- **多模态交互**：支持命令行执行、文件操作、网页浏览等多种交互方式
- **可视化界面**：提供Web UI和CLI两种交互方式

#### 评估基准支持

OpenHands的评估系统位于仓库的`evaluation/`目录下，支持多种主流Agent评估基准：

```
evaluation/
├── benchmarks/
│   ├── swe_bench/          # SWE-bench：软件工程问题修复
│   ├── gaia/               # GAIA：通用AI助手评估
│   ├── agent_bench/        # AgentBench：多场景Agent评估
│   ├── aider_bench/        # Aider Benchmark：代码编辑
│   ├── biocoder/           # BioCoder：生物信息学编程
│   ├── bird/               # BIRD：文本到SQL
│   ├── browsing_delegation/ # 浏览器代理评估
│   ├── discoverybench/     # DiscoveryBench：数据科学发现
│   ├── gorilla/            # Gorilla API调用评估
│   ├── gpqa/               # GPQA：研究生水平问答
│   ├── humanevalfix/       # HumanEval Fix：代码修复
│   ├── logic_reasoning/    # 逻辑推理评估
│   ├── ml_bench/           # ML-Bench：机器学习任务
│   └── webshell/           # WebShell任务
```

其中最重要的是SWE-bench和GAIA两个基准：

**SWE-bench**：评估模型解决真实GitHub Issue的能力。模型获取一个代码仓库和Issue描述，需要生成补丁解决问题。评估使用FAIL_TO_PASS测试（验证问题是否解决）和PASS_TO_PASS测试（确保不破坏现有功能）。OpenHands在SWE-bench上的评估脚本位于`evaluation/benchmarks/swe_bench/`目录。

**GAIA**：通用AI助手评估基准，测试Agent在真实世界任务中的综合能力，包括推理、工具使用、多步规划等。

#### 架构设计

OpenHands的系统架构包含以下核心组件：

- **用户界面**：提供Web UI和CLI两种交互方式
- **服务器（Server）**：处理会话管理和请求分发
- **控制器（Controller）**：协调各组件工作，管理Agent状态和执行循环
- **AgentHub**：包含多种专业Agent，其中`CodeActAgent`是核心Agent，负责代码相关任务
- **运行时（Runtime）**：提供沙箱环境（基于Docker容器），确保安全执行
- **存储（Storage）**：支持本地和云存储，保存会话状态和结果

#### 使用方法

**安装OpenHands**：

```bash
# 克隆仓库
git clone https://github.com/OpenHands/OpenHands.git
cd OpenHands

# 使用poetry安装依赖
poetry install

# 配置环境变量
cp config.template.toml config.toml
# 编辑config.toml，配置LLM API密钥等
```

**运行SWE-bench评估**：

```bash
# 进入评估目录
cd evaluation/benchmarks/swe_bench

# 运行推理（生成模型输出）
./scripts/run_infer.sh \
  llm.eval_gpt4_1106_preview \
  HEAD \
  CodeActAgent \
  10  # 并发任务数

# 评估结果
python eval/eval_infer.py
```

评估流程：任务调度层通过`run_infer.sh`控制并发 → 执行引擎层基于Docker容器化每个测试实例（如`sweb.eval.x86_64.django_s_django-11011`镜像） → 结果分析层生成包含通过率、迭代次数等指标的报告。

**运行GAIA评估**：

```bash
cd evaluation/benchmarks/gaia
# 运行GAIA基准测试
python run_infer.py \
  --llm-name gpt-4o \
  --agent-class CodeActAgent \
  --max-iterations 30
```

### 框架对比与选型

#### 功能对比

| 框架 | 定位 | 支持的评估 | 易用性 | 扩展性 | 适用场景 |
|------|------|-----------|--------|--------|---------|
| BFCL (Gorilla) | 函数调用/工具使用专项评估 | 函数调用准确率（AST+Exec）、多轮工具使用、延迟和成本分析 | 中等：需配置Handler和模型服务 | 高：支持自定义Handler适配任意模型 | 评估模型的Function Calling/Tool Use能力；智能体工具调用能力评测 |
| AlpacaEval | 指令跟随质量自动评估 | Win Rate、Length-controlled Win Rate | 高：pip安装即用，一行命令评估 | 中：支持自定义评估器配置，但局限于LLM-as-a-Judge范式 | 快速评估模型对话/指令跟随质量；模型开发中的快速迭代反馈 |
| lm-evaluation-harness | 统一LM学术基准评估 | 200+标准学术基准（MMLU、GSM8K、TruthfulQA等） | 高：统一CLI接口，配置简洁 | 高：YAML配置自定义Task，支持多种模型后端 | 学术论文标准评估；Open LLM Leaderboard提交；全面的模型能力画像 |
| OpenHands | 通用AI Agent评估与运行 | SWE-bench、GAIA、AgentBench、HumanEval等端到端Agent任务 | 中等：需Docker环境，配置较复杂 | 高：支持自定义Agent和评估基准 | 评估Agent端到端任务完成能力；软件开发智能体评估；真实环境中的Agent评测 |

#### 选型建议

**场景1：评估模型的Function Calling能力**

如果你的关注点是模型能否准确调用函数（如选择正确的函数、生成正确的参数、支持并行调用和多轮调用），BFCL是首选。它的AST检查和可执行性验证双维度评估，以及丰富的测试数据集，使其成为目前最全面的函数调用评估工具。适用于：
- 评估模型的Tool Use能力
- 对比不同模型的函数调用性能
- 智能体应用开发中的模型选型

**场景2：快速评估指令跟随质量**

如果你需要快速获取模型在对话/指令跟随方面的质量评分，AlpacaEval是最佳选择。它成本极低（仅需一次API调用的成本），速度快，适合在模型开发迭代过程中作为快速反馈指标。适用于：
- 模型微调过程中的快速质量评估
- 对比多个模型的对话输出质量
- 需要与已有排行榜模型进行快速对比

**场景3：全面的学术基准评估**

如果你需要按照学术标准全面评估模型的各方面能力（知识、推理、数学、代码等），lm-evaluation-harness是行业标准。它是Hugging Face Open LLM Leaderboard的后端，拥有最全面的基准测试覆盖和最活跃的社区支持。适用于：
- 准备学术论文的模型评估
- 提交Open LLM Leaderboard
- 需要可复现、可对比的标准评估结果

**场景4：评估Agent端到端任务完成能力**

如果你关注的是Agent在真实环境中的端到端表现（如修复真实代码Bug、完成多步骤复杂任务），OpenHands提供了最完整的解决方案。它不仅评估模型输出，还评估Agent在真实环境中的行为序列和最终成果。适用于：
- 评估软件开发Agent的实际能力
- 研究Agent的多步规划和工具使用
- 对比不同Agent架构（如CodeActAgent vs其他Agent）在真实任务上的表现

**多框架组合使用建议**：

在实际项目中，建议组合使用多个框架以获得全面的评估画像。例如：
1. 使用 **lm-evaluation-harness** 获取模型的基础能力画像（知识、推理、数学）
2. 使用 **BFCL** 评估模型的函数调用能力
3. 使用 **AlpacaEval** 进行快速迭代反馈
4. 使用 **OpenHands** 评估Agent的端到端任务完成能力

这种多维度评估策略可以全面覆盖从基础能力到复杂任务的评估需求，避免单一评估框架的局限性。

## 未来方向

### 在线评估与持续评估

#### 从静态评估集到动态在线评估

静态评估集对 Agent 有三大根本局限：能力边界近乎无限（写代码、订机票、操作网页…），无法穷尽覆盖；行为路径依赖（同一任务多种工具组合都能成功），只看最终输出会丢失过程质量；评估集污染风险（工具 API、网站界面随时间变化，旧评估集失效）。

动态在线评估（Dynamic Online Evaluation）改为在生产环境中持续、自适应地评估：**实时数据流**（自动收集交互记录与用户反馈）、**自适应难度调节**（类似自适应测试，连续通过则提难度、频繁失败则降难度以定位能力边界）、**分布漂移检测**（用户请求分布变化时动态调整评估权重）。落地挑战包括评估信号延迟（用户可能数周才发现问题）、隐私脱敏、以及在线与离线评估的变量控制平衡。

#### 影子模式（Shadow Mode）

借鉴 MLOps：用户请求同时发给生产 Agent（输出返用户）和候选 Agent（输出仅记录分析），在不影响用户的前提下对比两版输出。对 Agent 尤有价值——候选 Agent 无需执行真实副作用操作即可展示"会做什么"，且可在真实长对话流中评估上下文管理，并对比工具调用序列差异。实施要点：需语义级输出对齐（非字面匹配）、控制影子流量比例（5%–10%）、结构化日志（含完整推理链与工具调用）。

#### Canary 发布

渐进式上线：内部测试 → 小流量（1%–5%，看异常率与满意度）→ 中流量（5%–20%，做统计显著性检验与业务指标）→ 全量。Agent 的特殊考量：输出方差大需更大样本量；关注尾部风险（最差 5% 质量）而非均值；某些任务效果需延迟反馈（如旅行计划）。关键安全网是**自动回滚**——预设核心指标阈值（如完成率不低于旧版 −2%），突破即自动回滚。

#### 在线 A/B 测试的统计方法

- **样本量**：取决于最小可检测效应（MDE）、基线方差、功效（0.8）与显著性（0.05）；Agent 输出方差大，需更大样本（针对高方差的样本量校正尚缺专门研究）。
- **多重比较**：同时检验多指标会膨胀第一类错误，用 Bonferroni（保守）、Benjamini-Hochberg（控 FDR）或层次化检验校正。
- **序贯检验**：允许数据积累中多次查看结果并控整体错误率，可与 Canary 结合，达到显著即提前决策。
- **异质性效应（HTE）**：整体效果可能掩盖子群体差异（如新版对老用户友好、对新用户更差），用分组分析或 Causal Forest/Uplift Modeling 估计。
- **辛普森悖论**：不同时段用户构成差异可能使整体结论与子群体相反，需控制时间段协变量、均衡流量分配、分层分析。

### 自动化评估的进步

#### 更强的 LLM Judge

单一 Judge 有系统性偏差，改进方向有三：**多模型交叉评估**（集成投票取均值/中位数、分歧超阈值引入第三方裁决、按维度专精分工）；**校准技术**（锚点样本校准、排名转评分用 Bradley-Terry、温度缩放对齐人类分布）；**偏差缓解**——LLM Judge 已知位置偏差、冗长偏差、权威偏差，可用位置交换双向评估、长度归一化、盲评缓解（单 vs 多 Judge 的可靠度对比与边际收益拐点尚缺系统实证）。

#### 自动生成评估样本

用 LLM 自动生成测试用例以降低人工成本，方法包括种子扩展、场景模拟（模拟不同用户角色）、对抗生成（歧义/隐含约束/多步依赖）、变异测试（改约束/加干扰）。质量风险：ground truth 不可验证、分布偏移、幻觉标注（编造不存在的 API）。保障手段：人工抽检、多模型一致性过滤、工具调用的可执行性验证。

#### 评估的评估（Meta-evaluation）

衡量 Judge 可靠性看四点：与人类一致率（精确/近似/排序相关）、区分度、稳定性（多次评分方差）、偏差量化。代表性元评估基准：**JudgeBench**（arXiv 2410.12784，ICLR 2025，ScalerLab，用对抗性回答对覆盖知识/推理/数学/编程）、**JuStRank**（arXiv 2412.09569，从系统排序视角评估裁判）。二者主要面向通用 LLM 输出，Agent 专用元评估基准仍在建设中。元评估结果可反哺 Judge：基于偏差修正提示、少样本校准、奖励模型微调。

### 多模态Agent评估

#### 视觉+语言+动作的联合评估

多模态评估比文本难在三处：**跨模态对齐**（视觉内容与语言描述的语义对齐是模糊的）、**动作时序性**（点击/拖拽/输入的顺序与时机）、**模态间一致性**（文本推理与视觉决策可能不一致）。综合评估维度：

| 维度 | 示例指标 |
|------|----------|
| 视觉感知准确性 | 元素识别准确率、位置定位误差 |
| 语言理解准确性 | 指令解析准确率 |
| 跨模态推理 | 跨模态推理正确率 |
| 动作执行准确性 | 动作类型正确率、参数准确率 |
| 动作序列合理性 | 最短路径比、冗余动作率 |
| 多模态输出质量 | 视觉一致性、语言流畅度、图文匹配度 |

#### 多模态输出的 Ground Truth 构建

多模态输出（如"图文旅行攻略"）正确答案近乎无限，传统标准答案比对失效；且存在主观性（不同评估者偏好差异）与部分正确性（数据对但标注错）。可行路径：参考集方法（与高质量参考输出比相似度，借鉴 RAG 评估）、分解评估（按事实/语言/视觉/模态一致性分子维度）、人类偏好数据（收集 A vs B 偏好，用 Bradley-Terry/Elo 转相对分，即 RLHF 思路）。

#### GUI Agent 评估

GUI Agent 通过视觉理解屏幕、规划并执行鼠标键盘动作完成任务，场景分网页交互、桌面应用、移动应用、跨应用工作流。评估指标分层：任务完成率、步骤效率、操作精确度、错误恢复能力、时间效率。核心挑战是构建可复现环境（界面渲染受浏览器/系统/分辨率影响），方式有沙盒、模拟、真实录制回放，各有真实性与可控性的权衡。

公认基准（可直接用于评测）：**WebArena**（812 个网页任务）、**OSWorld**（真实 Ubuntu 桌面 369 任务）、**AndroidWorld**（116 个移动任务）、**Mind2Web**（2000+ 网页任务，跨网站泛化）、**ScreenSpot**（界面元素定位）。可按"网页/桌面/移动/跨应用"场景组合选用。

### 多Agent系统评估

协作模式分四类：层级结构（主控分解+执行）、平等协作（协商共识）、竞争对抗（辩论）、流水线协作（环节衔接）。除个体能力外，需评估：

- **分工合理性**：任务-能力匹配、负载均衡、冗余度。
- **通信效率**：通信量、轮次、信息冗余、时机恰当性（每条消息都耗 token 与延迟）。
- **冲突处理**：识别、解决机制（投票/加权/仲裁）、解决质量（理想是 1+1>2）。
- **整体效能**：质量提升、效率比（多耗 3 倍资源只提升 10% 则性价比存疑）、涌现能力。

| 评估方面 | 单Agent | 多Agent |
|----------|---------|---------|
| 评估对象 | 个体能力 | 个体 + 协作能力 |
| 成功标准 | 任务完成质量 | 质量 + 协作效率 |
| 失败归因 | 自身能力不足 | 个体/通信/分工皆可能 |
| 评估成本 | 单次运行 | 多次运行覆盖不同协作模式 |
| 可复现性 | 较高 | 较低（交互引入随机性） |

独特挑战是**因果归因**——任务失败时确定哪个 Agent 的哪个决策所致，流水线中错误会被放大或修正，需 Agent 行为追踪与因果分析。

### 评估的可复现性

#### 评估环境容器化

Agent 行为不仅取决于代码，还取决于模型版本、环境状态、随机种子，可复现性比传统软件更难。容器化是核心技术：镜像应含运行时、模拟工具与 API、评估数据集、评估脚本、环境配置。实践挑战：云端 API 模型无法打包进容器（需固定版本快照或本地部署）、动态外部服务（模拟则失真、真实则不可复现）、硬件差异（GPU 影响推理速度）。

#### 评估结果的可复现挑战

即使环境一致，结果仍可能不可复现：**模型版本漂移**（同名模型底层更新，需记录精确版本标识）；**采样随机性**（temperature>0 时 Agent 的随机性会被多步放大，建议固定种子或多次运行取统计量）；**环境差异**（网络延迟、并发干扰、时区相关行为）。

#### 评估协议标准化

当前缺乏统一协议，结果难以跨团队比较。标准应涵盖任务定义、指标定义、评估流程、报告格式（配置/版本/环境/样本量/方差/置信区间）、复现指南。推进难点：Agent 类型多样难统一、商业利益、技术快速演进（ISO/IEEE/ACM 或社区是否在推动标准化尚待查证）。

### 开放问题

- **如何评估"创造性"？** 创造性体现在解决方案新颖性、工具创新使用、问题重构、跨领域类比。可能方法：多样性度量（多次输出的差异）、惊喜度评估（人类主观判断）、远距离联想测试。尚缺系统化框架与基准。
- **如何评估长期记忆？** 维度包括记忆准确性（事实+时间）、检索相关性、整合能力、遗忘管理、跨会话迁移。挑战：需数天到数月时间跨度、Ground Truth 主观动态、交互路径依赖、隐私伦理。
- **如何评估开放环境适应能力？** 表现为新工具适应、环境变化适应、未知任务处理、资源约束降级。传统"给定任务→执行→评分"范式不足，需开放式交互评估、环境变异测试、持续学习评估。
- **如何降低评估成本？** 路径：开源评估基础设施（类比 JUnit/pytest）、合成数据替代人工标注、分层评估策略（快速/标准/深度）、众包评估平台、CI/CD 自动化流水线、轻量级评估模型（蒸馏降本）。根本上也是行业协作问题——评估标准、数据、工具成为公共物品时全行业受益。