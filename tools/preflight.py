"""Lead Finder — configuration check, and optionally a live credential test.

    python tools/preflight.py           # config only: what is set, what it blocks
    python tools/preflight.py --live    # also call every service this agent uses

Reads only this agent's .env. Nothing outside this folder.

`--live` is read-only against every service. A missing credential is SKIP, not
FAIL — that distinction is the whole point: FAIL means "you have a credential and
it does not work", which is the expensive thing to discover from a 3am cron.

Two `--live` checks cost a fraction of a cent (redditapis $0.002/read,
twitterapis $0.0008/read). Stated so nobody has to wonder.
"""
from __future__ import annotations

import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent

# name -> (required?, what it blocks if missing)
SPEC: dict[str, tuple[bool, str]] = {
    "ANTHROPIC_BASE_URL":          (True,  "every LLM call"),
    "ANTHROPIC_API_KEY":           (True,  "every LLM call"),
    "NEON_DATABASE_URL":           (True,  "the lead queue - this agent's whole output"),
    "LANGGRAPH_CHECKPOINT_SCHEMA": (True,  "durable graph state; must be unique per agent"),
    "TELEGRAM_BOT_TOKEN":          (True,  "lead alerts"),
    "TELEGRAM_CHAT_ID":            (True,  "lead alerts"),
    "TELEGRAM_CALLBACK_PREFIX":    (True,  "routing button presses back to this agent"),
    "SITE_REPO":                   (True,  "canonical service-line names"),
    "SITE_READ_TOKEN":             (True,  "canonical service-line names (read-only PAT)"),
    "GROQ_API_KEY":                (True,  "lead classification - nothing gets scored without it"),
    "SOURCES_ENABLED":             (True,  "which sources run at all"),
    "CLASSIFIER_PROVIDER":         (True,  "lead scoring"),
    "NOTIFY_MIN_SCORE":            (True,  "the alert threshold"),
    "DRY_RUN":                     (True,  "the kill switch must be explicit, never defaulted"),
    "REDDITAPIS_KEY":              (False, "the reddit source only"),
    "REDDITAPIS_POSTS_PATH":       (False, "the reddit source only"),
    "TWITTERAPIS_KEY":             (False, "the twitter source only"),
    "STACKEXCHANGE_KEY":           (False, "the stackexchange source only"),
    "GOOGLE_PLACES_API_KEY":       (False, "cold business discovery only"),
    "REDDIT_CLIENT_ID":            (False, "the PRAW standby only - Reddit declined commercial use"),
}


def load() -> dict[str, str]:
    out: dict[str, str] = {}
    env = AGENT_ROOT / ".env"
    if not env.exists():
        sys.exit(f"no .env at {env}")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def check_config(env: dict[str, str]) -> int:
    print("\nCONFIG")
    print("------")
    missing = 0
    for name, (required, blocks) in SPEC.items():
        if env.get(name, ""):
            print(f"  [ok]      {name}")
        elif required:
            missing += 1
            print(f"  [MISSING] {name:<26} blocks: {blocks}")
        else:
            print(f"  [ - ]     {name:<26} optional: {blocks}")
    return missing


