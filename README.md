<!-- mcp-name: io.github.sanhangkc/sanhang-mcp -->

# sanhang-mcp

三行弦图（MathMagic）的 MCP 服务。把一句自然语言的数学描述，渲染成一段 Manim 动画视频，返回可直接播放的 mp4 链接和可复用的 Manim 代码。

和其他 manim 系 MCP 最大的区别：本机不需要安装 Manim、LaTeX、ffmpeg，也不需要显卡。渲染在托管后端完成，本地只跑一个纯标准库的 Python 进程。

- 输入：一句话，例如「画出 y=sin(x) 在 0 到 2π 上的图像，用一个红点演示它随 x 增大而运动」
- 输出：mp4 直链 + 封面图 + 完整 Manim 代码 + 渲染耗时
- 依赖：Python 3.8 及以上，能访问 sanhangmath.top，仅此两项

## 为什么单独做一个

社区里已有的 manim MCP 大多走「本地装 Manim 然后执行代码」的路线：本地先装一整套 Python、Manim、LaTeX、ffmpeg（几个 GB），再把模型写的代码丢进去跑。这对笔记本不友好，对沙箱环境更难落地。

| | 本地执行型 | sanhang-mcp |
| --- | --- | --- |
| 本地依赖 | Manim + LaTeX + ffmpeg，数 GB | 只要 Python 3.8+ 标准库 |
| 出片形态 | 本地文件路径 | 可直接播放或嵌入的 URL |
| 失败面 | 环境问题、字体缺失、依赖冲突 | 一次 HTTP 调用 |
| 适合场景 | 离线、私有代码 | 教学演示、内容创作、Agent 编排 |

服务端自带提示词工程、版本对齐、渲染重试、多清晰度转码与语义缓存，客户端都不需要操心。

## 快速开始

### 前置条件

只有两件事：

