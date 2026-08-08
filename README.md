# Lead Finder

Finds business leads from seven sources, classifies them, and puts them in one
prioritised queue that the Outreach agent drains. Runs on Modal every 30
minutes.

**This agent never writes to any platform.** No comment, reply, DM, vote or
submission — not immediately, not after a delay. That is enforced by
`tools/no_write_endpoints.py`, which fails the build if a write endpoint ever
appears anywhere in the repo.

It is also the **sole writer of `core.leads`**, which is what makes "no
duplicates" a property of a UNIQUE constraint rather than a promise.

---

## Setup

```powershell
python -m venv .venv                                   # Python 3.11
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\wizcore
copy .env.example .env                                 # then fill it in
```

Never edit `.env` by hand in PowerShell — it mangles the encoding. Use:

```powershell
python tools/set_env.py KEY=VALUE
```

## Check it works

```powershell
python tools/preflight.py          # config only
python tools/preflight.py --live   # + call every service it uses (read-only)
```

`--live` costs a fraction of a cent (two metered vendors) and should end with
`lead_finder_agent: ready.`

## Run

```powershell
python main.py --dry-run                        # full run, writes nothing
python main.py --dry-run --sources hackernews   # one source
python main.py --digest                         # daily digest, all agents
python main.py                                  # honours DRY_RUN from .env
```

## Deploy

```powershell
.\deploy.ps1              # preflight -> migrations -> secret -> deploy
.\deploy.ps1 -DryRun      # print the plan, touch nothing
.\deploy.ps1 -SecretOnly  # after rotating a credential
```

On a **fresh database, deploy this agent first** — it owns the shared `core`
schema the other two reference through foreign keys.

## Configuration that matters

| Variable | Why |
|---|---|
| `DRY_RUN` | The kill switch. `1` = collect and classify, write nothing. |
| `SOURCES_ENABLED` | `inbound,hackernews,reddit,twitter,stackexchange,places,osm`. A broken source is removed from this list — no code change. |
| `NOTIFY_MIN_SCORE` | Telegram alert threshold (default 55). |
| `MAX_CANDIDATES_PER_RUN` | Cap per run; the rest age out. |
| `CLASSIFIER_PROVIDER` | `proxy` routes classification through the Claude proxy. `groq` is faster but a second vendor. |
| `LOOKBACK_MINUTES` | Overlaps the cron window deliberately; the unique constraint makes overlap free. |

Only credentials the **enabled** sources need are asserted, so a source you
have no key for costs nothing as long as it is not listed.

## Layout

```
sources/     one file per source, one interface, independently failing
scoring/     classification prompts + provider routing
pipeline/    dedupe · persist · notify · digest · trend feed
graph/       LangGraph state, nodes, build
tools/       preflight · set_env · migrations · no_write_endpoints (CI guard)
migrations/  the shared `core` schema — this agent owns it
```