def check_live(env: dict[str, str]) -> int:
    import requests

    print("\nLIVE (read-only)")
    print("----------------")
    failures = 0

    def run(label: str, key: str, fn):
        nonlocal failures
        if not key:
            print(f"  SKIP  {label:<20} not set")
            return
        try:
            ok, detail = fn()
            print(f"  {'PASS' if ok else 'FAIL'}  {label:<20} {detail[:76]}")
            failures += 0 if ok else 1
        except Exception as e:
            print(f"  FAIL  {label:<20} {type(e).__name__}: {str(e)[:60]}")
            failures += 1

    def claude():
        r = requests.post(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json",
                     # Cloudflare in front of the proxy 403s (error 1010) any
                     # request whose UA does not look like a CLI.
                     "user-agent": "claude-cli/1.0.0 (external, cli)"},
            json={"model": env["ANTHROPIC_MODEL"], "max_tokens": 8,
                  "messages": [{"role": "user", "content": "Reply with: ok"}]}, timeout=60)
        return r.status_code == 200, f"{env['ANTHROPIC_MODEL']} HTTP {r.status_code}"

    def neon():
        import psycopg
        with psycopg.connect(env["NEON_DATABASE_URL"], connect_timeout=20) as c:
            v = c.execute("SHOW server_version").fetchone()[0]
            n = c.execute("SELECT count(*) FROM core.leads").fetchone()[0]
            return True, f"PG {v}, core.leads has {n} row(s)"

    def telegram():
        r = requests.get(f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/getMe",
                         timeout=25).json()
        return bool(r.get("ok")), f"@{(r.get('result') or {}).get('username')}"

    def github():
        r = requests.get(f"https://api.github.com/repos/{env['SITE_REPO']}",
                         headers={"Authorization": f"Bearer {env['SITE_READ_TOKEN']}"}, timeout=25)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        perms = r.json().get("permissions", {})
        # Loud on purpose: three agents that only READ should not hold push.
        warn = "  <- NOT read-only (has push/admin)" if perms.get("push") else ""
        return True, f"{r.json().get('full_name')}{warn}"

    def groq():
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {env['GROQ_API_KEY']}"},
                          json={"model": env.get("GROQ_CLASSIFY_MODEL", "llama-3.3-70b-versatile"),
                                "max_tokens": 8, "messages": [{"role": "user", "content": "Say ok"}]},
                          timeout=40)
        return r.status_code == 200, f"HTTP {r.status_code}"

    def reddit():
        path = env.get("REDDITAPIS_POSTS_PATH", "/api/reddit/posts")
        r = requests.get(f"{env['REDDITAPIS_BASE_URL']}{path}",
                         params={"subreddit": "smallbusiness", "sort": "new", "limit": 1},
                         headers={"Authorization": f"Bearer {env['REDDITAPIS_KEY']}"}, timeout=30)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} on {path}"
        return True, f"{path} -> {len(r.json().get('posts', []))} post(s)"

    def twitter():
        r = requests.get(f"{env['TWITTERAPIS_BASE_URL']}/twitter/tweet/advanced_search",
                         params={"query": '"need a website built" lang:en'},
                         headers={"Authorization": f"Bearer {env['TWITTERAPIS_KEY']}"}, timeout=30)
        return r.status_code == 200, f"HTTP {r.status_code}"

    def hn():
        r = requests.get(f"{env['HN_BASE_URL']}/search_by_date",
                         params={"query": "looking for developer", "tags": "story", "hitsPerPage": 1},
                         timeout=25)
        return r.status_code == 200, f"{r.json().get('nbHits', 0)} hits (no key needed)"

    def stackexchange():
        r = requests.get("https://api.stackexchange.com/2.3/info",
                         params={"site": "stackoverflow", "key": env["STACKEXCHANGE_KEY"]}, timeout=25)
        d = r.json()
        return "error_message" not in d, f"quota_remaining={d.get('quota_remaining')}"

    run("Claude proxy", env.get("ANTHROPIC_API_KEY", ""), claude)
    run("Neon", env.get("NEON_DATABASE_URL", ""), neon)
    run("Telegram", env.get("TELEGRAM_BOT_TOKEN", ""), telegram)
    run("Site repo (PAT)", env.get("SITE_READ_TOKEN", ""), github)
    run("Groq", env.get("GROQ_API_KEY", ""), groq)
    run("redditapis", env.get("REDDITAPIS_KEY", ""), reddit)
    run("twitterapis", env.get("TWITTERAPIS_KEY", ""), twitter)
    run("Hacker News", "always", hn)
    run("Stack Exchange", env.get("STACKEXCHANGE_KEY", ""), stackexchange)
    return failures


def main() -> int:
    env = load()
    missing = check_config(env)
    failures = check_live(env) if "--live" in sys.argv else 0
    print()
    if missing:
        print(f"{missing} required variable(s) missing.")
    if failures:
        print(f"{failures} live check(s) failed.")
    if not missing and not failures:
        print("lead_finder_agent: ready.")
    return 1 if (missing or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
