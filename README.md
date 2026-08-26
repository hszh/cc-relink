<div align="center">
  <img src="assets/cc-relink-icon.svg" width="90" alt="cc-relink 图标">
  <h1>cc-relink</h1>
  <p>找回重启编辑器或 resume 会话后「不见了」的 Claude Code 回答。</p>
  <p><a href="README_EN.md">English</a></p>
</div>

记录其实没丢，都还在文件里，只是 Claude Code 没有正确渲染出来。这种情况会在某轮请求发生网络重试时出现，坏掉的其实是 Claude Code 用来重建对话的 `parentUuid` 链，`cc-relink` 的作用是把它接回去。不改变对话内容和顺序，只是让其在 CLI 和 VSCode 插件中正常显示。

## 1. 问题

重开 VSCode、resume 会话，发现有些助手回答没了。不是整段对话消失，是**特定几轮**的回答不见了，
你自己发的消息一条不少。

Claude Code 把每个会话存成 `~/.claude/projects/<项目>/<会话id>.jsonl`，条目之间用
`uuid` / `parentUuid` 串成链表。加载时从最后一条往回走这条链，只渲染走到的节点，
落在旁支上的一律不显示。

### 1.1 成因

某轮请求发生网络重试时，Claude Code 会记一条 `system` / `api_error`，它的 `parentUuid` 是
**发请求那一刻**的父节点 —— 但这条记录真正被 append 到文件里，是在这一轮回答**已经写完之后**。
于是写入器的「当前父节点」指针回退，下一条用户消息挂到了 `api_error` 上，把整段回答甩成旁支：

```
用户消息
  └─ attachment ─┬─ assistant … ─→ 最终回答        ← 成了旁支，不渲染
                 └─ api_error  ─→ 下一条用户消息   ← resume 实际走的链
```

最直接的证据是时间戳倒挂 —— 某个真实会话里 `api_error` 的时间戳是 `07:40:30`，
但它在文件里的位置排在 `07:52:49` 那条回答**之后**。

只有发生过重试的那几轮会丢回答，其余完好，所以丢失看起来毫无规律。

同类链断裂的上游 issue：
[#22526](https://github.com/anthropics/claude-code/issues/22526)、
[#24304](https://github.com/anthropics/claude-code/issues/24304)、
[#21751](https://github.com/anthropics/claude-code/issues/21751)。

### 1.2 修复后

`cc-relink` 把 `api_error` 的父指针改到被甩掉那条分支的末节点，两条链接回一条：

```
用户消息
  └─ attachment ─→ assistant … ─→ 最终回答 ─→ api_error ─→ 下一条用户消息
```

## 2. 改动范围

只改 `parentUuid` 字段，且只改 `system` / `api_error` 条目。不增删任何行，不碰消息内容、
thinking 块、签名，也不碰 `file-history-snapshot` 记录。

## 3. 安装

```bash
uv tool install git+https://github.com/hszh/cc-relink
# 或
pipx install git+https://github.com/hszh/cc-relink
```

也可以直接跑单文件 —— 无依赖，Python ≥ 3.9：

```bash
python3 cc_relink.py scan
```

## 4. 用法

```bash
# 只读扫描全机所有会话
cc-relink scan
cc-relink scan -p myproject           # 只看项目目录名含 myproject 的

# 预演修复（默认 dry-run，不写盘）
cc-relink fix 1f711467                # 会话 id 给前缀就够
cc-relink fix 1f711467 aae730d1       # 一次多个
cc-relink fix --all
cc-relink fix --all -v                # -v 顺带打印被恢复的回答

# 真正写盘
cc-relink fix --all --apply

# 只读：把不可见的回答导出成 markdown，不动原文件
cc-relink export 1f711467 -o ./recover
```

扫描输出示例：

```
扫描 45 个会话…

  1af3c40d  丢失回答   1 条 | 可修复   1 | api_error   6 | -home-me-myproject
  aae730d1  丢失回答  21 条 | 可修复  21 | api_error  21 | -home-me-myproject
  ec369410  丢失回答   7 条 | 可修复   7 | api_error  18 | -home-me-myproject

共 3 个会话受影响。
```

## 5. 安全设计

- **默认 dry-run**：`fix` 只打印计划，不加 `--apply` 不写盘。
- **每次写盘自动备份**到 `~/.claude/transcript-backups/<时间戳>/`，放在 `projects/` 之外，
  免得备份被 Claude Code 当成会话扫进列表。
- **字节级最小改动**：只重新序列化被改的那几行（用 Claude Code 自己的紧凑格式），
  其余行原样保留。经临时文件 + 原子替换写入。
- **安全闸**：先在内存里试修并重走父链，只有「不可见回答数严格下降」才落盘。
- **幂等**：健康文件零改动；对已修复的文件重跑会报 `无需修复`。

## 6. 注意

> [!IMPORTANT]
> 修复前请先关闭目标会话。客户端内存里还持有旧父链，可能将它覆盖回去。

修复后需要重启 VSCode 或重新 resume 才能看到效果。

触发条件是重试，所以网络或代理不稳会大幅提高中招概率。根因在客户端，
稳住网络是唯一能自己做的预防。

## 7. License

MIT
