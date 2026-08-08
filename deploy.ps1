# ═══════════════════════════════════════════════════════════════════════════
# Deploy THIS agent. Self-contained: reads only this folder.
#
#     .\deploy.ps1                 preflight -> migrations -> secret -> deploy
#     .\deploy.ps1 -SecretOnly     after rotating a credential
#     .\deploy.ps1 -Live           include live credential checks in preflight
#     .\deploy.ps1 -DryRun         print the plan, touch nothing
#
# One-time, before the first run:
#     .\.venv\Scripts\python.exe -m pip install -r requirements.txt
#     .\.venv\Scripts\modal.exe setup        (browser login, once per machine)
#
# Order is deliberate and not configurable:
#   1. PREFLIGHT  - a missing credential should stop the deploy, not surface as
#                   a container that crashes on boot looking like a code bug.
#   2. MIGRATIONS - a container booting against a schema older than its code
#                   fails confusingly. Advisory-locked and idempotent, so
#                   running it every deploy costs nothing.
#   3. SECRET     - Modal reads a secret at container START, so shipping new
#                   code against a stale secret is the classic "works locally"
#                   outage. Regenerated from .env every time.
#   4. CODE
# ═══════════════════════════════════════════════════════════════════════════
param(
    [switch]$SecretOnly,
    [switch]$SkipMigrations,
    [switch]$Live,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$agent = Split-Path $root -Leaf

# Folder -> Modal secret name. Kept here rather than derived, so renaming a
# folder cannot silently point an agent at another agent's credentials.
$secretMap = @{
    "content_poster_agent" = "wizcodes-content"
    "lead_finder_agent"    = "wizcodes-leadfind"
    "outreach_agent"       = "wizcodes-outreach"
}
$secretName = $secretMap[$agent]
if (-not $secretName) { Write-Host "no Modal secret mapped for '$agent'" -ForegroundColor Red; exit 1 }

$py    = Join-Path $root ".venv\Scripts\python.exe"
$modal = Join-Path $root ".venv\Scripts\modal.exe"
if (-not (Test-Path $py)) { Write-Host "no venv - run: python -m venv .venv" -ForegroundColor Red; exit 1 }

# ── 1. preflight ──
Write-Host "`n[1/4] preflight ($agent)" -ForegroundColor Cyan
$pfArgs = @((Join-Path $root "tools\preflight.py"))
if ($Live) { $pfArgs += "--live" }
& $py @pfArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "`npreflight failed - fix the above before deploying." -ForegroundColor Red
    exit 1
}

# ── 2. migrations ──
if ($SkipMigrations) {
    Write-Host "`n[2/4] migrations SKIPPED" -ForegroundColor DarkGray
} else {
    Write-Host "`n[2/4] migrations" -ForegroundColor Cyan
    if ($DryRun) { Write-Host "    (dry run)" } else {
        & $py (Join-Path $root "tools\apply_migrations.py")
        if ($LASTEXITCODE -ne 0) { Write-Host "migrations failed - nothing deployed." -ForegroundColor Red; exit 1 }
    }
}

# ── 3. secret ──
Write-Host "`n[3/4] secret -> $secretName" -ForegroundColor Cyan
if (-not (Test-Path $modal)) { Write-Host "modal not installed in this venv" -ForegroundColor Red; exit 1 }

# Build KEY=VALUE pairs in Python and hand them over as an argv LIST. Several
# real values contain spaces, parentheses and slashes (REDDIT_USER_AGENT is the
# obvious one); any shell in the path would mangle them into a credential that
# is wrong in a way nothing reports. Empty values are DROPPED, never shipped:
# an empty string in Modal satisfies a `if not os.getenv(...)` guard while being
# useless, so a missing credential should be absent and fail loudly at startup.
$pairs = & $py -c @"
from pathlib import Path
skip = {'BRAND_FONT_PATH','BRAND_FONT_REGULAR_PATH','BRAND_FONT_MONO_PATH'}
for line in Path(r'$root/.env').read_text(encoding='utf-8-sig').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k, v = line.split('=', 1); k, v = k.strip(), v.strip()
    if v and k not in skip: print(f'{k}={v}')
"@
if ($LASTEXITCODE -ne 0) { Write-Host "could not read .env" -ForegroundColor Red; exit 1 }

Write-Host "    $($pairs.Count) keys from $agent/.env"
if ($DryRun) { Write-Host "    (dry run - secret not written)" } else {
    & $modal secret create $secretName @pairs --force
    if ($LASTEXITCODE -ne 0) { Write-Host "secret sync failed." -ForegroundColor Red; exit 1 }
}

if ($SecretOnly) { Write-Host "`nsecret-only: done" -ForegroundColor Green; exit 0 }

# ── 4. code ──
Write-Host "`n[4/4] deploy" -ForegroundColor Cyan
$appFile = Join-Path $root "modal_app.py"
if (-not (Test-Path $appFile)) {
    Write-Host "    modal_app.py not written yet - infrastructure is ready, code is not." -ForegroundColor Yellow
    exit 0
}
if ($DryRun) { Write-Host "    (dry run)"; exit 0 }
Push-Location $root
try { & $modal deploy modal_app.py } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { Write-Host "deploy failed." -ForegroundColor Red; exit 1 }
Write-Host "`n$agent deployed." -ForegroundColor Green
