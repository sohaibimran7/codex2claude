#!/usr/bin/env python3
"""Import OpenAI Codex threads into Claude Code as native, resumable sessions.

For each Codex thread this writes:
  * ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl  - a native Claude Code session
  * ~/.claude/codex-imports/<encoded-cwd>/<codex-id>.md - the full, untruncated transcript

Design:
  * The whole Codex history (all rollout segments, plus history inherited from the
    thread it was forked from) is converted, so the Claude transcript is complete.
  * Codex compaction points become Claude `compact_boundary` records. Codex's own
    summaries are encrypted, so summaries are rebuilt from the readable history.
  * On resume, Claude replays the conversation since Codex's last compaction
    (trimmed to --budget-tokens if that is still too long). The last boundary
    carries, depending on --mode:
      free    no model calls: an index of the user's prompts plus instructions to
              search the full .md transcript with Grep/Read (retrieval on demand).
      claude  the same, plus a summary Claude writes via a rolling, chunked pass
              that never needs the whole history in context.
  * Forked threads link to their parent's transcript instead of copying it.

Nothing existing is overwritten: a session that already exists is skipped unless
--force, in which case the old file is moved to an _archive/ folder first.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
NAMESPACE = uuid.UUID("6f1d3c2a-9b7e-4c55-8a1e-c0de2c1a0de0")  # stable ids => idempotent re-imports

# Context Codex injects as "user" messages; Claude Code has its own equivalents.
INJECTED_USER_PREFIXES = (
    "<environment_context", "<codex_internal_context", "# AGENTS.md instructions",
    "<recommended_plugins", "<in-app-browser-context", "<user_instructions",
    "<INSTRUCTIONS>", "<permissions", "<skills_instructions", "<apps_instructions",
)
# Calibrated against Claude Code's /context on an imported Codex thread: tool output is
# escaped JSON/code and tokenizes at ~2.1 chars per token. Round down to stay safe.
CHARS_PER_TOKEN = 2.0
IMAGE_TOKENS = 3000  # fallback when dimensions are unknown


def image_tokens(src: dict) -> int:
    """API cost of an image: (w*h)/750 after scaling the long edge to <=1568px."""
    if src.get("type") != "base64":
        return IMAGE_TOKENS
    try:
        head = base64.b64decode(src["data"][:64] + "=" * (-len(src["data"][:64]) % 4))
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return IMAGE_TOKENS
        w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        scale = min(1.0, 1568 / max(w, h, 1))
        return max(1, int(w * scale * h * scale / 750))
    except Exception:
        return IMAGE_TOKENS


# --------------------------------------------------------------------------- Codex side

def load_db() -> dict[str, dict]:
    path = CODEX_HOME / "state_5.sqlite"
    if not path.exists():
        cands = sorted(CODEX_HOME.glob("state_*.sqlite"))
        if not cands:
            return {}
        path = cands[-1]
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in db.execute("select * from threads")}
    try:
        for r in db.execute("select parent_thread_id, child_thread_id from thread_spawn_edges"):
            rows.setdefault(r[0], {}).setdefault("_children", []).append(r[1])
    except sqlite3.Error:
        pass
    return rows


_FILE_INDEX: dict[str, list[Path]] | None = None


def rollout_files(thread_id: str) -> list[Path]:
    """All rollout segments belonging to a thread, oldest first."""
    global _FILE_INDEX
    if _FILE_INDEX is None:
        _FILE_INDEX = {}
        pat = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
        for root in (CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"):
            for p in root.rglob("rollout-*.jsonl"):
                m = pat.search(p.name)
                if m:
                    _FILE_INDEX.setdefault(m.group(1), []).append(p)
    files = []
    for p in _FILE_INDEX.get(thread_id, []):
        meta = first_meta(p)
        if meta and meta.get("id") == thread_id:
            files.append((meta.get("timestamp", ""), str(p), p))
    return [p for _, _, p in sorted(files)]


def first_meta(path: Path) -> dict | None:
    try:
        with open(path) as fh:
            rec = json.loads(fh.readline())
        return rec.get("payload") if rec.get("type") == "session_meta" else None
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(path: Path):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def is_subagent(meta: dict, row: dict | None) -> bool:
    if isinstance(meta.get("source"), dict) or meta.get("parent_thread_id"):
        return True
    if row and (row.get("agent_path") or "subagent" in str(row.get("source") or "")):
        return True
    return False


def load_thread_records(thread_id: str, upto: int | None = None, depth: int = 0) -> tuple[list[dict], dict]:
    """Raw records of a thread in order, including history inherited through forks."""
    files = rollout_files(thread_id)
    if not files:
        return [], {}
    meta = first_meta(files[0]) or {}
    records: list[dict] = []
    parent = meta.get("forked_from_id")
    if parent and not meta.get("parent_thread_id") and depth < 25:
        inherited, _ = load_thread_records(parent, meta.get("forked_from_ordinal_exclusive"), depth + 1)
        for r in inherited:
            r["_inherited"] = True
        records.extend(inherited)
    for f in files:
        for r in read_jsonl(f):
            if upto is not None and isinstance(r.get("ordinal"), int) and r["ordinal"] >= upto:
                continue
            r["_thread"] = thread_id
            records.append(r)
    return records, meta


# --------------------------------------------------------------------------- normalized items

@dataclass
class Item:
    kind: str  # user | text | reasoning | call | result | agent | note | compaction | turn
    ts: str
    data: dict = field(default_factory=dict)
    inherited: bool = False
    thread: str = ""


def to_blocks(content) -> list[dict]:
    """Codex content (str or list of parts) -> Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content if isinstance(content, list) else [content]:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        t = part.get("type")
        if t in ("input_text", "output_text", "text", "summary_text", "Text"):
            if (part.get("text") or "").strip():
                blocks.append({"type": "text", "text": part["text"]})
        elif t in ("input_image", "output_image", "image"):
            url = part.get("image_url") or part.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                mt = head[5:] or "image/png"
                if mt in ("image/png", "image/jpeg", "image/gif", "image/webp") and len(data) * 3 // 4 <= 5_000_000:
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
                    continue
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                blocks.append({"type": "text", "text": f"[image: {str(url)[:200]}]"})
        elif t == "encrypted_content":
            blocks.append({"type": "text", "text": "[encrypted by Codex - not recoverable]"})
        else:
            blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False)[:4000]})
    return blocks


