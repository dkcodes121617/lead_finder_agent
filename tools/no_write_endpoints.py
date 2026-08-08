"""CI guard: the Lead Finder must never write to any platform.

    python tools/no_write_endpoints.py        exit 1 on any violation

## Why this file exists

"Never post, comment, vote, reply or DM" is currently a sentence in a document.
Documents do not fail builds. Six months from now, someone — plausibly the
person who wrote the rule — adds "just an auto-reply to the highest-intent
leads" because it seems obviously useful, and nothing anywhere objects.

This objects. It greps the whole agent for the endpoint paths and client methods
that write to a platform, and fails the build if one appears.

The rule is absolute and deliberately has no "unless": no comment, no reply, no
DM, no vote, no submission, no follow — not immediately, and not after a delay.
This agent reads public sources and writes rows in our own database. That is all
it is for.

## What is NOT a violation

Writing to Postgres and sending Telegram notifications. Both are ours; neither
touches a prospect. Nothing here objects to `INSERT INTO core.leads`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "output", ".ruff_cache"}
# This file necessarily contains every banned pattern; scanning it would be a
# guaranteed self-report.
SKIP_FILES = {Path(__file__).name}

# (pattern, what it would do). Word-bounded so `submit_button` or a variable
# called `vote_count` does not trip it — a guard that cries wolf gets disabled,
# and a disabled guard protects nothing.
BANNED: list[tuple[str, str]] = [
    (r"/api/submit\b", "Reddit submit endpoint"),
    (r"/api/comment\b", "Reddit comment endpoint"),
    (r"/api/vote\b", "Reddit vote endpoint"),
    (r"/api/compose\b", "Reddit private message endpoint"),
    (r"/api/editusertext\b", "Reddit edit endpoint"),
    (r"/api/follow\b", "follow endpoint"),
    (r"\.reply\s*\(", "PRAW reply()"),
    (r"\.submit\s*\(", "PRAW submit()"),
    (r"\.upvote\s*\(", "PRAW upvote()"),
    (r"\.downvote\s*\(", "PRAW downvote()"),
    (r"\.message\s*\(\s*[\"']", "PRAW redditor.message()"),
    (r"\bsubreddit\.subscribe\s*\(", "subreddit subscribe"),
    (r"/2/tweets\b", "X post-tweet endpoint"),
    (r"/tweet/create\b", "X create-tweet endpoint"),
    (r"/dm_conversations\b", "X direct-message endpoint"),
    (r"/direct_messages\b", "X direct-message endpoint"),
    (r"/favorites/create\b", "X like endpoint"),
    (r"/statuses/update\b", "X post endpoint"),
    (r"/2\.3/answers/add\b", "Stack Exchange answer endpoint"),
    (r"/comments/add\b", "Stack Exchange comment endpoint"),
    (r"graph\.facebook\.com[^\"'\s]*/(feed|comments|messages)\b", "Meta publish endpoint"),
    (r"graph\.instagram\.com[^\"'\s]*/media_publish\b", "Instagram publish endpoint"),
    (r"api\.linkedin\.com[^\"'\s]*/(ugcPosts|posts)\b", "LinkedIn publish endpoint"),
]

COMPILED = [(re.compile(p, re.I), why) for p, why in BANNED]

# A line carrying this marker is documentation about the rule, not a call.
ALLOW_MARKER = "no-write-ok"


def scan() -> list[tuple[Path, int, str, str]]:
    violations: list[tuple[Path, int, str, str]] = []
    for path in AGENT_ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts) or path.name in SKIP_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                continue
            stripped = line.strip()
            # Comments and docstring prose describe the rule constantly. Only
            # flag lines that could actually execute.
            if stripped.startswith("#"):
                continue
            for pattern, why in COMPILED:
                if pattern.search(line):
                    violations.append((path, lineno, why, stripped[:110]))
    return violations


def main() -> int:
    violations = scan()
    if not violations:
        scanned = sum(
            1 for p in AGENT_ROOT.rglob("*.py")
            if not any(part in SKIP_DIRS for part in p.parts)
        )
        print(f"no_write_endpoints: OK - {scanned} file(s) scanned, no platform writes found")
        return 0

    print("no_write_endpoints: FAILED\n")
    print("The Lead Finder must never write to any platform - no comment, reply,")
    print("DM, vote or submission, including after a delay.\n")
    for path, lineno, why, snippet in violations:
        rel = path.relative_to(AGENT_ROOT)
        print(f"  {rel}:{lineno}")
        print(f"    {why}")
        print(f"    {snippet}\n")
    print(f"{len(violations)} violation(s).")
    print(
        "If a line is documentation rather than a call, append the marker "
        f"'{ALLOW_MARKER}' to it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
