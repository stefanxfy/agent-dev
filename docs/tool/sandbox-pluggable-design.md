# 沙箱后端可插拔架构设计

> 本文档定义 agent-dev 沙箱**执行层**的可插拔重构方案。
> 上游依据:[sandbox-implementation.md](./sandbox-implementation.md)(CC 沙箱解读)、[tool-security-architecture.md](./tool-security-architecture.md) §5。
> 配套:本文档是 **WHAT**(架构 + 决策);实施清单(plan)是 **HOW**(步骤 + effort + 完成定义),单独产出。

---

## 一、背景与动机

### 1.1 现状

`agent_core/tools/sandbox_manager.py` 是单例 `SandboxManager`,`wrap_with_sandbox` 硬编码调用 `npx -y @anthropic-ai/sandbox-runtime@latest wrap --config <json> -- <cmd>`。

### 1.2 实测缺陷(2026-07-04 触发确认)

| 缺陷 | 证据 |
|---|---|
| **CLI 接口不匹配 → 命令 127 失败** | `sandbox-runtime@1.0.0` 的 bin 是 `srt`,无 `wrap` 子命令;代码拼的 `wrap` 被 shell 当命令名 → `/bin/bash: wrap: command not found` → `exit_code=127`(日志 `logs/app/agent.log` 17:38:42/49/57 + 17:39:05 四次复现) |
| **零真集成测试 → CLI bug 漏网** | `enabled_sandbox` fixture 全 mock 平台/依赖/初始化,从未真跑 npx,所以接口错误从未被测出 |
| **`cleanup_after_command` 孤儿** | 定义于 `sandbox_manager.py:265`,但 `builtin.py` BashTool 执行链从不调用 → CC #29316 bare-git 防护空转 |
| **平台检测错位** | `_is_supported_platform` / `_check_dependencies`(`sandbox_manager.py:308/329`)是 NativeBackend 的实现细节,却放在 SandboxManager(UseCase 层) |
| **多 session 单例污染隐患** | 模块级单例 `sandbox_manager` 被 Streamlit 多 session 共享,后一个 session 的 `load_config` 覆盖前一个 |

### 1.3 重构动机

1. **可插拔**:加新 backend 不动核心(沙箱判定、配置生成、permission 引擎)
2. **可测**:每个 backend 一条真集成测试,根除"零真集成测试"
3. **可观测**:auto 模式下用户能知道实际用了哪个 backend
4. **契合 Python 架构**:NativeBackend 直调 `sandbox-exec`/`bwrap`,去掉 Node/npx 供应链摩擦;SrtBackend 支持独立二进制,保留 CC 同款语义

---

## 二、设计目标与非目标

### 2.1 目标

- 支持两个 backend:**NativeBackend**(Python 直调 `sandbox-exec` / `bwrap`)+ **SrtBackend**(`srt` 独立二进制)
- **auto + 显式**两种切换模式
- 加第三个 backend(Docker/firejail/远程)= 加一个文件 + 组合根注册一行,**不动核心**
- 每个 backend 可独立真集成测试

### 2.2 非目标(本次不做,独立任务)

| 项 | 归属 |
|---|---|
| `audit_logger` 注入断链修复(`web/app.py:749` 构造 engine 漏传) | permission 层 P0,独立 |
| classifier LLM 兜底永 stub | permission 层 P1,独立 |
| `permission_loader._parse_settings_dict` 死代码清理 | permission 层 P2,独立 |
| engine Step 1.5+5 重复跑 hook | permission 层 P2,独立 |

---

## 三、已锁定决策