def blocks_text(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if b["type"] == "text":
            out.append(b["text"])
        elif b["type"] == "image":
            out.append("[image]")
        elif b["type"] == "tool_result":
            out.append(blocks_text(b["content"]) if isinstance(b["content"], list) else str(b["content"]))
    return "\n".join(out)


def safe_tool_name(name: str | None, namespace: str | None = None) -> str:
    n = f"{namespace}__{name}" if namespace and name else (name or "tool")
    return re.sub(r"[^a-zA-Z0-9_-]", "_", n)[:64]


def safe_id(call_id: str | None) -> str:
    c = re.sub(r"[^a-zA-Z0-9_-]", "_", call_id or "")
    return c or "call_" + uuid.uuid4().hex[:16]


class SubagentResolver:
    """Recovers readable text for inter-agent messages Codex encrypted in the parent."""

    def __init__(self, db: dict, thread_id: str):
        self.by_path: dict[str, str] = {}
        stack = [thread_id]
        while stack:  # all descendants, keyed by agent_path
            tid = stack.pop()
            for c in db.get(tid, {}).get("_children", []):
                row = db.get(c, {})
                if row.get("agent_path"):
                    self.by_path.setdefault(row["agent_path"], c)
                stack.append(c)
        self.cache: dict[str, list[tuple[str, str]]] = {}

    def answers(self, tid: str) -> list[tuple[str, str]]:
        if tid not in self.cache:
            out = []
            for f in rollout_files(tid):
                for r in read_jsonl(f):
                    p = r.get("payload") or {}
                    if r.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
                        txt = blocks_text(to_blocks(p.get("content")))
                        if txt.strip():
                            out.append((r.get("timestamp", ""), txt))
            self.cache[tid] = out
        return self.cache[tid]

    def recover(self, author: str, ts: str) -> str | None:
        tid = self.by_path.get(author)
        if not tid:
            return None
        prior = [t for s, t in self.answers(tid) if s <= ts]
        return prior[-1] if prior else None


def normalize(records: list[dict], db: dict, stats: dict) -> tuple[list[Item], dict]:
    info = {"model": None, "turn_finals": {}}
    reasoning_summaries: dict[str, list[str]] = {}
    for r in records:  # reasoning summaries are only readable in the item stream
        p = r.get("payload") or {}
        if r.get("type") == "event_msg" and p.get("type") == "item_completed":
            it = p.get("item") or {}
            if it.get("type") == "Reasoning" and it.get("summary_text"):
                reasoning_summaries[it.get("id")] = it["summary_text"]

    items: list[Item] = []
    resolvers: dict[str, SubagentResolver] = {}
    for r in records:
        t, p, ts = r.get("type"), r.get("payload") or {}, r.get("timestamp", "")
        inh, src = bool(r.get("_inherited")), r.get("_thread", "")
        add = lambda kind, **d: items.append(Item(kind, ts, d, inh, src))
        if src not in resolvers:
            resolvers[src] = SubagentResolver(db, src)
        resolver = resolvers[src]
        stats["records"] = stats.get("records", 0) + 1
        if t == "turn_context":
            info["model"] = p.get("model") or info["model"]
        elif t == "event_msg":
            et = p.get("type")
            if et == "task_started":
                add("turn", turn_id=p.get("turn_id"))
            elif et == "task_complete" and p.get("last_agent_message"):
                info["turn_finals"][p.get("turn_id")] = p["last_agent_message"]
            elif et == "thread_settings_applied":
                info["model"] = (p.get("thread_settings") or {}).get("model") or info["model"]
        elif t == "compacted":
            add("compaction", kept_user_messages=sum(
                1 for x in p.get("replacement_history") or [] if x.get("type") == "message" and x.get("role") == "user"))
            stats["compactions"] = stats.get("compactions", 0) + 1
        elif t == "response_item":
            rt = p.get("type")
            stats[f"response_item/{rt}"] = stats.get(f"response_item/{rt}", 0) + 1
            if rt == "message":
                role = p.get("role")
                blocks = to_blocks(p.get("content"))
                if role == "assistant":
                    if blocks:
                        add("text", blocks=blocks, phase=p.get("phase"))
                elif role == "user":
                    first = blocks_text(blocks[:1]).lstrip()
                    if first.startswith(INJECTED_USER_PREFIXES):
                        stats["skipped_injected_context"] = stats.get("skipped_injected_context", 0) + 1
                    elif first.startswith("<heartbeat"):
                        add("note", text="[Codex automation heartbeat]\n" + blocks_text(blocks)[:2000] + "\n\n")
                    elif blocks:
                        add("user", blocks=blocks)
                elif role in ("developer", "system"):
                    txt = blocks_text(blocks).lstrip()
                    if txt.startswith("<turn_aborted"):
                        add("note", text="[The user interrupted the previous turn]\n\n")
                    else:
                        stats["skipped_developer_instructions"] = stats.get("skipped_developer_instructions", 0) + 1
            elif rt == "reasoning":
                summ = [s.get("text", "") if isinstance(s, dict) else str(s) for s in p.get("summary") or []]
                summ = [s for s in summ if s] or reasoning_summaries.get(p.get("id")) or []
                txt = "\n".join((c.get("text") or "") for c in p.get("content") or [] if isinstance(c, dict))
                if summ or txt.strip():
                    add("reasoning", text="\n".join(summ) if summ else txt)
            elif rt in ("function_call", "custom_tool_call", "local_shell_call", "tool_search_call") or (
                    rt and rt.endswith("_call") and p.get("call_id")):
                raw = p.get("arguments", p.get("input", p.get("action")))
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = raw
                else:
                    parsed = raw
                inp = parsed if isinstance(parsed, dict) else {"input": parsed}
                add("call", call_id=safe_id(p.get("call_id") or p.get("id")),
                    name=safe_tool_name(p.get("name") or rt, p.get("namespace")), input=inp)
            elif rt and rt.endswith("_call_output"):
                out = p.get("output")
                if isinstance(out, dict) and "content" in out:
                    out = out["content"]
                blocks = to_blocks(out)
                txt = blocks_text(blocks)
                err = bool(re.search(r"(exit code|Exit code|exit_code\"?:)\s*[1-9]", txt[:4000])) or \
                    (isinstance(p.get("output"), dict) and p["output"].get("success") is False)
                add("result", call_id=safe_id(p.get("call_id")), blocks=blocks or [{"type": "text", "text": "(no output)"}], is_error=err)
            elif rt == "web_search_call":
                act = p.get("action") or {}
                add("text", blocks=[{"type": "text", "text": f"[web search: {act.get('query') or json.dumps(act)[:300]}]"}])
            elif rt == "agent_message":
                header = blocks_text([b for b in to_blocks(p.get("content")) if "encrypted" not in b.get("text", "")])
                encrypted = any(isinstance(c, dict) and c.get("type") == "encrypted_content" for c in p.get("content") or [])
                recovered = resolver.recover(p.get("author", ""), ts) if encrypted else None
                add("agent", author=p.get("author"), recipient=p.get("recipient"), header=header.strip(),
                    encrypted=encrypted, recovered=recovered)
            elif rt == "compaction":
                pass  # encrypted summary; the paired top-level `compacted` record drives the boundary
            else:
                stats[f"unknown/{rt}"] = stats.get(f"unknown/{rt}", 0) + 1
                add("text", blocks=[{"type": "text", "text": f"[Codex {rt}] " + json.dumps(p, ensure_ascii=False)[:2000]}])
    return items, info


# --------------------------------------------------------------------------- Claude messages

@dataclass
class Msg:
    role: str  # user | assistant | boundary | summary
    ts: str
    content: list = field(default_factory=list)
    inherited: bool = False
    real_user: bool = False  # a genuine human prompt (safe cut point)
    extra: dict = field(default_factory=dict)


def est_tokens(obj) -> int:
    if isinstance(obj, list):
        return sum(est_tokens(b) for b in obj)
    if isinstance(obj, dict):
        if obj.get("type") == "image":
            return image_tokens(obj.get("source") or {})
        if obj.get("type") == "tool_result":
            return est_tokens(obj.get("content")) + 10
        if obj.get("type") == "tool_use":
            return int(len(json.dumps(obj.get("input"), ensure_ascii=False)) / CHARS_PER_TOKEN) + 10
        return int(len(obj.get("text", "")) / CHARS_PER_TOKEN)
    if isinstance(obj, str):
        return int(len(obj) / CHARS_PER_TOKEN)
    return 0


def cap_blocks(blocks: list[dict], limit: int, where: str) -> list[dict]:
    out = []
    for b in blocks:
        if b["type"] == "text" and len(b["text"]) > limit:
            cut = len(b["text"]) - limit
            b = {"type": "text", "text": b["text"][: limit // 2] + f"\n\n[... {cut} characters omitted - full text in {where} ...]\n\n" + b["text"][-limit // 2:]}
        out.append(b)
    return out


def build_messages(items: list[Item], max_block_chars: int, md_path: str, stats: dict) -> list[Msg]:
    msgs: list[Msg] = []
    cur: Msg | None = None
    open_calls: list[str] = []

    def flush():
        nonlocal cur
        if cur is None:
            return
        if cur.role == "user":
            # every tool_use of the previous assistant message needs a result right here
            have = {b["tool_use_id"] for b in cur.content if b.get("type") == "tool_result"}
            missing = [c for c in open_calls if c not in have]
            for c in missing:
                cur.content.insert(0, {"type": "tool_result", "tool_use_id": c, "is_error": True,
                                       "content": [{"type": "text", "text": "[no output recorded in Codex - the call was interrupted or still running]"}]})
            stats["synthetic_tool_results"] = stats.get("synthetic_tool_results", 0) + len(missing)
            cur.content.sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
            open_calls.clear()
        if cur.content:
            msgs.append(cur)
        cur = None

    def start(role: str, it: Item):
        nonlocal cur
        if cur is not None and cur.role != role:
            if cur.role == "assistant":
                open_calls[:] = [b["id"] for b in cur.content if b.get("type") == "tool_use"]
                msgs.append(cur)
                cur = None
                if not open_calls:
                    pass
            else:
                flush()
        if cur is None:
            cur = Msg(role, it.ts, [], it.inherited)
        return cur

    def close_assistant_with_results():
        # an assistant message with pending tool calls must be followed by a user message
        nonlocal cur
        if cur is not None and cur.role == "assistant":
            open_calls[:] = [b["id"] for b in cur.content if b.get("type") == "tool_use"]
            msgs.append(cur)
            cur = None
        if open_calls:
            cur = Msg("user", msgs[-1].ts if msgs else "", [], msgs[-1].inherited if msgs else False)
            flush()

    for it in items:
        d = it.data
        if it.kind == "turn":
            continue
        if it.kind == "compaction":
            close_assistant_with_results()
            flush()
            msgs.append(Msg("boundary", it.ts, inherited=it.inherited, extra={"source": "codex", **d}))
            continue
        if it.kind in ("text", "reasoning", "call"):
            m = start("assistant", it)
            if it.kind == "text":
                m.content.extend(cap_blocks(d["blocks"], max_block_chars, md_path))
            elif it.kind == "reasoning":
                m.content.append({"type": "text", "text": f"<codex_reasoning_summary>\n{d['text']}\n</codex_reasoning_summary>"})
            else:
                inp = d["input"]
                if len(json.dumps(inp, ensure_ascii=False)) > max_block_chars:
                    inp = {"input_truncated": json.dumps(inp, ensure_ascii=False)[:max_block_chars],
                           "note": f"truncated - full call in {md_path}"}
                m.content.append({"type": "tool_use", "id": d["call_id"], "name": d["name"], "input": inp})
            continue
        # user-side items
        if it.kind == "result":
            if cur is not None and cur.role == "assistant":
                start("user", it)
            if d["call_id"] not in open_calls:
                # result for a call that is not the immediately preceding message: keep as text
                m = start("user", it)
                m.content.append({"type": "text", "text": f"[Output of earlier Codex tool call {d['call_id']}]\n" + blocks_text(d["blocks"])[:max_block_chars]})
                stats["orphan_tool_results"] = stats.get("orphan_tool_results", 0) + 1
                continue
            m = start("user", it)
            if any(b.get("tool_use_id") == d["call_id"] for b in m.content if b.get("type") == "tool_result"):
                continue
            m.content.append({"type": "tool_result", "tool_use_id": d["call_id"], "is_error": d["is_error"],
                              "content": cap_blocks(d["blocks"], max_block_chars, md_path)})
            continue
        m = start("user", it)
        if it.kind == "user":
            m.content.extend(cap_blocks(d["blocks"], max_block_chars, md_path))
            m.real_user = m.real_user or not any(b.get("type") == "tool_result" for b in m.content)
        elif it.kind == "agent":
            body = d["header"] or f"Message from {d['author']} to {d['recipient']}"
            if d["encrypted"]:
                body += ("\n[Payload encrypted by Codex. Recovered from the sub-agent's own transcript:]\n" + d["recovered"]) \
                    if d.get("recovered") else "\n[Payload encrypted by Codex - not recoverable]"
                stats["agent_msgs_recovered" if d.get("recovered") else "agent_msgs_encrypted"] = \
                    stats.get("agent_msgs_recovered" if d.get("recovered") else "agent_msgs_encrypted", 0) + 1
            m.content.append({"type": "text", "text": f"<codex_agent_message author=\"{d['author']}\" recipient=\"{d['recipient']}\">\n"
                                                      f"{body[:max_block_chars]}\n</codex_agent_message>"})
        elif it.kind == "note":
            m.content.append({"type": "text", "text": d["text"]})
    if cur is not None and cur.role == "assistant":
        msgs.append(cur)  # a trailing unanswered tool call is fine only at the very end; resolved below
        cur = None
        if any(b.get("type") == "tool_use" for b in msgs[-1].content):
            open_calls[:] = [b["id"] for b in msgs[-1].content if b.get("type") == "tool_use"]
            cur = Msg("user", msgs[-1].ts, [], msgs[-1].inherited)
            flush()
    else:
        flush()
    # merge adjacent same-role messages created by boundary handling
    merged: list[Msg] = []
    for m in msgs:
        if merged and m.role == merged[-1].role and m.role == "user" and not any(
                b.get("type") == "tool_result" for b in m.content):
            merged[-1].content.extend(m.content)
            merged[-1].real_user = merged[-1].real_user or m.real_user
        else:
            merged.append(m)
    return merged


# --------------------------------------------------------------------------- summaries

def digest_lines(msgs: list[Msg], tool_chars: int = 300, text_chars: int = 4000) -> list[str]:
    lines = []
    for m in msgs:
        if m.role == "boundary":
            lines.append("--- (context was compacted here in Codex) ---")
            continue
        if m.role == "summary":
            continue
        for b in m.content:
            t = b.get("type")
            if t == "text":
                txt = b["text"]
                if txt.startswith("<codex_reasoning_summary>"):
                    lines.append("REASONING: " + txt[25:-27].strip()[:600])
                elif m.role == "assistant":
                    lines.append("ASSISTANT: " + txt[:text_chars])
                else:
                    lines.append(("USER: " if m.real_user else "CONTEXT: ") + txt[:text_chars])
            elif t == "image":
                lines.append(f"{m.role.upper()}: [image]")
            elif t == "tool_use":
                lines.append(f"TOOL CALL {b['name']}: " + json.dumps(b["input"], ensure_ascii=False)[:tool_chars])
            elif t == "tool_result":
                lines.append("TOOL OUTPUT: " + blocks_text(b["content"] if isinstance(b["content"], list) else [])[:tool_chars])
    return lines


SUMMARY_PROMPT = """You are writing the context summary for a coding-agent conversation that is being moved from OpenAI Codex into Claude Code. The agent that resumes will see ONLY your summary plus the most recent messages, so it must be able to continue the work seamlessly.

{prev}Below is {which} of the conversation as a condensed digest (tool outputs are truncated). Write an updated summary that covers everything so far, with these sections:
1. Primary requests and intent (the user's goals, in their own words where it matters)
2. Key technical context (repos, paths, clusters/infrastructure, models, datasets, configs, commands)
3. Files and artifacts created or changed, and why
4. Experiments, runs and jobs: what was launched, where, status and results with the actual numbers
5. Errors, fixes and decisions (including approaches rejected and why)
6. User preferences and constraints stated during the conversation
7. Open tasks and the current state of work, most recent last
Be specific and factual; keep identifiers, paths, job IDs and numbers exact. Do not invent anything. Output only the summary, at most {words} words.

<digest>
{digest}
</digest>"""


def llm(prompt: str, claude_bin: str, model: str) -> str:
    cmd = [claude_bin, "-p", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--disable-slash-commands", "--model", model,
           "--system-prompt", "You summarize conversation transcripts accurately and concisely."]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900)
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"claude -p failed ({res.returncode}): {res.stderr[-800:]}")
    return res.stdout.strip()


def prompt_index(msgs_before: list[Msg], max_chars: int) -> str:
    """Timestamped list of the human prompts, most recent kept, to guide searching the transcript."""
    rows = []
    for m in msgs_before:
        if m.role == "user" and m.real_user:
            txt = " ".join(blocks_text([b for b in m.content if b.get("type") == "text"]).split())
            if txt:
                where = " (pre-fork)" if m.inherited else ""
                rows.append(f"- [{m.ts[:19].replace('T', ' ')}]{where} {txt[:160]}{'...' if len(txt) > 160 else ''}")
    out, size = [], 0
    for r in reversed(rows):
        if size + len(r) > max_chars:
            out.append(f"- ... {len(rows) - len(out)} earlier prompts not listed (search the transcript)")
            break
        out.append(r)
        size += len(r)
    return "\n".join(reversed(out))


def search_help(md_path: str, n_turns: int, first_ts: str, last_ts: str) -> str:
    return (
        f"The earlier history is not loaded into context, but it is complete and searchable in {md_path} "
        f"({n_turns} user turns, {first_ts[:10]} to {last_ts[:10]}). In that file each user turn starts with "
        "'## User — <timestamp>', agent replies with '### Codex — <timestamp>', tool calls with '**Tool call `<name>`**' "
        "and tool output with '**Output**'. To recall something, Grep the file for distinctive terms (job IDs, file "
        "names, paths, error text, numbers, or the timestamp of a prompt from the index below) and then Read the "
        "surrounding lines. Prefer searching over guessing whenever earlier context matters."
    )


def make_summary(msgs_before: list[Msg], mode: str, args, log, md_path: str = "") -> str:
    real = [m for m in msgs_before if m.role == "user" and m.real_user and not m.inherited]
    help_ = search_help(md_path, len(real), real[0].ts if real else "", real[-1].ts if real else "")
    if mode == "free":
        return help_ + "\n\nIndex of the user's prompts so far (most recent last):\n" + prompt_index(msgs_before, args.index_chars)
    lines = digest_lines(msgs_before)
    cap, size, start = int(args.summary_input_tokens * CHARS_PER_TOKEN), 0, 0
    for k in range(len(lines) - 1, -1, -1):  # only the most recent part feeds the summary
        size += len(lines[k])
        if size > cap:
            start = k + 1
            break
    if start:
        lines = ["[... earlier history omitted from this summary - see the full transcript ...]"] + lines[start:]
    if not lines:
        return help_
    # rolling summary over chunks that each fit comfortably in context
    chunk_chars = int(args.chunk_tokens * CHARS_PER_TOKEN)
    chunks, cur, size = [], [], 0
    for l in lines:
        l = l[:chunk_chars]
        if size + len(l) > chunk_chars and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(l)
        size += len(l)
    if cur:
        if chunks and size < chunk_chars * 0.25:  # fold a small remainder into the last chunk
            chunks[-1] += "\n" + "\n".join(cur)
        else:
            chunks.append("\n".join(cur))
    summary = ""
    for i, ch in enumerate(chunks):
        log(f"    summarizing chunk {i + 1}/{len(chunks)} (~{int(len(ch) / CHARS_PER_TOKEN / 1000)}k tokens)")
        prev = f"Summary of the conversation up to this point:\n<previous_summary>\n{summary}\n</previous_summary>\n\n" if summary else ""
        which = "the next part" if summary else ("the first part" if len(chunks) > 1 else "the whole")
        summary = llm(SUMMARY_PROMPT.format(prev=prev, which=which, digest=ch, words=args.summary_words),
                      args.claude_bin, args.model)
    return summary + "\n\n" + help_


def summary_text(body: str, md_path: str, model: str | None, orig_cwd: str = "", cwd: str = "", branch: str = "") -> str:
    where = ""
    if orig_cwd and orig_cwd != cwd:
        where = (f"Codex worked in a separate git worktree at {orig_cwd}"
                 f"{f' (branch {branch})' if branch else ''}, not in {cwd}. Files it created or edited live in that "
                 "worktree/branch; check there (or `git worktree list`) before assuming something is missing.\n\n")
    return (
        "This session is being continued from a previous conversation that ran out of context. "
        "The text below covers the earlier portion of the conversation.\n\n"
        f"Note: this conversation was originally held in OpenAI Codex (model: {model or 'unknown'}) and imported into Claude Code. "
        "Tool calls named exec, exec_command, apply_patch, wait, spawn_agent, etc. were Codex tools and are not available here - "
        "use your own tools. <codex_reasoning_summary> blocks are summaries of Codex's hidden reasoning.\n\n"
        f"{where}{body}\n\n"
        "If you need specific details from before compaction (like exact code snippets, error messages, or content you generated), "
        f"read the full transcript at: {md_path}\n\nRecent messages are preserved verbatim."
    )


# --------------------------------------------------------------------------- transcript (.md)

def write_markdown(path: Path, title: str, meta: dict, items: list[Item]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f"# {title}\n\nCodex thread `{meta.get('id')}` · cwd `{meta.get('cwd')}` · started {meta.get('timestamp')}\n\n")
        inh_done = False
        for it in items:
            d = it.data
            if it.inherited and not inh_done:
                fh.write("> Turns below marked (inherited) come from the thread this one was forked from.\n\n")
                inh_done = True
            tag = " (inherited)" if it.inherited else ""
            ts = it.ts[:19].replace("T", " ")
            if it.kind == "user":
                fh.write(f"\n## User{tag} — {ts}\n\n{blocks_text(d['blocks'])}\n")
            elif it.kind == "text":
                fh.write(f"\n### Codex{tag} — {ts}\n\n{blocks_text(d['blocks'])}\n")
            elif it.kind == "reasoning":
                fh.write(f"\n*Reasoning summary:* {d['text']}\n")
            elif it.kind == "call":
                fh.write(f"\n**Tool call `{d['name']}`** ({d['call_id']})\n\n```\n{json.dumps(d['input'], ensure_ascii=False, indent=1)}\n```\n")
            elif it.kind == "result":
                fh.write(f"\n**Output** ({d['call_id']}{', error' if d['is_error'] else ''})\n\n```\n{blocks_text(d['blocks'])}\n```\n")
            elif it.kind == "agent":
                rec = d.get("recovered") or ("[encrypted]" if d["encrypted"] else "")
                fh.write(f"\n**Agent message {d['author']} → {d['recipient']}**\n\n{d['header']}\n{rec}\n")
            elif it.kind == "note":
                fh.write(f"\n_{d['text']}_\n")
            elif it.kind == "compaction":
                fh.write(f"\n---\n*Codex compacted its context here ({ts}).*\n\n---\n")


HTML_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#6b6862;--user:#eef3fb;--line:#e2dfd8;--code:#f3f1ec;--accent:#b4541a}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a18;--fg:#ecebe6;--mute:#a19d94;--user:#23303f;--line:#34322e;--code:#262522;--accent:#e08a4f}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 80px}
header{border-bottom:1px solid var(--line);margin-bottom:24px;padding-bottom:12px}
h1{font-size:22px;margin:0 0 6px} .meta{color:var(--mute);font-size:13px} a{color:var(--accent)}
.msg{margin:14px 0;white-space:pre-wrap;word-wrap:break-word}
.user{background:var(--user);border-radius:12px;padding:10px 14px}
.who{font-size:12px;color:var(--mute);margin-bottom:2px;white-space:normal}
.reason{color:var(--mute);font-style:italic;font-size:13px;margin:6px 0;white-space:pre-wrap}
details{margin:4px 0;font-size:13px;border-left:2px solid var(--line);padding-left:10px}
summary{cursor:pointer;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
pre{background:var(--code);padding:8px 10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;font-size:12px;margin:6px 0}
.note,.agent{color:var(--mute);font-size:13px;margin:10px 0;white-space:pre-wrap}
.agent{border-left:3px solid var(--accent);padding-left:10px}
.compact{text-align:center;color:var(--mute);font-size:12px;margin:26px 0;border-top:1px dashed var(--line);padding-top:6px}
.inh{opacity:.85}
.idx li{margin:6px 0;font-size:14px} footer{margin-top:30px}
"""


def write_html(path: Path, title: str, meta: dict, items: list[Item], md_path: Path, links: list[tuple[str, str]],
               per_page: int = 40, line_chars: int = 160, msg_chars: int = 20000):
    """Readable chat history: an index of prompts (path) plus pages of conversation next to it
    (<stem>/page-N.html). Runs of tool activity fold into one row; the .md keeps everything."""
    import html as _h
    esc = lambda t: _h.escape(t or "", quote=True)
    cut = lambda t, n: t if len(t) <= n else t[:n] + f"\n… [{len(t) - n:,} more characters in the raw transcript]"
    one = lambda t, n=line_chars: (lambda x: x if len(x) <= n else x[:n] + "…")(" ".join((t or "").split()))
    page_dir = path.parent / path.stem
    page_dir.mkdir(parents=True, exist_ok=True)
    head = lambda t, extra="": (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
                               f"content='width=device-width,initial-scale=1'><title>{esc(t)}</title><style>{HTML_CSS}</style>"
                               f"</head><body><main>{extra}")

    # split into pages at user prompts
    pages: list[list[Item]] = [[]]
    count = 0
    for it in items:
        if it.kind == "user":
            if count and count % per_page == 0:
                pages.append([])
            count += 1
        pages[-1].append(it)
    prompts = [i for i in items if i.kind == "user"]
    span = f"{(prompts[0].ts if prompts else '')[:10]} → {(items[-1].ts if items else '')[:10]}"

    def nav(n: int) -> str:
        parts = [f"<a href='../{esc(path.name)}'>All prompts</a>"]
        if n > 0:
            parts.append(f"<a href='page-{n}.html'>← Earlier</a>")
        parts.append(f"Page {n + 1} of {len(pages)}")
        if n + 1 < len(pages):
            parts.append(f"<a href='page-{n + 2}.html'>Later →</a>")
        return "<div class='meta'>" + " · ".join(parts) + "</div>"

    index_rows = []
    for n, page in enumerate(pages):
        with open(page_dir / f"page-{n + 1}.html", "w") as fh:
            fh.write(head(f"{title} · page {n + 1}", f"<header><h1>{esc(title)}</h1>{nav(n)}</header>\n"))
            run: list[str] = []

            def flush_run():
                if run:
                    calls = sum(1 for r in run if r.startswith("🔧"))
                    label = f"⚙️ {calls} tool call{'s' if calls != 1 else ''}" if calls else "💭 thinking"
                    fh.write(f"<details><summary>{label}</summary>"
                             f"<pre>{esc(chr(10).join(run))}</pre></details>\n")
                    run.clear()

            for k, it in enumerate(page):
                d, ts = it.data, it.ts[:19].replace("T", " ")
                inh = " inh" if it.inherited else ""
                if it.kind in ("call", "result", "reasoning"):
                    if it.kind == "call":
                        inp = d["input"]
                        txt = inp["input"] if isinstance(inp.get("input"), str) else json.dumps(inp, ensure_ascii=False)
                        run.append(f"🔧 {d['name']}: {one(txt)}")
                    elif it.kind == "result":
                        run.append(f"   ↳ {'ERROR ' if d['is_error'] else ''}{one(blocks_text(d['blocks']))}")
                    else:
                        run.append(f"💭 {one(d['text'], 240)}")
                    continue
                flush_run()
                if it.kind == "user":
                    anchor = f"u{n}-{k}"
                    index_rows.append((n, anchor, ts, blocks_text(d["blocks"]), it.inherited))
                    fh.write(f"<div class='msg user{inh}' id='{anchor}'><div class='who'>You · {esc(ts)}</div>"
                             f"{esc(cut(blocks_text(d['blocks']), msg_chars))}</div>\n")
                elif it.kind == "text":
                    fh.write(f"<div class='msg{inh}'><div class='who'>Codex · {esc(ts)}</div>{esc(cut(blocks_text(d['blocks']), msg_chars))}</div>\n")
                elif it.kind == "agent":
                    rec = d.get("recovered") or ("[encrypted by Codex]" if d["encrypted"] else "")
                    fh.write(f"<details class='agent'><summary>🤝 {esc(d['author'])} → {esc(d['recipient'])}: "
                             f"{esc(one(d['header'].split(chr(10))[0]))}</summary><pre>{esc(cut(d['header'] + chr(10) + rec, 4000))}</pre></details>\n")
                elif it.kind == "note":
                    fh.write(f"<div class='note'>{esc(d['text'].strip())}</div>\n")
                elif it.kind == "compaction":
                    fh.write(f"<div class='compact'>Codex compacted its context here · {esc(ts)}</div>\n")
            flush_run()
            fh.write(f"<footer>{nav(n)}</footer></main></body></html>\n")

    with open(path, "w") as fh:
        fh.write(head(title, f"<header><h1>{esc(title)}</h1><div class='meta'>Imported from OpenAI Codex · thread "
                             f"{esc(meta.get('id'))} · {esc(span)} · {len(prompts)} prompts · {len(pages)} pages<br>"
                             f"Start reading: <a href='{esc(page_dir.name)}/page-1.html'>first page</a> · "
                             f"<a href='{esc(page_dir.name)}/page-{len(pages)}.html'>latest page</a><br>"
                             f"Tool activity is condensed here; the complete raw transcript is "
                             f"<a href='{esc(md_path.name)}'>{esc(md_path.name)}</a>."))
        for label, href in links:
            fh.write(f"<br>{esc(label)}: <a href='{esc(href)}'>chat history</a>")
        fh.write("</div></header><h2 style='font-size:16px'>Your prompts</h2><ol class='idx'>\n")
        for n, anchor, ts, text, inh in index_rows:
            fh.write(f"<li><a href='{esc(page_dir.name)}/page-{n + 1}.html#{anchor}'>{esc(ts)}</a>"
                     f"{' <span class=meta>(pre-fork)</span>' if inh else ''} — {esc(one(text, 220))}</li>\n")
        fh.write("</ol></main></body></html>\n")


def write_history(md_path: Path, title: str, meta: dict, items: list[Item], links: list[tuple[str, str]]) -> Path:
    write_markdown(md_path, title, meta, items)
    html_path = md_path.with_suffix(".html")
    write_html(html_path, title, meta, items, md_path, links)
    return html_path


def desktop_link(thread_id: str) -> str:
    return "claude://claude.ai/epitaxy/local_" + str(uuid.uuid5(NAMESPACE, "desktop:" + thread_id))


# --------------------------------------------------------------------------- assembly

def encode_project(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def resolve_cwd(cwd: str | None, row: dict) -> str:
    """Map Codex worktrees (~/.codex/worktrees/<id>/<repo>) back to the main checkout."""
    if not cwd:
        return str(HOME)
    wt_root = str(CODEX_HOME / "worktrees") + "/"
    if not cwd.startswith(wt_root):
        return cwd
    gitfile = Path(cwd) / ".git"
    if gitfile.is_file():
        m = re.match(r"gitdir:\s*(.+?)/\.git/worktrees/", gitfile.read_text())
        if m and Path(m.group(1)).is_dir():
            return m.group(1)
    name = Path(cwd).name
    for cand in [HOME / name, *[Path(p) / name for p in glob.glob(str(HOME / "*")) if Path(p).is_dir()]]:
        if (cand / ".git").exists():
            return str(cand)
    return cwd


def detect_version() -> str:
    try:
        vs = sorted((HOME / "Library/Application Support/Claude/claude-code").iterdir(),
                    key=lambda p: [int(x) if x.isdigit() else 0 for x in p.name.split(".")])
        if vs:
            return vs[-1].name
    except OSError:
        pass
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20).stdout
        return out.split()[0]
    except Exception:
        return "2.1.280"


def assemble(msgs: list[Msg], session_id: str, cwd: str, branch: str, version: str, model: str | None,
             title: str) -> list[dict]:
    recs: list[dict] = [{"type": "custom-title", "customTitle": title, "sessionId": session_id}]
    prev = None
    base = {"isSidechain": False, "userType": "external", "entrypoint": "cli", "cwd": cwd,
            "sessionId": session_id, "version": version, "gitBranch": branch}
    for i, m in enumerate(msgs):
        u = str(uuid.uuid5(NAMESPACE, f"{session_id}:{i}"))
        ts = m.ts or datetime.now(timezone.utc).isoformat()
        if m.role == "boundary":
            r = {"parentUuid": None, "logicalParentUuid": prev, **base, "type": "system", "subtype": "compact_boundary",
                 "content": "Conversation compacted", "isMeta": False, "level": "info", "timestamp": ts, "uuid": u,
                 "compactMetadata": {"trigger": "auto", "preTokens": m.extra.get("pre_tokens", 0)}}
        elif m.role == "summary":
            r = {"parentUuid": prev, **base, "type": "user", "message": {"role": "user", "content": m.extra["text"]},
                 "isCompactSummary": True, "isVisibleInTranscriptOnly": True, "uuid": u, "timestamp": ts}
        elif m.role == "user":
            content = m.content
            if len(content) == 1 and content[0]["type"] == "text":
                content = content[0]["text"]
            r = {"parentUuid": prev, **base, "type": "user", "message": {"role": "user", "content": content},
                 "uuid": u, "timestamp": ts, "permissionMode": "default"}
            if m.real_user:
                r["promptId"] = str(uuid.uuid5(NAMESPACE, u))
        else:
            has_tool = any(b.get("type") == "tool_use" for b in m.content)
            r = {"parentUuid": prev, **base, "type": "assistant", "uuid": u, "timestamp": ts,
                 "requestId": "req_codeximport_" + u.replace("-", "")[:20],
                 "message": {"id": "msg_codeximport_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                             "model": model or "codex", "content": m.content,
                             "stop_reason": "tool_use" if has_tool else "end_turn", "stop_sequence": None,
                             "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0, "service_tier": "standard"}}}
        recs.append(r)
        prev = u
    last_prompt = next((m for m in reversed(msgs) if m.role == "user" and m.real_user), None)
    if last_prompt:
        recs.append({"type": "last-prompt", "lastPrompt": blocks_text(last_prompt.content)[:200],
                     "leafUuid": prev, "sessionId": session_id})
    return recs


def place_final_boundary(msgs: list[Msg], budget: int) -> int | None:
    """Index where the resume-time context should start, or None if everything fits."""
    last_b = max((i for i, m in enumerate(msgs) if m.role == "boundary"), default=-1)
    tail = sum(est_tokens(m.content) for m in msgs[last_b + 1:])
    if tail <= budget:
        return last_b if last_b >= 0 else None
    # walk back from the end until the budget is used, then cut at the next human prompt
    acc, cut = 0, len(msgs)
    for i in range(len(msgs) - 1, last_b, -1):
        acc += est_tokens(msgs[i].content)
        if acc > budget:
            break
        cut = i
    tails = [0] * (len(msgs) + 1)
    for i in range(len(msgs) - 1, -1, -1):
        tails[i] = tails[i + 1] + est_tokens(msgs[i].content)
    # prefer starting at a human prompt: the nearest one before the cut if it stays under
    # 1.5x budget, else the first one after it
    for j in range(cut - 1, last_b, -1):
        if tails[j] > budget * 1.25:
            break
        if msgs[j].role == "user" and msgs[j].real_user:
            return j
    for j in range(cut, len(msgs)):
        if msgs[j].role == "user" and msgs[j].real_user:
            return j
    # no human prompt in the tail (one very long autonomous turn): cut before any assistant message
    for j in range(cut, len(msgs)):
        if msgs[j].role == "assistant":
            return j
    return len(msgs) - 1


def validate(recs: list[dict], budget: int) -> list[str]:
    problems = []
    by_uuid = {r["uuid"]: r for r in recs if "uuid" in r}
    msgs = [r for r in recs if r.get("type") in ("user", "assistant", "system")]
    for a, b in zip(msgs, msgs[1:]):
        if b.get("parentUuid") not in (a["uuid"], None):
            problems.append(f"broken chain at {b['uuid']}")
        if a["type"] == "assistant":
            ids = [c["id"] for c in a["message"]["content"] if c.get("type") == "tool_use"]
            if ids:
                nxt = b["message"]["content"] if b["type"] == "user" and isinstance(b["message"]["content"], list) else []
                got = {c.get("tool_use_id") for c in nxt if c.get("type") == "tool_result"}
                if set(ids) - got:
                    problems.append(f"tool_use without result after {a['uuid']}")
    # what Claude Code sends on resume: everything after the last boundary
    last = max((i for i, r in enumerate(msgs) if r.get("subtype") == "compact_boundary"), default=-1)
    tail = msgs[last + 1:]
    tok = sum(est_tokens(r["message"]["content"]) for r in tail if "message" in r and not r.get("isCompactSummary"))
    if tok > budget * 1.5:
        problems.append(f"resume context ~{tok} tokens exceeds budget {budget}")
    first = next((r for r in tail if not r.get("isCompactSummary")), None)
    if first and first["type"] == "user" and isinstance(first["message"]["content"], list) and any(
            c.get("type") == "tool_result" for c in first["message"]["content"]):
        problems.append("resume context starts with an orphan tool_result")
    seen_ids: set[str] = set()
    for r in msgs:
        c = r.get("message", {}).get("content")
        if isinstance(c, str):
            if not c.strip():
                problems.append(f"empty message {r['uuid']}")
            continue
        if isinstance(c, list):
            if not c:
                problems.append(f"empty message {r['uuid']}")
            for b in c:
                if b.get("type") == "text" and not b["text"].strip():
                    problems.append(f"empty text block in {r['uuid']}")
                if b.get("type") == "tool_use":
                    if b["id"] in seen_ids:
                        problems.append(f"duplicate tool_use id {b['id']}")
                    seen_ids.add(b["id"])
    for r in recs:
        json.dumps(r)
    return problems


def convert(thread_id: str, db: dict, args, log) -> dict:
    row = db.get(thread_id, {})
    records, meta = load_thread_records(thread_id)
    if not records:
        return {"thread": thread_id, "status": "no-rollout"}
    stats: dict = {}
    items, info = normalize(records, db, stats)
    model = info["model"] or row.get("model")
    cwd = args.cwd or resolve_cwd(row.get("cwd") or meta.get("cwd"), row)
    title_raw = row.get("name") or row.get("title") or next(
        (blocks_text(i.data["blocks"])[:80] for i in items if i.kind == "user"), thread_id)
    title = f"{args.title_prefix}{title_raw}"
    session_id = str(uuid.uuid5(NAMESPACE, "codex:" + thread_id))
    proj = encode_project(cwd)
    out_root = Path(args.out) if args.out else CLAUDE_HOME
    md_path = (Path(args.out) if args.out else CLAUDE_HOME) / "codex-imports" / proj / f"{thread_id}.md"
    target = out_root / "projects" / proj / f"{session_id}.jsonl"

    if target.exists() and not args.force:
        return {"thread": thread_id, "status": "exists", "session": session_id, "path": str(target)}

    own = [i for i in items if not i.inherited]
    inherited = [i for i in items if i.inherited]
    prefix: list[Msg] = []  # history that feeds summaries but is not copied into the session
    fork_note = ""
    if inherited and args.inherit == "link":
        parent = meta.get("forked_from_id")
        prow = db.get(parent, {})
        pmd = md_path.parent / f"{parent}.md"
        fork_note = (f"This thread was forked in Codex from the thread \"{prow.get('name') or prow.get('title') or parent}\" "
                     f"(Codex id {parent}); the pre-fork history is in {pmd} (and earlier ancestors next to it).")
        for anc in dict.fromkeys(i.thread for i in inherited):  # transcripts for ancestors not imported yet
            anc_md = md_path.parent / f"{anc}.md"
            if not anc_md.exists():
                arow = db.get(anc, {})
                write_history(anc_md, (arow.get("name") or anc) + " (history up to the fork point)",
                              {"id": anc, "cwd": cwd, "timestamp": arow.get("created_at")},
                              [i for i in inherited if i.thread == anc], [])
        prefix = build_messages(inherited, args.max_block_chars, str(pmd), stats)
        msgs = [Msg("boundary", own[0].ts if own else "", extra={"source": "fork"})] + \
            build_messages(own, args.max_block_chars, str(md_path), stats)
        items = own
    else:
        msgs = build_messages(items, args.max_block_chars, str(md_path), stats)

    # summaries for Codex's own compaction points (cosmetic; not sent on resume)
    if args.no_codex_boundaries:
        msgs = [m for m in msgs if m.role != "boundary"]
    final_at = place_final_boundary(msgs, args.budget_tokens)
    if final_at is not None and msgs[final_at].role != "boundary":
        msgs.insert(final_at, Msg("boundary", msgs[final_at].ts, extra={"source": "budget"}))
    out: list[Msg] = []
    for i, m in enumerate(msgs):
        out.append(m)
        if m.role != "boundary":
            continue
        before = [x for x in prefix + msgs[:i] if x.role in ("user", "assistant", "boundary")]
        m.extra["pre_tokens"] = sum(est_tokens(x.content) for x in before)
        is_final = i == final_at
        if is_final:
            log(f"  final boundary at message {i}: {args.mode} mode, ~{m.extra['pre_tokens'] // 1000}k earlier tokens")
            body = make_summary(before, args.mode, args, log, str(md_path))
        else:  # intermediate boundaries are never sent to the model; keep them light
            body = "Codex compacted its context at this point. The full history is in " + str(md_path) + "."
        if fork_note:
            body = fork_note + "\n\n" + body
        out.append(Msg("summary", m.ts, extra={"text": summary_text(body, str(md_path), model, row.get("cwd") or meta.get("cwd") or "", cwd, row.get("git_branch") or "")}))
    msgs = out

    html_path = md_path.with_suffix(".html")
    parent = meta.get("forked_from_id") if not meta.get("parent_thread_id") else None
    links: list[tuple[str, str]] = []
    header = (f"📜 **Imported from OpenAI Codex:** *{title_raw}*\n\n"
              f"**[Open the full chat history]({html_path})**: every message from Codex, including the parts "
              f"that aren't loaded into Claude's context. Claude searches the raw transcript `{md_path}` when it "
              "needs earlier details.")
    if parent:
        prow = db.get(parent, {})
        pname = prow.get("name") or prow.get("title") or parent
        phtml = md_path.parent / f"{parent}.html"
        header += f"\n\n↩️ Forked from **{pname}**: [its chat history]({phtml})"
        if args.desktop:
            header += f" · [open that chat]({desktop_link(parent)})"
        links = [("Forked from " + pname, phtml.as_uri())]
    top = [Msg("assistant", msgs[0].ts if msgs else "", [{"type": "text", "text": header}])]
    if not msgs or msgs[0].role != "boundary":
        top += [Msg("boundary", msgs[0].ts if msgs else "", extra={"pre_tokens": 0}),
                Msg("summary", msgs[0].ts if msgs else "", extra={"text": summary_text(
                    "This is the start of the imported conversation; nothing earlier is missing.", str(md_path), model,
                    row.get("cwd") or meta.get("cwd") or "", cwd, row.get("git_branch") or "")})]
    msgs = top + msgs

    recs = assemble(msgs, session_id, cwd, row.get("git_branch") or "", args.version, model, title)
    problems = validate(recs, args.budget_tokens)
    write_history(md_path, title_raw, {**meta, "cwd": cwd}, items, links)
    if problems and not args.allow_problems:
        return {"thread": thread_id, "status": "invalid", "problems": problems[:10], "stats": stats}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        arch = target.parent / "_archive"
        arch.mkdir(exist_ok=True)
        shutil.move(str(target), str(arch / f"{target.stem}.{datetime.now().strftime('%Y%m%d%H%M%S')}.jsonl"))
    tmp = target.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, target)
    last_b = max((i for i, r in enumerate(recs) if r.get("subtype") == "compact_boundary"), default=-1)
    resume_tok = sum(est_tokens(r["message"]["content"]) for r in recs[last_b + 1:] if "message" in r)
    total_tok = sum(est_tokens(r["message"]["content"]) for r in recs if "message" in r and not r.get("isCompactSummary"))
    manifest = CLAUDE_HOME / "codex-imports" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    result = {"thread": thread_id, "status": "ok", "session": session_id, "title": title, "cwd": cwd,
              "path": str(target), "transcript": str(md_path), "history": str(html_path), "messages": len(recs),
              "total_tokens_est": total_tok, "resume_tokens_est": resume_tok,
              "boundaries": sum(1 for r in recs if r.get("subtype") == "compact_boundary"),
              "problems": problems, "stats": stats, "imported_at": datetime.now(timezone.utc).isoformat()}
    if not args.out:
        with open(manifest, "a") as fh:
            fh.write(json.dumps(result) + "\n")
    return result


def desktop_dir(explicit: str | None) -> Path | None:
    """The Claude desktop app's per-account session index (the folder holding local_*.json)."""
    if explicit:
        return Path(explicit)
    root = HOME / "Library/Application Support/Claude/claude-code-sessions"
    recs = sorted(root.glob("*/*/local_*.json"), key=lambda p: p.stat().st_mtime)
    return recs[-1].parent if recs else None


def register_desktop(result: dict, row: dict, args) -> str | None:
    """Add the imported session to the desktop app's sidebar (visible after an app restart)."""
    folder = desktop_dir(args.desktop_dir)
    if folder is None:
        return None
    sid = "local_" + str(uuid.uuid5(NAMESPACE, "desktop:" + result["thread"]))
    path = folder / f"{sid}.json"
    if path.exists() and not args.force:
        return str(path)
    now = int(datetime.now().timestamp() * 1000)
    rec = {"sessionId": sid, "cliSessionId": result["session"], "cwd": result["cwd"], "originCwd": result["cwd"],
           "createdAt": row.get("created_at_ms") or now, "lastActivityAt": row.get("updated_at_ms") or now,
           "isArchived": bool(row.get("archived")), "permissionMode": "default", "remoteMcpServersConfig": [],
           "model": args.desktop_model, "title": result["title"], "titleSource": "user"}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, path)
    return str(path)


def select_threads(db: dict, args) -> list[str]:
    if args.threads:
        return args.threads
    ids = []
    for tid, row in db.items():
        if "rollout_path" not in row:
            continue
        if row.get("archived") and not args.include_archived:
            continue
        meta = {"source": row.get("source")}
        try:
            meta["source"] = json.loads(row.get("source") or '""')
        except (json.JSONDecodeError, TypeError):
            pass
        if is_subagent(meta, row) and not args.include_subagents:
            continue
        if args.project:
            cwd = resolve_cwd(row.get("cwd"), row)
            if os.path.realpath(cwd) != os.path.realpath(args.project):
                continue
        ids.append((row.get("updated_at_ms") or 0, tid))
    return [t for _, t in sorted(ids, reverse=True)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("threads", nargs="*", help="Codex thread ids (default: use --project or --all)")
    ap.add_argument("--project", help="import every top-level thread whose (worktree-resolved) cwd is this folder")
    ap.add_argument("--all", action="store_true", help="import every top-level thread")
    ap.add_argument("--list", action="store_true", help="only list what would be imported")
    ap.add_argument("--include-subagents", action="store_true")
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--mode", choices=["free", "claude"], default="free",
                    help="free: replay since the last compaction, earlier history searchable via a prompt index and "
                         "the full transcript (no model calls). claude: additionally have Claude write a summary")
    ap.add_argument("--model", default="sonnet", help="model for --mode claude")
    ap.add_argument("--index-chars", type=int, default=12000, help="size cap of the prompt index")
    ap.add_argument("--claude-bin", default=None)
    ap.add_argument("--budget-tokens", type=int, default=80000, help="max tokens sent when the session is resumed")
    ap.add_argument("--chunk-tokens", type=int, default=100000, help="digest chunk size per summarization call")
    ap.add_argument("--summary-words", type=int, default=2500)
    ap.add_argument("--summary-input-tokens", type=int, default=300000,
                    help="summaries read at most this much of the most recent digest")
    ap.add_argument("--inherit", choices=["link", "full"], default="link",
                    help="forked threads: link to the parent's history (default) or copy it in")
    ap.add_argument("--max-block-chars", type=int, default=30000, help="truncate single blocks beyond this (full text in .md)")
    ap.add_argument("--no-codex-boundaries", action="store_true", help="only keep the final boundary")
    ap.add_argument("--cwd", help="override the Claude project folder")
    ap.add_argument("--title-prefix", default="[Codex] ")
    ap.add_argument("--version", default=None, help="Claude Code version to stamp (default: detected)")
    ap.add_argument("--out", help="write into this folder instead of ~/.claude (dry run)")
    ap.add_argument("--force", action="store_true", help="re-import; the old file is moved to _archive/")
    ap.add_argument("--allow-problems", action="store_true")
    ap.add_argument("--desktop", action="store_true",
                    help="also list imported sessions in the Claude desktop app's sidebar (restart the app to see them)")
    ap.add_argument("--desktop-dir", help="override the desktop app's session index folder")
    ap.add_argument("--desktop-model", default="claude-opus-5-5", help="model the desktop app resumes with")
    args = ap.parse_args()
    if not (args.threads or args.project or args.all):
        ap.error("give thread ids, --project or --all")
    args.version = args.version or detect_version()
    if not args.claude_bin:
        app = sorted((HOME / "Library/Application Support/Claude/claude-code").glob("*/claude.app/Contents/MacOS/claude"))
        args.claude_bin = str(app[-1]) if app else (shutil.which("claude") or "claude")
    log = lambda s: print(s, file=sys.stderr, flush=True)
    db = load_db()
    ids = select_threads(db, args)
    if args.list:
        for t in ids:
            r = db.get(t, {})
            print(t, (r.get("name") or r.get("title") or "")[:70], sep="\t")
        print(f"{len(ids)} threads", file=sys.stderr)
        return
    ok = 0
    for n, t in enumerate(ids, 1):
        log(f"[{n}/{len(ids)}] {t} {(db.get(t, {}).get('name') or '')[:60]}")
        try:
            res = convert(t, db, args, log)
        except Exception as e:  # keep going on bulk imports
            res = {"thread": t, "status": "error", "error": repr(e)}
        if res["status"] == "ok" and args.desktop and not args.out:
            res["desktop"] = register_desktop(res, db.get(t, {}), args)
        ok += res["status"] == "ok"
        print(json.dumps(res, ensure_ascii=False))
    log(f"done: {ok}/{len(ids)} imported")


if __name__ == "__main__":
    main()
