#!/usr/bin/env python3
"""
修复 Claude Code 会话记录（.jsonl）因 API 重试导致的父链断裂。

背景
----
当一轮请求发生网络重试时，`system/api_error` 记录带的 parentUuid 是「发请求那一刻」
的父节点，但它真正被 append 到文件里是在这一轮回答写完之后。于是写入器的
"当前父节点"指针回退，下一条用户消息挂到了 api_error 上，把中间整段助手回答
甩成了旁支。Claude Code 重新加载会话时只沿 parentUuid 单链回溯，旁支不渲染，
表现为「重启后有些回答不见了」。

本脚本把这些 api_error 的 parentUuid 改指到该轮助手回答的末节点，接回单链。
只改 parentUuid 字段，不增删任何行，不改任何消息内容。

用法
----
  # 扫描（只读），列出所有损坏的会话
  cc-relink scan
  cc-relink scan -p Flux2          # 只看项目名含 Flux2 的

  # 预演修复（默认 dry-run，不写盘）
  cc-relink fix --all
  cc-relink fix 1f711467
  cc-relink fix 1f711467 aae730d1
  cc-relink fix aae730d1 -v        # -v 打印被恢复回答的开头

  # 真正写盘（自动备份到 ~/.claude/transcript-backups/<时间戳>/）
  cc-relink fix --all --apply

  # 把不可见的回答导出成 markdown（只读，不改原文件）
  cc-relink export 1f711467 -o /tmp/recover

注意
----
修复后需重启 VSCode / 重新 resume 会话才能看到效果。若目标会话正开着，
先关掉再修——否则客户端内存里的旧父链可能覆盖回去。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
BACKUP_ROOT = Path.home() / ".claude" / "transcript-backups"


# ---------------------------------------------------------------- 读写

def load(path: Path):
    """返回 (rows, raw_lines)。无法解析的行 rows 里放 None，原样保留。"""
    raw = path.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in raw:
        if not line.strip():
            rows.append(None)
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append(None)
    return rows, raw


def spine(rows) -> set[str]:
    """从文件里最后一个带 uuid 的条目出发，沿 parentUuid 回溯出的可见单链。"""
    by_id = {r["uuid"]: r for r in rows if r and "uuid" in r}
    ids = [r for r in rows if r and "uuid" in r]
    if not ids:
        return set()
    chain: set[str] = set()
    cur = ids[-1]["uuid"]
    while cur and cur in by_id and cur not in chain:  # cycle-safe
        chain.add(cur)
        cur = by_id[cur].get("parentUuid")
    return chain


def texts(row) -> list[str]:
    content = (row.get("message") or {}).get("content") or []
    if isinstance(content, str):
        return [content]
    return [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]


def orphan_replies(rows, chain: set[str]) -> list[tuple[int, str]]:
    """不在单链上、且含文本的助手消息 —— 也就是重启后会消失的回答。"""
    out = []
    for i, r in enumerate(rows):
        if not r or r.get("type") != "assistant" or r.get("uuid") in chain:
            continue
        for t in texts(r):
            out.append((i, t))
    return out


# ---------------------------------------------------------------- 诊断 / 修复

def plan_repairs(rows) -> list[tuple[int, str, str]]:
    """
    找出需要改父指针的 api_error 条目。
    返回 [(行下标, 旧 parentUuid, 新 parentUuid)]。

    判定：type=system / subtype=api_error，且其 parentUuid 在文件里有多个子节点，
    且它不是最早出现的那个子节点 —— 即它把先前的助手分支给甩掉了。
    新父 = 该 parentUuid 的所有后代中，出现在本条目之前、文件下标最大的那个
    （排除本条目自身的子树），也就是被甩掉那条分支的末节点。
    """
    idx = {r["uuid"]: i for i, r in enumerate(rows) if r and "uuid" in r}
    children: dict[str, list[str]] = {}
    for r in rows:
        if r and "uuid" in r:
            children.setdefault(r.get("parentUuid"), []).append(r["uuid"])

    def subtree(root: str) -> set[str]:
        seen, stack = set(), [root]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(children.get(u, []))
        return seen

    repairs = []
    for i, r in enumerate(rows):
        if not r or r.get("type") != "system" or r.get("subtype") != "api_error":
            continue
        parent = r.get("parentUuid")
        sibs = children.get(parent, [])
        if parent is None or len(sibs) < 2:
            continue
        if min(idx[s] for s in sibs) >= i:  # 它就是最早的子节点，正常
            continue
        mine = subtree(r["uuid"])
        cands = [u for u in subtree(parent) if u not in mine and idx[u] < i and u != parent]
        if not cands:
            continue
        new_parent = max(cands, key=lambda u: idx[u])
        repairs.append((i, parent, new_parent))
    return repairs


def apply_in_memory(rows, repairs) -> None:
    for i, _old, new in repairs:
        rows[i]["parentUuid"] = new


def diagnose(path: Path) -> dict:
    rows, raw = load(path)
    before = spine(rows)
    lost_before = orphan_replies(rows, before)
    repairs = plan_repairs(rows)

    trial = json.loads(json.dumps([r for r in rows]))  # deep copy
    apply_in_memory(trial, repairs)
    after = spine(trial)
    lost_after = orphan_replies(trial, after)

    return {
        "path": path,
        "rows": rows,
        "raw": raw,
        "n_lines": len(raw),
        "repairs": repairs,
        "lost_before": lost_before,
        "lost_after": lost_after,
        "spine_before": len(before),
        "spine_after": len(after),
        "n_api_error": sum(1 for r in rows if r and r.get("subtype") == "api_error"),
    }


def write_back(path: Path, rows, raw, repairs, backup_dir: Path) -> None:
    """只重写被改动的那几行，其余字节原样保留。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)

    lines = list(raw)
    for i, _old, _new in repairs:
        lines[i] = json.dumps(rows[i], ensure_ascii=False, separators=(",", ":"))

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 导出

