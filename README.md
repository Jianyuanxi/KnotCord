# KnotCord（结绳）

给 Claude Code 用的本地记忆插件。名字取自"结绳记事"：一个结记一件事。

它记的是"发生过什么"——做过什么、改了什么、为什么这么定。新会话开头自动注入本项目的近况，需要时可以按关键词搜旧事。

自用项目，只在 Windows + Python 3.13 上跑过。

## 和同类的区别

- **记录不调模型**：hook 只把操作存进本地 SQLite，攒够一批才交给模型提炼一次。一天的模型调用有次数和花费上限。
- **只记有用的**：读文件、搜索、截图这类只读操作不入库。
- **不留常驻进程**：每个会话一个 stdio MCP server 兼后台线程，会话退出它就退；会话关闭时拉起一个一次性收尾进程，做完即退。没有端口、没有网页、没有云端。
- **数据全在本地**：`~/.knotcord/`，密钥和 token 入库前先脱敏。
- **旧事实会过时**：新事实和旧的冲突时，旧的标成"已被取代"，默认搜不到。

## 结构

```
knotcord/
  core/      数据库、配置、租约、子进程、脱敏、日志（只用标准库）
  ingest/    增量读会话 transcript
  hooks/     hook 入口（SessionStart / UserPromptSubmit / SessionEnd）
  inject.py  开头注入的文本
  recall.py  提问前自动召回（默认关，recall.enabled）
  consolidate.py 每周整理：保守去重、降权评分、晋升提名
  extract/   批量提炼：codex exec → DeepSeek 兜底，预算和熔断
  search/    FTS5 检索（汉字二元组 + 英文整词）+ 本地向量检索（RRF 混合），内置记忆文件索引
  migrate/   从 claude-mem 导入
  server/    MCP server + worker 线程
  drain.py   会话关闭后的收尾进程
  janitor.py 临时文件、日志、备份的清理
  cli.py     usage / doctor / migrate / reindex / install
  codex_setup.py  把 KnotCord 接进 Codex（install --codex）：改 ~/.codex 的 config.toml 和 hooks.json
hooks/hooks.json  mcp.json  .claude-plugin/  skills/
```

## 安装

`hooks/hooks.json` 和 `mcp.json` 里的解释器要写绝对路径（hook 不依赖 PATH）。克隆后先按自己的环境重写：

```
python -I -X utf8 knotcord\boot.py install --apply --python <你的 python.exe 绝对路径>
```

然后把仓库目录作为本地插件加进 Claude Code。依赖：`mcp`（必需）；`numpy`、`onnxruntime`、`tokenizers` 给"按意思搜"用，缺任何一个或没下载模型，向量一路就静默关闭，关键词检索照常。

按意思搜（可选）：

```
python -I -X utf8 knotcord\boot.py fetch-model
```

下载约 24 MB 的 bge-small-zh-v1.5 int8 ONNX 模型到 `~/.knotcord/models/`（先试 huggingface.co，不通换 hf-mirror.com；大小和 sha256 都核对，重复运行不会重下）。`tokenizers` 可装进 `~/.knotcord/venv/Lib/site-packages`（`boot.py` 会把它和用户 site 都加进 `-I` 模式的路径）。之后持有租约的 worker 会在后台给文档补向量，每轮限量、可中断；`doctor` 的"缺向量 N 条"就是还没补的数量。

### 接入 Codex（可选）

