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
  hooks/     hook 入口（SessionStart / SessionEnd）
  inject.py  开头注入的文本
  extract/   批量提炼：codex exec → DeepSeek 兜底，预算和熔断
  search/    FTS5 检索（汉字二元组 + 英文整词），内置记忆文件索引
  migrate/   从 claude-mem 导入
  server/    MCP server + worker 线程
  drain.py   会话关闭后的收尾进程
  janitor.py 临时文件、日志、备份的清理
  cli.py     usage / doctor / migrate / reindex / install
hooks/hooks.json  mcp.json  .claude-plugin/  skills/
```

## 安装

`hooks/hooks.json` 和 `mcp.json` 里的解释器要写绝对路径（hook 不依赖 PATH）。克隆后先按自己的环境重写：

```
python -I -X utf8 knotcord\boot.py install --apply --python <你的 python.exe 绝对路径>
```

然后把仓库目录作为本地插件加进 Claude Code。依赖：`mcp`（必需）；`numpy`、`onnxruntime`、`tokenizers` 留给向量检索，目前还没用到。

提炼通道：默认 `codex exec`，不可用时走 DeepSeek，密钥从环境变量 `DEEPSEEK_API_KEY` 读。两个都没有时只记录不提炼。

## 命令

```
python -I -X utf8 knotcord\boot.py usage     今天提炼了几次、花了多少
python -I -X utf8 knotcord\boot.py doctor    文件、进程、租约、积压体检
python -I -X utf8 knotcord\boot.py migrate   从 ~/.claude-mem 导入（只读打开源库）
python -I -X utf8 knotcord\boot.py reindex   重建内置记忆文件索引
```

## 测试

```
python -m pytest tests -q
```

测试只写临时目录（环境变量 `KNOTCORD_HOME`），不碰真实数据。

## 状态

第一版。关键词检索、迁移、采集、提炼、注入已有测试覆盖；向量检索和 Codex CLI 客户端支持还没做。