1. 安装 uv（一个命令，之后 uvx 会自动下载并托管 Python，本机不需要预装 Python）：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# 或者 macOS 用 Homebrew
brew install uv
```

2. 能访问 sanhangmath.top。

### 先自检，再接入

终端里跑一次，确认后端通、环境也对：

```bash
uvx sanhang-mcp --check
uvx sanhang-mcp --whoami
```

前者看到 OK https://sanhangmath.top/api -> {"status": "healthy", ...} 就说明通；后者打印你的配额标识（首次运行自动生成，存在 ~/.sanhang-mcp/identifier，不需要手动配置）。

注意这里的终端命令只是自检用。真正使用是把下一节的配置写进客户端，让客户端去拉起这个进程；不需要在终端里常驻运行。

也可以直接拿源码跑，零第三方依赖：

```bash
python3 sanhang_mcp.py --check
python3 sanhang_mcp.py --version
```

## 接入客户端

这个服务在各客户端里统一叫 sanhang-mcp。三种常见配置如下。

WorkBuddy，配置文件 ~/.workbuddy/mcp.json：

```json
{
  "mcpServers": {
    "sanhang-mcp": {
      "command": "uvx",
      "args": ["sanhang-mcp"]
    }
  }
}
```

Claude Desktop，配置文件 claude_desktop_config.json（macOS 在 ~/Library/Application Support/Claude/）：

```json
{
  "mcpServers": {
    "sanhang-mcp": {
      "command": "uvx",
      "args": ["sanhang-mcp"]
    }
  }
}
```

Cursor，配置文件 ~/.cursor/mcp.json，格式同上。DeepSeek harness、Cline、Continue 等只要支持 stdio MCP，写法一致。

没装 uv 的话，把 command 换成 python3，args 换成 sanhang_mcp.py 的绝对路径。Windows 上必须写 python.exe 的绝对路径，相对路径会启动失败。

想固定配额标识（例如一台机器多人共用、或想让用量归到自己名下），加一段 env：

```json
"env": { "SANHANG_USER_IDENTIFIER": "your-name" }
```

改完配置要彻底重启客户端才会加载。WorkBuddy 还需要在连接器页右上角的「自定义连接器」里点一次信任。

## 三个工具

| 工具 | 作用 | 必填 | 主要可选参数 |
| --- | --- | --- | --- |
| sanhang_render | 生成弦图动画，返回视频与代码 | prompt | wait（默认 true 阻塞）、max_wait_seconds（默认 240）、user_identifier、motto、logo_url |
| sanhang_task | 查询、等待或取消渲染任务 | task_id | action（status / wait / cancel）、max_wait_seconds |
| sanhang_gallery | 只读浏览公开作品墙，找参考风格 | 无 | q、tags、category、sort_by、page、page_size |

典型用法有两种。

第一种，直接阻塞等结果，适合大多数客户端：

```json
sanhang_render({ "prompt": "画出 y=x^2 的图像，在顶点标一个红点" })
```

第二种，先提交再轮询，适合有工具调用超时限制的客户端：

```json
sanhang_render({ "prompt": "...", "wait": false })          -> 拿到 task_id
sanhang_task({ "task_id": "...", "action": "wait", "max_wait_seconds": 300 })
```

渲染通常 30 到 90 秒。命中缓存时秒回，同一描述第二次调用基本是 0.1 秒。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| SANHANG_BASE_URL | https://sanhangmath.top/api | 渲染后端地址 |
| SANHANG_USER_IDENTIFIER | 本机自动生成 | 配额与归属标识。不设时首次运行生成 mcp-xxxxxxxx 并落盘复用；多人共用一台机器时才需要手动指定 |
| SANHANG_STATE_DIR | ~/.sanhang-mcp | 本机标识的存放目录，容器或只读环境可改到可写路径 |
| SANHANG_WAIT | 240 | 默认阻塞等待上限（秒） |
| SANHANG_POLL_INTERVAL | 3 | 轮询间隔（秒） |
| SANHANG_HTTP_TIMEOUT | 30 | 单次 HTTP 超时（秒） |
| SANHANG_LOG | 开 | 设为 0 关闭 stderr 日志 |

旧版 MANIM_MCP_ 前缀仍然可用，两个前缀同时出现时 SANHANG_ 优先。

## 自测

仓库自带一个最小 MCP 客户端，不需要任何外部依赖，用来验证握手、工具发现、错误码和输出纯净性：

```bash
python3 test_client.py                         # 20 项协议断言，约 2 秒
python3 test_client.py --generate "画个抛物线"   # 真实渲染一次
python3 test_client.py --gallery               # 只读工具，不花钱
```

## 工作方式

```
MCP 客户端 --stdio JSON-RPC--> sanhang-mcp 进程 --HTTPS--> sanhangmath.top/api
                                                           |- LLM 出码
                                                        |- Manim + LaTeX 渲染
                                                           |- ffmpeg 转码
```

协议版本 2025-06-18，兼容 2025-03-26 与 2024-11-05。stdio 传输，一行一条 JSON-RPC，stdout 只写协议消息，日志全部走 stderr。每个请求各开线程处理，长渲染期间 ping 仍然能回；带 progressToken 时会回传 notifications/progress；收到 notifications/cancelled 会中断轮询并请求后端停止渲染。

## 边界

- 渲染能力受后端限制，复杂三维场景、超长动画可能超时，建议把描述拆小。
- 后端有配额：同一标识每天 50 次、终身 1000 次，另有每分钟 5 次的突发限制。超限返回 429，不是服务故障。
- 高清档位、视频下载、代码免水印复制属于站点会员能力，免费调用返回 480p15 在线观看版本。
- 视频链接是公网直链，任何拿到链接的人都能播放。

## 目录结构

```
.
├── sanhang_mcp.py     # 服务端，零第三方依赖
├── test_client.py     # 最小 MCP 客户端与协议自测
├── pyproject.toml     # 打包元数据，py-modules 入口
├── README.md
└── LICENSE
```

## License

MIT