Codex CLI 和 Codex 桌面版共用 `~/.codex`。接入后两个客户端写同一个库：同一个文件夹在两边是同一个项目名（取 `.git` 所在目录名，没有 `.git` 取当前目录名；路径的大小写、`\\?\` 前缀、`/mnt/c/` 写法先理平），各自能搜到对方的记录，`sessions.client` 区分来源（`claude` / `codex`）。

```
python -I -X utf8 knotcord\boot.py install --codex --python <你的 python.exe 绝对路径>            # 预演：只打印要改什么，不写文件
python -I -X utf8 knotcord\boot.py install --codex --apply --python <你的 python.exe 绝对路径>   # 真的写
python -I -X utf8 knotcord\boot.py install --codex --remove --apply                              # 撤销
```

它只改 `~/.codex`（或 `CODEX_HOME`）下两个文件里 KnotCord 自己的部分，别的内容和注释原样保留，改之前先把原文件备份成同目录的 `config.toml.bak-knotcord`、`hooks.json.bak-knotcord`（只留最新一份；内容没变不重备份）：

- `config.toml`：末尾追加一段带 `# >>> knotcord` / `# <<< knotcord <<<` 标记的 `[mcp_servers.knotcord]`，Codex 里的 agent 能用 `search`、`timeline`、`get`（`ingest` 是给 Claude Code 的 hook 用的，已用 `disabled_tools` 隐藏）。重复运行不会产生第二段；已有手工登记的同名段会拒绝并提示先删掉。
- `hooks.json`：并入 `SessionStart`（开局注入近况，`startup|clear|compact` 触发，`resume` 不注入）和 `SessionEnd`（会话结束时登记、拉起收尾进程）各一条。hook 用的是 `knotcord/hooks/entry.py ... --codex`，输出是纯 ASCII 的 JSON（Windows 里 Codex 经 PowerShell 转发 hook 输出，中文会被按 GBK 重编码，转义后不受影响）。

装完在 Codex 里输入 `/hooks`，把两条 knotcord 的 hook 点“信任”——Codex 要求用户审核后 hook 才会运行，信任记录绑定 hook 内容的哈希，改过命令要重新点一次，这一步程序不代做。会话记录的采集不依赖 hook：MCP server 里的 worker 每 60 秒扫一次 `~/.codex/sessions`，hook 只管开局注入和结束收尾。

提炼通道：默认 `codex exec`，不可用时走 DeepSeek，密钥从环境变量 `DEEPSEEK_API_KEY` 读，没有再读 `~/.knotcord/config.toml` 的 `extract.deepseek_api_key`。两个都没有时只记录不提炼。

## 项目地图

项目之间的关系（同一套产品的电脑端和手机端、作业属于哪门课）只有用户自己知道，所以 KnotCord 不猜，只读用户手写的地图：`~/CLAUDE.md`（`projmap.map_file` 可改）里"名字 / 路径 / 是什么 / 关联"的表格，加各项目目录下 `CLAUDE.local.md`（没有就看 `CLAUDE.md`）的"## 相关项目"一节。声明过的关系用来给检索结果略微加分；Codex 的开局注入会列出相关项目（Claude Code 本来就读项目说明文件，不重复）。地图和说明文件都只读，改过之后清理任务会自动重建；`boot.py projmap` 立即重建，并另列"库里有记录但地图里没有的项目"和"跨项目改过文件的候选"供用户决定要不要补进地图。

## 命令

```
python -I -X utf8 knotcord\boot.py usage     今天提炼了几次、花了多少
python -I -X utf8 knotcord\boot.py doctor    文件、进程、租约、积压体检
python -I -X utf8 knotcord\boot.py migrate   从 ~/.claude-mem 导入（只读打开源库）
python -I -X utf8 knotcord\boot.py reindex   重建内置记忆文件索引
python -I -X utf8 knotcord\boot.py fetch-model   下载向量检索用的嵌入模型（幂等）
python -I -X utf8 knotcord\boot.py rename-project 旧名 新名   项目文件夹改名后合并旧记录
python -I -X utf8 knotcord\boot.py projmap   立即重建项目地图并打印关系和候选
```

## 测试

```
python -m pytest tests -q
```

测试只写临时目录（环境变量 `KNOTCORD_HOME`），不碰真实数据。

## 状态

第一版。关键词检索、迁移、采集、提炼、注入已有测试覆盖；按意思搜（本地 bge-small-zh-v1.5 int8，向量放内存里直接做点积，和关键词结果用 RRF 合并）已做，模型文件和依赖缺失时自动退回纯关键词；Codex 客户端已接通：会话记录能采集（扫 `~/.codex/sessions`），`install --codex` 能一步登记 MCP server 和 hook，已在本机用 `codex mcp list` 确认登记成功、手工拉起 server 完成握手和 `tools/list`；Codex 会话里的 hook 注入、会话结束收尾、MCP 工具调用还没在真实 Codex 会话里跑过（Codex 额度恢复后验证）。
