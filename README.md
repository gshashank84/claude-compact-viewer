# claude-compact-viewer

A tiny, **read-only** local web viewer for [Claude Code](https://claude.com/claude-code) session **compact summaries**.

When you run `/compact` (or Claude Code auto-compacts on running out of context), the conversation is summarized and the result is written into your session transcript — but the CLI gives you no easy way to *read back* those summaries later. So when you're deciding **"should I resume this session for my next task, or start fresh?"** you're flying blind.

This tool answers that question. It scans your local `~/.claude/projects/` transcripts and shows, per session, exactly what a resume would put back into context.

- 🔒 **Read-only & local.** Reads `~/.claude/projects/*/*.jsonl` only. Never writes to, uploads, or mutates your transcripts. No network calls.
- 🪶 **Zero dependencies.** Pure Python stdlib + a single self-contained HTML page. No build step, no `npm`, no framework.
- ⚡ **One file.** `compact_viewer.py` is the whole thing.

## Install

```bash
pipx install claude-compact-viewer    # recommended
# or
pip install claude-compact-viewer
```

Or just run it from a clone — there are no dependencies:

```bash
git clone https://github.com/gshashank84/claude-compact-viewer
python3 claude-compact-viewer/compact_viewer.py
```

## Usage

```bash
compact-viewer                       # if installed
python3 compact_viewer.py            # from source
```

It starts a local server and opens your browser at <http://localhost:8765>.

```bash
COMPACT_VIEWER_PORT=9000 compact-viewer   # use a different port
```

## What you get

**Session list (left):** every session across all your projects, with filter (title / branch / prompt), sort (recent / most-compacted / largest), and a "compacted only" toggle. Each row shows compaction count, current context size (color-coded), git branch, message count, file size, and age.

**Session detail (right):**
- The **latest compact summary** rendered as readable sections (Primary Request, Key Technical Concepts, Pending tasks, Current work, …) — not a raw text wall. A **raw** toggle flips back to verbatim text.
- A **section-jump** bar that scrolls to any heading in the latest summary.
- The **full compaction history** for that session, each showing `trigger` (manual/auto), the `preTokens → postTokens` compression, duration, and how many recent messages were preserved verbatim. **Expand all / collapse all** for multi-compaction sessions.
- A copy-able `claude --resume <session-id>` command.

### Keyboard shortcuts

| Key     | Action                                  |
| ------- | --------------------------------------- |
| `j`/`k` | Move down / up the session list         |
| `/`     | Focus the search box                    |
| `Esc`   | Blur the search box                     |

## How it works

Claude Code stores each session as a JSON-Lines transcript at
`~/.claude/projects/<project-slug>/<session-id>.jsonl`, appended to live. Each
`/compact` event writes two linked entries:

1. a `type: "user"` entry with `isCompactSummary: true` whose `message.content`
   holds the full summary text, and
2. a `type: "system"` entry with `compactMetadata`
   (`trigger`, `preTokens`, `postTokens`, `durationMs`, and which messages were
   preserved verbatim).

A session can be compacted many times; the **last** summary is what an actual
resume would put back into context. This viewer parses those entries (caching by
file mtime) and presents them. That's all it does — it never modifies anything.

> Transcript format is an internal Claude Code detail and may change between
> releases; the parser degrades gracefully (falls back to raw text) if a summary
> doesn't match the expected shape.

## License

[MIT](LICENSE) © Shashank Gupta

---

*Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
