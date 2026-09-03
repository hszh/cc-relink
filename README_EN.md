<div align="center">
  <img src="assets/cc-relink-icon.svg" width="90" alt="cc-relink icon">
  <h1>cc-relink</h1>
  <p>Recover Claude Code replies that vanish after you restart the editor or resume a session.</p>
  <p><a href="README.md">中文文档</a></p>
</div>

Nothing was actually lost — the messages are all still in the transcript, Claude Code just
fails to render them. It happens when a request hits a network retry: what breaks is the
`parentUuid` chain Claude Code uses to rebuild the conversation, and `cc-relink` links it back
up. Message content and ordering are left untouched — the replies simply show up again in the
CLI and the VSCode extension.

## 1. The problem

You reopen VSCode, resume a session, and some of the assistant's answers are simply gone.
Not the whole conversation — just certain turns. Your own prompts are all there.

Claude Code stores each session as a JSONL file under `~/.claude/projects/<project>/<session-id>.jsonl`,
where entries form a linked list via `uuid` / `parentUuid`. On load it walks that chain
backward from the last entry and renders only what it finds. Anything on a side branch
is invisible.

### 1.1 Root cause

When a request hits a network retry, Claude Code logs a `system` / `api_error` entry whose
`parentUuid` is the parent **as of the moment the request was sent** — but that entry is only
appended to the file *after* the turn's reply has finished streaming. The writer's
"current parent" pointer regresses, so the next user prompt attaches to the `api_error`
entry instead of to the assistant's reply, orphaning the whole reply branch:

```
user prompt
  └─ attachment ─┬─ assistant … ─→ final reply       ← orphaned, never rendered
                 └─ api_error  ─→ next user prompt   ← the chain resume actually follows
```

The tell-tale sign is a timestamp inversion — in one real session the `api_error` entry is
stamped `07:40:30` but sits in the file *after* the reply stamped `07:52:49`.

Only turns that experienced a retry lose their reply. Everything else is intact, which is
why the loss looks arbitrary.

Upstream reports of the same chain-corruption class:
[#22526](https://github.com/anthropics/claude-code/issues/22526),
[#24304](https://github.com/anthropics/claude-code/issues/24304),
[#21751](https://github.com/anthropics/claude-code/issues/21751).

### 1.2 After

`cc-relink` re-points the `api_error` entry to the tail of the orphaned branch, splicing it
back into a single chain:

```
user prompt
  └─ attachment ─→ assistant … ─→ final reply ─→ api_error ─→ next user prompt
```

## 2. What it changes

Only the `parentUuid` field, and only on `system` / `api_error` entries. It never adds or
removes a line, and never touches message content, thinking blocks, signatures, or
`file-history-snapshot` records.

## 3. Install

```bash
uv tool install git+https://github.com/hszh/cc-relink
# or
pipx install git+https://github.com/hszh/cc-relink
```

Or just run the single file — no dependencies, Python ≥ 3.9:

```bash
python3 cc_relink.py scan
```

## 4. Usage

```bash
# Read-only scan of every session on the machine
cc-relink scan
cc-relink scan -p myproject          # only projects whose dir name contains "myproject"

# Preview a fix (dry-run is the default — nothing is written)
cc-relink fix 1f711467               # session id prefix is enough
cc-relink fix 1f711467 aae730d1      # several at once
cc-relink fix --all
cc-relink fix --all -v               # -v also prints the recovered replies

# Actually write
cc-relink fix --all --apply

# Read-only: dump the invisible replies to markdown without touching the transcript
cc-relink export 1f711467 -o ./recover
```

Example scan output:

```
扫描 45 个会话…

  1af3c40d  丢失回答   1 条 | 可修复   1 | api_error   6 | -home-me-myproject
  aae730d1  丢失回答  21 条 | 可修复  21 | api_error  21 | -home-me-myproject
  ec369410  丢失回答   7 条 | 可修复   7 | api_error  18 | -home-me-myproject

共 3 个会话受影响。
```

## 5. Safety

- **Dry-run by default.** `fix` prints its plan and writes nothing unless you pass `--apply`.
- **Backup on every write** to `~/.claude/transcript-backups/<timestamp>/`, outside the
  `projects/` tree so Claude Code never sees the copies as sessions.
- **Byte-minimal rewrite.** Only the modified lines are re-serialized (in Claude Code's own
  compact format); every other line is preserved verbatim. Written via a temp file and an
  atomic replace.
- **Guarded.** Each file is repaired in memory first and the chain re-walked; the result is
  written only if the number of invisible replies strictly decreased.
- **Idempotent.** A healthy transcript gets zero edits. Re-running on a repaired file reports
  `无需修复`.

## 6. Notes

> [!IMPORTANT]
> Close the target session before repairing it. A client that still holds the old chain in
> memory may write it back.

Restart VSCode or resume the session to see the effect.

Retries are the trigger, so an unstable network or proxy makes this much more likely.
Stabilizing the connection is the only real prevention; the bug itself is client-side.

## 7. Links

Feel free to visit [LINUX DO](https://linux.do) to browse the community's latest topics and discussions.