| 决策 | 内容 |
|---|---|
| **A** | auto 模式默认优先级 `["native", "srt"]`(native 零外部依赖、可测、契合 Python) |
| **B(修正)** | `SandboxRuntimeConfig` 提升为正式 dataclass,但**是纯数据(零翻译方法)**;翻译职责(`_cfg_to_srt_json` / `_cfg_to_seatbelt_profile` / `_cfg_to_bwrap_argv`)是各 backend 的**私有方法**。加新 backend 不动 config |
| **C** | MVP 不做 capability 声明(假设两 backend 都支持 fs+network 全集) |
| **D** | 新 settings.json schema:`backend` / `backendPriority` / `backends.{srt,native}`;老配置缺字段 → 默认 `auto` = native 优先(向后兼容,但属于隐形行为变化,需 release notes 说明) |
| **E** | 强制可观测性日志:`backend_selected` / `backend_unavailable` / 每次 wrap 记录 backend 名 |
| **F** | 每 backend 一条真集成测试(不 mock,按平台 skip) |
| **注入方式** | **静态显式注入**:组合根(web/app.py `get_agent()`)显式构造 backend 列表,依赖注入给 SandboxManager。**不**用模块级全局 registry |

---

## 四、架构(同心圆 + 依赖方向)

```text
                    ┌─────────────────────────────────────┐
   Framework        │  srt binary · sandbox-exec · bwrap  │  ← 最易变,实现细节
   (细节/可换)       │  subprocess · binary path · npx     │
                    ├─────────────────────────────────────┤
   Adapter          │  SrtBackend        NativeBackend    │  ← 翻译 + subprocess
   (实现细节)        │  (各 backend 私有 translator,       │
                    │   不污染内层 config)                │
                    ├─────────────────────────────────────┤
   UseCase          │  SandboxManager                     │  ← 编排:判可用→选→wrap
   (应用规则)        │  select_backend(调度策略)          │
                    │  _build_runtime_config              │
                    ├─────────────────────────────────────┤
   Entity           │  SandboxBackend (Protocol)          │  ← 最稳定,抽象
   (业务策略)        │  SandboxRuntimeConfig (纯数据)      │
                    │  should_use_sandbox (判定规则)       │
                    └─────────────────────────────────────┘
              依赖箭头:全部向内(外层知内层,内层不知外层)
```

**依赖铁律**:源码依赖只指向内层。Entity 层(SandboxRuntimeConfig / Protocol / should_use_sandbox)不知道任何具体 backend、不 import subprocess、不 import 平台模块。

---

## 五、核心抽象

### 5.1 SandboxRuntimeConfig(Entity 层,纯数据)

```python
@dataclass
class SandboxRuntimeConfig:
    """backend 无关的沙箱规则语义。零行为,零翻译方法。"""
    fs_allow_write: list[str]
    fs_deny_write: list[str]
    fs_allow_read:  list[str]
    fs_deny_read:   list[str]
    net_allowed: list[str]
    net_denied: list[str]
```

由 `SandboxManager._build_runtime_config()` 构造(从应用层 permission 规则 + sandbox settings → 此 dataclass)。

### 5.2 SandboxBackend Protocol(UseCase↔Adapter 边界)

```python
class SandboxBackend(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def initialize(self) -> None: ...
    def wrap(self, command: str, cfg: SandboxRuntimeConfig,
             working_dir: str) -> str: ...
    def cleanup(self) -> None: ...
```

**契约**:`is_available()=False` 时 `wrap()` 不被调用(由 SandboxManager 在 select 阶段保证)。`wrap` 返回给 `subprocess.run(shell=True)` 的命令字符串。

### 5.3 两个实现(Adapter 层)

| Backend | 平台 | `is_available()` | `wrap()` 产出 | translator |
|---|---|---|---|---|
| `NativeBackend` | macOS | `/usr/bin/sandbox-exec` 存在 | `sandbox-exec -p '<profile>' <cmd>` | `_cfg_to_seatbelt(cfg)` |
| `NativeBackend` | Linux | `which bwrap` 存在 | `bwrap --unshare-net --ro-bind … -- <cmd>` | `_cfg_to_bwrap_argv(cfg)` |
| `SrtBackend` | 通用 | `which srt` 或 `binaryPath` 存在 | `srt -s <tmp.json> -c <cmd>` | `_cfg_to_srt_json(cfg)` |

translator 是各 backend 的**私有方法**,接收 `SandboxRuntimeConfig`,产出该 backend 的原生格式。

### 5.4 NullBackend(兜底)

显式 backend 不可用且 `failIfUnavailable=false` 时使用。`wrap()` 返回原命令 + 打 WARNING。**不静默**(修正现状 fail-open 静默降级问题)。

