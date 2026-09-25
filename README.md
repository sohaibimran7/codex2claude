# codex2claude

Imports OpenAI Codex threads into Claude Code as native, resumable sessions. It needs only Python 3.9+ and has no dependencies.

## Usage

```bash
# see what would be imported for a project
python3 codex2claude.py --project ~/my-project --list

# free mode (no model calls): import every top-level thread in a project and list it in the desktop app
python3 codex2claude.py --project ~/my-project --desktop

# claude mode: Claude also writes a summary of each thread (uses your Claude account)
python3 codex2claude.py --project ~/my-project --mode claude --desktop

# one thread, or everything
python3 codex2claude.py <codex-thread-id>
python3 codex2claude.py --all --desktop

# dry run into a folder, leaving ~/.claude untouched
python3 codex2claude.py --project ~/my-project --out /tmp/codex-dry
```

Resume from the project folder with `claude -r <session-id>` (the id is printed in each JSON result line). With `--desktop`, restart the Claude app and the imports appear in the sidebar with a `[Codex]` prefix.

## Modes

Both modes replay the conversation since Codex's last compaction. If that part is longer than `--budget-tokens` (default 80k), it is trimmed to fit. The earlier history is not loaded into context.

- **free** (default): the boundary message contains an index of your prompts with timestamps, plus instructions to Grep and Read the full transcript. Claude looks up earlier details on demand.
- **claude**: the same, plus a summary written by Claude (Sonnet by default, `--model`). It is built as a rolling summary over chunks of about 100k tokens, reading at most the most recent `--summary-input-tokens` (default 300k) of a condensed digest, so no single call needs the whole history.

## What gets converted

- All rollout segments of a thread (Codex's "paginated" history).
- Messages, reasoning summaries (the readable ones; encrypted reasoning cannot be recovered), tool calls and outputs, and images.
- Inter-agent messages. When Codex encrypted the payload, the text is recovered from the subagent's own transcript where possible (about 65% in testing).
- Codex compactions become native `compact_boundary` records. Codex's own summaries are encrypted, so they cannot be reused.
- Forked threads link to their parent's transcript instead of copying the parent's history. Pass `--inherit full` to copy it anyway.
- Codex worktree folders (`~/.codex/worktrees/<id>/<repo>`) are mapped back to the main checkout, and the summary tells Claude where Codex's files actually live.
- Thread titles (`[Codex] <title>`), git branch, and timestamps.
- Injected context (AGENTS.md, environment and permission blocks, developer instructions) is dropped. Claude Code supplies its own.
- Subagent and guardian threads are skipped. Pass `--include-subagents` to import them.

## Comparison with other tools

These are the other tools that move Codex sessions into Claude Code. The table covers only the Codex → Claude direction and is based on reading each tool's source in September 2026. They may have changed since, so check the linked repos for current behaviour.

| | **codex2claude** (this) | [transession](https://github.com/inmzhang/transession) + [MisterBrookT/codex2claude](https://github.com/MisterBrookT/codex2claude) | [session-migrate](https://github.com/xhluca/session-migrate) | [cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer) | [resume-from](https://github.com/alexei-led/resume-from) | [sessionbridge](https://tongtongtju.github.io/sessionbridge/) | [cli-continues](https://github.com/yigitkonur/cli-continues) |
|---|---|---|---|---|---|---|---|
| Creates a native, resumable Claude session | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ only with `--new-session`; by default pastes a summary into the current chat | ❌ Markdown handoff prompt |
| Sessions with millions of tokens fit in context on resume | ✅ boundary placed by token budget | ❌ replays the full history | ❌ replays the full history | ⚠️ resets to Codex's compacted history, but drops the earlier history | ⚠️ silently drops the oldest turns beyond ~60k tokens | ❌ | ⚠️ keeps only the last ~10 messages |
| Earlier history kept and searchable | ✅ full transcript + prompt index | ✅ in the session file | ✅ in the session file | ❌ | ❌ | ❌ | ❌ |
| Optional Claude-written summary | ✅ `--mode claude` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Tool calls and outputs | ✅ | ✅ | ✅ | ✅ | ⚠️ one-line text; outputs dropped | ⚠️ renamed to Bash/Edit; ordering can break | ⚠️ one-line summaries |
| Reasoning summaries | ✅ | ⚠️ only where stored on the reasoning record | ❌ | ⚠️ partial | ❌ | ❌ | ⚠️ first lines only |
| Inter-agent (subagent) messages | ✅ encrypted payloads recovered from subagent transcripts | ❌ | ❌ (reported as dropped) | ⚠️ only if plain text | ✅ text only | ❌ | ⚠️ fallback only |
| Images | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Multi-file ("paginated") threads and forks | ✅ segments merged; forks linked to parent | – | ⚠️ rejects paginated subagent histories | – | – | – | – |
| Codex worktrees mapped back to the real project | ✅ | ❌ | ⚠️ manual `--cwd` override | ❌ | ❌ | ❌ | ❌ |
| Bulk import | ✅ `--project` / `--all` | ✅ (wrapper) | ❌ | ❌ | ❌ | ❌ | ❌ |
| Skips subagent / guardian threads | ✅ | ❌ | ⚠️ flagged in its listing only | ❌ | ❌ | ❌ | ❌ |
| Keeps Codex thread titles | ✅ | ⚠️ titled from the first prompt, not the Codex thread name | – | – | – | – | – |
| Readable HTML chat history | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Claude desktop app sidebar | ✅ `--desktop` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Never overwrites existing files | ✅ (`--force` archives first) | ✅ | ✅ | ✅ (`--force` backs up) | ✅ | ✅ | ⚠️ overwrites its handoff file |
| Runtime | Python 3.9+, no dependencies | Rust + Python | Python 3.11+ | Rust | Node ≥ 24 | Node (runs `npm install` on each call) | Node ≥ 22.5 |

"–" means the reviewed source did not show clearly either way.

Several of those tools also convert in other directions (Claude → Codex, or between many agents), which this tool does not.

## Output

- `~/.claude/projects/<project>/<session>.jsonl` is the Claude Code session. Session ids are deterministic, so re-running skips threads that are already imported. `--force` re-imports and moves the old file to `_archive/`.
- `~/.claude/codex-imports/<project>/<codex-id>.md` is the complete, untruncated transcript that Claude searches.
- `~/.claude/codex-imports/<project>/<codex-id>.html` is a readable chat history: an index of every prompt linking into pages of 40 prompts each (`<codex-id>/page-N.html`). Tool activity is condensed to one line per call.
- Every imported chat starts with a header message linking to that history (and, for forks, to the parent's history and chat). The header sits before a compaction boundary, so it is shown in the app but never sent to the model.
- `~/.claude/codex-imports/manifest.jsonl` has one line per import.

## Limits

- Codex tools (`exec`, `apply_patch`, …) remain in the history under their original names. Claude sees them but uses its own tools.
- A single block over `--max-block-chars` (30k) is truncated in the session. The full text is in the `.md` transcript.
- Token counts are estimates, calibrated against Claude Code's `/context` (about 2 characters per token for tool-heavy content).
- The Codex and Claude Code session formats are undocumented. This tool was tested against Codex 0.155-alpha and Claude Code 2.1.280.