def export_md(diag: dict, out_dir: Path) -> Path:
    rows = diag["rows"]
    chain = spine(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (diag["path"].stem[:8] + ".md")

    buf = [f"# 会话恢复：{diag['path'].name}\n",
           "> `[孤儿]` 标记的条目是重启后不再渲染的内容。\n"]
    for r in rows:
        if not r or r.get("type") not in ("user", "assistant"):
            continue
        content = (r.get("message") or {}).get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        tx = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        tools = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not tx and not tools:
            continue
        tag = " **[孤儿]**" if r.get("uuid") not in chain else ""
        ts = (r.get("timestamp") or "")[11:19]
        buf.append(f"\n## {'用户' if r['type'] == 'user' else '助手'}  `{ts}`{tag}\n")
        for b in tx:
            buf.append(b["text"].strip() + "\n")
        for b in tools:
            arg = json.dumps(b.get("input", {}), ensure_ascii=False)
            buf.append(f"\n<sub>🔧 {b.get('name')}: `{arg[:160]}`</sub>\n")
    dest.write_text("\n".join(buf), encoding="utf-8")
    return dest


# ---------------------------------------------------------------- CLI

def find_sessions(patterns: list[str], project: str | None) -> list[Path]:
    if not PROJECTS_DIR.is_dir():
        sys.exit(f"找不到 {PROJECTS_DIR}")
    files = [
        f
        for d in sorted(PROJECTS_DIR.iterdir())
        if d.is_dir() and (project is None or project in d.name)
        for f in sorted(d.glob("*.jsonl"))
    ]
    if not patterns:
        return files
    hit, missing = [], []
    for p in patterns:
        m = [f for f in files if f.stem.startswith(p) or p in f.stem]
        if not m:
            missing.append(p)
        hit.extend(m)
    if missing:
        sys.exit(f"匹配不到会话: {', '.join(missing)}")
    return list(dict.fromkeys(hit))


def short(uuid: str | None) -> str:
    return (uuid or "None")[:8]


def cmd_scan(args) -> int:
    files = find_sessions([], args.project)
    print(f"扫描 {len(files)} 个会话…\n")
    bad = 0
    for f in files:
        try:
            d = diagnose(f)
        except Exception as e:  # 坏文件不该中断整批
            print(f"  !! {f.stem[:8]}  解析失败: {e}")
            continue
        if not d["lost_before"]:
            continue
        bad += 1
        fixable = len(d["lost_before"]) - len(d["lost_after"])
        print(
            f"  {f.stem[:8]}  丢失回答 {len(d['lost_before']):3d} 条 | "
            f"可修复 {fixable:3d} | api_error {d['n_api_error']:3d} | "
            f"{f.parent.name}"
        )
    print(f"\n共 {bad} 个会话受影响。" if bad else "\n没有发现损坏的会话。")
    return 0


def cmd_fix(args) -> int:
    pats = [] if args.all else args.session
    if not pats and not args.all:
        sys.exit("请指定会话 id 前缀，或用 --all")
    files = find_sessions(pats, args.project)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / stamp
    touched = 0

    for f in files:
        d = diagnose(f)
        if not d["repairs"]:
            if pats:
                print(f"{f.stem[:8]}  无需修复（父链已完整）")
            continue

        # 安全闸：必须确实减少不可见回答，且不能反而变糟
        gain = len(d["lost_before"]) - len(d["lost_after"])
        if gain <= 0:
            print(f"{f.stem[:8]}  跳过：修复不会改善可见性（before={len(d['lost_before'])} after={len(d['lost_after'])}）")
            continue

        print(f"\n{f.stem}  ({f.parent.name})")
        print(f"  行数 {d['n_lines']}，主链 {d['spine_before']} → {d['spine_after']} 节点")
        for i, old, new in d["repairs"]:
            print(f"  行{i + 1}: api_error {short(d['rows'][i]['uuid'])}  parentUuid {short(old)} → {short(new)}")
        print(f"  恢复可见回答 {gain} 条，仍不可见 {len(d['lost_after'])} 条")
        for _, t in d["lost_before"][:  gain if args.verbose else 0]:
            print(f"    ↳ {t[:70].replace(chr(10), ' ')}")

        if args.apply:
            apply_in_memory(d["rows"], d["repairs"])
            write_back(f, d["rows"], d["raw"], d["repairs"], backup_dir)
            print("  ✅ 已写入")
            touched += 1
        else:
            touched += 1

    if not touched:
        print("\n没有需要修复的会话。")
    elif args.apply:
        print(f"\n完成，改动 {touched} 个会话。备份：{backup_dir}")
        print("重启 VSCode / 重新 resume 会话后生效。若会话正开着，先关掉再修。")
    else:
        print(f"\n[预演] {touched} 个会话可修复，未写盘。加 --apply 执行。")
    return 0


def cmd_export(args) -> int:
    pats = [] if args.all else args.session
    files = find_sessions(pats, args.project)
    out = Path(args.out)
    for f in files:
        d = diagnose(f)
        if not d["lost_before"] and not args.all_sessions:
            continue
        dest = export_md(d, out)
        print(f"{f.stem[:8]}  孤儿回答 {len(d['lost_before'])} 条 → {dest}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="修复 Claude Code 会话记录的父链断裂（API 重试导致回答重启后不显示）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[1].split("注意")[0],
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="只读扫描，列出损坏的会话")
    s.add_argument("-p", "--project", help="只看项目目录名含该字符串的")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("fix", help="修复指定会话或全部（默认 dry-run）")
    s.add_argument("session", nargs="*", help="会话 id 前缀，可给多个")
    s.add_argument("--all", action="store_true", help="修复所有损坏的会话")
    s.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    s.add_argument("-p", "--project", help="限定项目目录")
    s.add_argument("-v", "--verbose", action="store_true", help="打印被恢复回答的开头")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("export", help="把不可见的回答导出成 markdown（只读）")
    s.add_argument("session", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--all-sessions", action="store_true", help="连没损坏的也导出")
    s.add_argument("-o", "--out", default="./claude-recover", help="输出目录")
    s.add_argument("-p", "--project")
    s.set_defaults(func=cmd_export)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