---

## 六、切换机制(auto + 显式)

**关键原则**:auto 模式才跨 backend 探测;显式模式**不**偷偷 cross-fallback(违反用户显式意图)。

| `backend` | 选中 backend 可用? | `failIfUnavailable` | 行为 |
|---|---|---|---|
| `auto` | — | — | 按 `backendPriority` 顺序探测 `is_available()`,首个为真的用 |
| `auto` | 全部不可用 | false | `NullBackend` + 醒目警告 |
| `auto` | 全部不可用 | true | `SystemExit(1)` |
| `srt` / `native` | ✅ | — | 用它 |
| `srt` / `native` | ❌ | true | `SystemExit(1)` |
| `srt` / `native` | ❌ | false | `NullBackend` + 警告(**不**回退到另一 backend) |

`select_backend(config, backends)` 在 SandboxManager 初始化时执行一次并缓存(对应 web/app.py 启动 `load_config` 时机)。

---

## 七、配置 schema(settings.json)

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": false,
    "autoAllowBashIfSandboxed": true,
    "allowUnsandboxedCommands": true,

    "backend": "auto",
    "backendPriority": ["native", "srt"],
    "backends": {
      "srt":    { "binaryPath": null },
      "native": { "profileDir": null }
    }
  }
}
```

向后兼容:缺 `backend` 字段 → 默认 `"auto"`;缺 `backendPriority` → 默认 `["native","srt"]`;缺 `backends.*` → 各 backend 走自身默认(PATH 自动发现)。

---

## 八、可观测性(决策 E,强制)

| 时机 | 日志 | 级别 |
|---|---|---|
| 启动选中 | `⚙️ [backend_selected] name=<n> reason=<auto_priority[k]_available \| explicit>` | INFO |
| 探测失败 | `⚠️ [backend_unavailable] name=<n> reason=<binary_not_found \| platform_unsupported>` | WARNING |
| 每次 wrap | `⚙️ [backend_wrap] name=<n> cmd_preview=<cmd[:60]>` | DEBUG |
| NullBackend 兜底 | `⚠️ [backend_null_fallback] reason=<explicit_unavailable \| auto_all_unavailable> original=<n>` | WARNING |

logger:`agent_core.sandbox`(现有)。

---

## 九、测试策略(决策 F)

每 backend 一条**真**集成测试(不 mock),按平台 skip:

| 测试 | 平台 | 断言 |
|---|---|---|
| `test_native_macos_seatbelt` | macOS only | 真跑 `sandbox-exec`,写 `denyWrite` 路径 → 断言写失败 |
| `test_native_linux_bwrap` | Linux only | 真跑 `bwrap --unshare-net`,断言网络断 |
| `test_srt_backend_e2e` | 通用(需 srt 二进制) | 真跑 `srt`,断言 wrap 生效 + denyWrite 拦截 |
| `test_select_backend_auto` | 跨平台 | mock is_available 组合,验证优先级 + fallback 矩阵 |
| `test_select_backend_explicit` | 跨平台 | 验证显式模式不 cross-fallback |

CI 策略:平台 skip 用 `pytest.mark.skipif(sys.platform != ...)`。srt 测试用 `which srt` 探测,无则 skip(不阻断 CI)。

---

## 十、与现有代码的对照

| 现有 | 动作 | 依据 |
|---|---|---|
| `SandboxManager` 单例 | **保留形态**,web/app.py 改 per-agent 注入实例;模块级实例留作非 web 入口(CLI/测试)兜底 | P1 多 session 污染 |
| `wrap_with_sandbox` npx 拼装 | **删**,委托 `self._backend.wrap()` | 127 bug 根因 |
| `wrap_with_sandbox_argv` | **删**(M3+ 未用,死代码) | 清理 |
| `_build_runtime_config` | **改**:返回 `SandboxRuntimeConfig` dataclass(非 dict) | 决策 B |
| `_is_supported_platform` / `_check_dependencies` | **下沉**到 `NativeBackend.is_available()` | P1 平台检测错位 |
| `cleanup_after_command` | **接线**:接到 BashTool 执行链(`builtin.py` wrap 后调用) | P1 孤儿 |
| `_get_sandbox_tmp_dir` / `_scrub_bare_git` / `_cleanup_sandbox_tmp_dir` | 保留,作为 `cleanup` 默认实现(base class 或共享 mixin) | 复用 |
| `sandbox_decision.py` | **不动** | backend 无关 |
| `builtin.py` `should_use_sandbox` + `wrap_with_sandbox` 调用点 | **不动**(对外接口签名不变) | 解耦 |
| `permission_engine.py` | **不动** | 非本次范围 |
| 新增 `sandbox_backends/{base,native,srt}.py` | **新建** | 决策 B/E |

---

## 十一、推迟的细节决策

| 项 | 推迟理由 | 升级信号 |
|---|---|---|
| 动态 plugin 注册(entry_points / 配置驱动 / 目录扫描) | 当前 2 个固定 backend,静态注入够用 | 第三方包贡献 backend / 运行时切换需求 |
| capability 声明(`supports_fs` / `supports_network`) | 两 backend 都覆盖 fs+network 全集 | 加第三个 backend 且能力不齐 |
| `cleanup` 拆成可选 Protocol(`SupportsCleanup`) | NullBackend 不需要 cleanup,但 MVP 强行统一可接受 | backend 多样化后 ISP 厯 |
| 服务化部署 | 源码解耦优先,服务是物理表现 | 团队/部署规模需要 |

接口(Protocol + 纯数据 config)已留好,以上都是"物理实现细节",随时可换,不动架构。

---

## 十二、验收标准

对照 [sandbox-implementation.md](./sandbox-implementation.md):

- [ ] **§5.2 SandboxManager 单例形态保留**,但 web/app.py 改注入实例
- [ ] **§5.3 shouldUseSandbox 不变**(`sandbox_decision.py` 零改动)
- [ ] **§5.4 autoAllowBashIfSandboxed 不变**(permission 引擎零改动)
- [ ] **wrap 实际生效**:UI 触发 `ls -la /tmp` 真返回结果(不再 127)
- [ ] **NativeBackend 真集成测试通过**(macOS: denyWrite 拦截生效)
- [ ] **SrtBackend 真集成测试通过**(若有 srt 二进制)
- [ ] **加 DockerBackend 沙盘演练**:只加 `docker_backend.py` + 组合根一行,不动 Entity/UseCase 层(OCP 验证)
- [ ] **可观测性**:启动日志含 `backend_selected`,auto 模式可追溯实际 backend
- [ ] **多 session 隔离**:两个 Streamlit session 用不同 settings 不互相覆盖

---

## 十三、风险提示

1. **Seatbelt profile 语法学习曲线**:NativeBackend 的 `_cfg_to_seatbelt` 要写正确的 `.sb` profile(allowWrite/denyWrite → Seatbelt 规则翻译),参考 [sandbox-implementation.md](./sandbox-implementation.md) §3 路径语义 + CC `sandbox-adapter.ts` 的 `resolvePathPatternForSandbox`。
2. **`sandbox-exec` 官方 deprecated**(Apple 推荐 App Sandbox),但仍可用且是 macOS 命令级沙箱的事实标准 —— 在 CLI 工具场景无更好替代,接受。
3. **bwrap 不支持 glob**(`*`/`?`/`[`):含 glob 的 fs 规则在 Linux 只对部分路径生效,需在 NativeBackend 启动时 warning(对齐 CC `getLinuxGlobPatternWarnings`)。
4. **隐形行为变化**(决策 D):老配置缺 backend 字段 → 默认 native 优先,与原硬编码 srt-via-npx 不同,需 release notes 显式说明。
5. **srt 二进制分发**:SrtBackend 依赖用户预装 `srt`(PATH 或 `binaryPath`),本项目不打包。文档需说明安装方式(`npm i -g` 或 GitHub Releases 二进制)。

---

## 十四、实施记录(2026-07-04)

### 14.1 实施状态
Step 1–7 全部完成。代码:`agent_core/tools/sandbox_backends/`(新建 base/_cleanup/native_backend/srt_backend)+ `sandbox_manager.py` / `builtin.py` / `web/app.py` / `sandbox_prompt.py`(改造)。测试:`tests/test_sandbox_manager.py`(重写)+ `test_native_backend.py` / `test_srt_backend.py` / `test_sandbox_cleanup.py`(新建)+ 6 个旧测试文件 fixture 迁移。

### 14.2 实施偏差(相对设计文档)
| 偏差 | 说明 |
|---|---|
| 共享清理用模块函数 | 设计 §10 写"DefaultCleanup mixin",实施用 `_cleanup.py` 模块函数(更松耦合,无 MRO 风险),功能等价 |
| SandboxManager 保留 `__new__` 单例 | scope 排除多 session 隔离后,单例形态让测试 fixture 与 production 读模块级 `sandbox_manager` 一致(去掉单例导致 46 个测试 fixture 失效) |
| `cleanup_after_command` 总跑共享清理 | 不委托 backend,而是总调 `run_default_cleanup`(bare-git scrub 对 host 上所有 bash 都有意义),backend.cleanup 只做特定清理(srt config 文件) |
| `is_available` 结果缓存 | NativeBackend/SrtBackend 首次 `shutil.which` 后缓存,避免 1000+ 次 engine 调用反复查 PATH(性能测试 50ms 阈值) |
| Seatbelt profile 简化 | `file-read*` 通配 + `/dev/null` 等 device 例外;`signal*`/`sysctl-read` 在现代 sandbox-exec 报"unbound variable",已去 |
| srt config schema 实测确认 | network 用 `allowedDomains`/`deniedDomains`(**非** allowedHosts),通过 npx 实测确认(原代码假设错,即 127 bug 根因之一) |

### 14.3 测试结果
- sandbox 12 文件:**350 passed, 3 skipped**(Linux bwrap + 2 个 srt 真集成因未装 srt 跳过)
- **macOS Seatbelt 真集成 PASSED**(`test_native_macos_seatbelt`:allow 写 cwd exit 0 / deny 写 outside exit 1)—— 证明 OS 级隔离真的发生,不再只是字符串包装
- conftest.py 加 autouse `_reset_sandbox_singleton` 防跨 test 单例污染
- 全套 non-sandbox failed 均为 pre-existing:`test_fix_c`(依赖 base modified `turn_chain.py` 1891 行用户未提交工作,非本次改)、`TestPerformance`(perf flaky,单独跑绿)、70 errors 全是 `chromadb` 未装

### 14.4 Defer 项(独立任务)
- 多 session 单例隔离(需改 sandbox_decision/bash_permissions/sandbox_prompt/builtin 函数签名,scope 已排除)
- audit_logger 注入断链(permission 层 P0)
- classifier LLM 兜底 stub(permission 层 P1)
- Seatbelt profile 完备性(对齐 CC 上千条规则,持续工程)

### 14.5 用户向:行为变化 + srt 安装

**行为变化(决策 D)**:老 settings.json 缺 `backend` 字段 → 默认 `auto` = **native 优先**(macOS `sandbox-exec` / Linux `bwrap`,无需 srt)。与原硬编码 srt-via-npx 不同(那个还因 CLI 接口错一直 exit 127)。想对齐 CC 同款语义,装 srt + 配 `"backendPriority": ["srt", "native"]`。

**最小配置**(原生 backend,零外部依赖):
```json
{ "sandbox": { "enabled": true, "backend": "auto" } }
```

**srt 可选安装**(仅当想用 SrtBackend,如 Linux 无 bwrap 或想对齐 CC):
```bash
npm i -g @anthropic-ai/sandbox-runtime   # 或 GitHub Releases 下预编译二进制(1MB)
which srt                                  # 验证(/usr/local/bin/srt 或类似)
```
装后 `SrtBackend.is_available()=True`,auto 模式按 `backendPriority` 选用。

**显式锁定 backend**(CI / 合规场景):
```json
{ "sandbox": { "enabled": true, "backend": "native", "failIfUnavailable": true } }
```
