#requires -Version 7.0
<#
.SYNOPSIS
    First-time + ongoing PythonAnywhere deploy for this bot — native Windows
    PowerShell port of scripts/pa_deploy.sh, driven entirely from your terminal
    via PythonAnywhere's HTTP API.

.DESCRIPTION
    Reads config from .env in the repo root. Required keys:
        PA_USERNAME, PA_API_TOKEN, TELEGRAM_BOT_TOKEN, AI_API_KEY

    Idempotent: re-running heals partial state — it recreates a deleted web app,
    fixes WSGI / source_directory / virtualenv drift, and pushes a fresh .env.
    For ongoing pushes the GitHub Actions workflow (.github/workflows/deploy.yml)
    is simpler; this script is for first-time setup and recovery.

    Requires PowerShell 7+ (winget install Microsoft.PowerShell). Windows
    PowerShell 5.1 lacks Invoke-WebRequest -Form / -SkipHttpErrorCheck.

    Two unavoidable manual steps (PythonAnywhere limits, not ours):
      1. Grab an API token: https://www.pythonanywhere.com/account/#api_token
      2. If the script has to create a NEW bash console, open the URL it prints
         once in a browser so PA initializes it. This is skipped automatically
         when the clone + virtualenv already exist on PA (e.g. you only deleted
         the web app), so a web-app-only recovery runs fully headless.

.EXAMPLE
    .\scripts\pa_deploy.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Repo root = parent of this script's folder (scripts\..).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

# ---- pretty output ----------------------------------------------------------
function Write-Ok   { param([string]$m) Write-Host "  $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "! $m" -ForegroundColor Yellow }
function Write-Err  { param([string]$m) Write-Host "x $m" -ForegroundColor Red }
function Write-Info { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Die        { param([string]$m) Write-Err $m; exit 1 }

# ---- read .env --------------------------------------------------------------
function Import-DotEnv {
    param([string]$Path)
    $h = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $h }
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $line = ($raw -replace "`r$", '').Trim()
        if ($line -eq '' -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1)
        # strip one matching pair of surrounding quotes
        if ($val.Length -ge 2 -and (
                ($val.StartsWith('"') -and $val.EndsWith('"')) -or
                ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $h[$key] = $val
    }
    return $h
}

if (-not (Test-Path -LiteralPath '.env')) {
    Die ".env not found in repo root. Copy .env.example to .env and fill it in first."
}
$envCfg = Import-DotEnv '.env'

function Get-Cfg {
    param([string]$Name, [string]$Default = $null)
    if ($envCfg.ContainsKey($Name) -and $envCfg[$Name] -ne '') { return $envCfg[$Name] }
    return $Default
}
function Require-Cfg {
    param([string]$Name, [string]$Hint)
    $v = Get-Cfg $Name
    if (-not $v) { Write-Err "$Name is not set in .env."; if ($Hint) { Write-Host "   $Hint" }; exit 1 }
    return $v
}

$PaUsername = Require-Cfg 'PA_USERNAME'       'Your PythonAnywhere username (e.g. alicesmith).'
$PaToken    = Require-Cfg 'PA_API_TOKEN'      'Get one at https://www.pythonanywhere.com/account/#api_token'
[void](Require-Cfg 'TELEGRAM_BOT_TOKEN'       'From @BotFather on Telegram.')
[void](Require-Cfg 'AI_API_KEY'               'Your Cerebras / OpenAI-compatible API key.')

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required but not installed." }

$RepoUrl = (& git remote get-url origin 2>$null)
if (-not $RepoUrl) { Die "this repo has no 'origin' remote. Push to GitHub first, then re-run." }
$RepoName = [IO.Path]::GetFileNameWithoutExtension($RepoUrl.Trim())

# PA consoles have no SSH key for GitHub, so convert SSH remotes to HTTPS for
# the remote clone (works for public repos).
$CloneUrl = $RepoUrl.Trim()
if ($CloneUrl -like 'git@github.com:*')        { $CloneUrl = 'https://github.com/' + $CloneUrl.Substring('git@github.com:'.Length) }
elseif ($CloneUrl -like 'ssh://git@github.com/*') { $CloneUrl = 'https://github.com/' + $CloneUrl.Substring('ssh://git@github.com/'.Length) }

$PaApi               = "https://www.pythonanywhere.com/api/v0/user/$PaUsername"
$Domain              = "$PaUsername.pythonanywhere.com"
$ProjectDir          = "/home/$PaUsername/$RepoName"
$VenvDir             = "/home/$PaUsername/.virtualenvs/telegram-bot"
$WsgiFile            = "/var/www/${PaUsername}_pythonanywhere_com_wsgi.py"
$WebhookUrlResolved  = "https://$Domain/api/webhook"
$PythonVersion       = 'python313'

Write-Host ""
Write-Info "Deploying $RepoName to https://$Domain"
Write-Host "    project:  $ProjectDir"
Write-Host "    venv:     $VenvDir"
Write-Host "    wsgi:     $WsgiFile"
Write-Host ""

# ---- HTTP helper (PA API) ---------------------------------------------------
# Returns @{ Code; Body }. -SkipHttpErrorCheck keeps 4xx/5xx from throwing so we
# can branch on the status code (PA returns 403 for a missing web app, etc.).
function Invoke-Pa {
    param(
        [string]$Method = 'Get',
        [Parameter(Mandatory)][string]$Path,   # absolute URL, or a path appended to $PaApi
        $Body = $null,
        [switch]$Form,
        [switch]$NoAuth,
        [int]$TimeoutSec = 30
    )
    $uri = if ($Path -match '^https?://') { $Path } else { "$PaApi$Path" }
    $headers = @{}
    if (-not $NoAuth) { $headers['Authorization'] = "Token $PaToken" }
    $p = @{
        Uri = $uri; Method = $Method; Headers = $headers; TimeoutSec = $TimeoutSec
        SkipHttpErrorCheck = $true
    }
    if ($Form)              { $p.Form = $Body }
    elseif ($null -ne $Body) {
        $p.Body = $Body
        # A hashtable body needs an explicit Content-Type. Invoke-WebRequest does
        # not reliably set one for non-POST verbs (e.g. PATCH), and PA's API then
        # rejects the request with HTTP 415 ("Unsupported media type").
        if ($Body -is [System.Collections.IDictionary]) {
            $p.ContentType = 'application/x-www-form-urlencoded'
        }
    }
    try {
        # Read the status off the response object rather than -StatusCodeVariable:
        # that parameter isn't present in every PowerShell 7.x build (e.g. the
        # scoop pwsh 7.6.x lacks it), and referencing it makes Invoke-WebRequest
        # throw "parameter cannot be found", which this catch would then
        # misreport as a network failure (Code = 0). $resp.StatusCode works on
        # all 7.x when paired with -SkipHttpErrorCheck.
        $resp = Invoke-WebRequest @p
        return [pscustomobject]@{ Code = [int]$resp.StatusCode; Body = [string]$resp.Content }
    } catch {
        # Network-level failure (DNS, TLS, timeout) — no HTTP status.
        return [pscustomobject]@{ Code = 0; Body = [string]$_.Exception.Message }
    }
}

# Files API existence check (needs no console): 200 if the path exists on PA.
function Test-PaPath {
    param([string]$RemotePath)
    (Invoke-Pa -Path "/files/path$RemotePath").Code -eq 200
}

# ---- 1. Verify API token works ----------------------------------------------
Write-Info "Verifying PA API token..."
if ((Invoke-Pa -Path '/cpu/').Code -ne 200) {
    Die "PA API rejected the token. Check PA_USERNAME and PA_API_TOKEN in .env."
}

# ---- 2. Create web app (idempotent) -----------------------------------------
# Detect existence via the LIST endpoint, not GET /webapps/<domain>/: PA returns
# 403 ("You do not have permission to perform this action.") — NOT 404 — for a
# domain with no web app (e.g. just after deleting one), so a per-domain status
# check can't tell "missing" apart from "forbidden". The list is unambiguous.
Write-Info "Ensuring web app exists..."
$listResp = Invoke-Pa -Path '/webapps/'
if ($listResp.Code -ne 200) { Die "could not list web apps (HTTP $($listResp.Code))." }
$exists = $false
foreach ($app in @($listResp.Body | ConvertFrom-Json)) {
    if ($app.domain_name -eq $Domain) { $exists = $true; break }
}
if ($exists) {
    Write-Ok "Web app already exists."
} else {
    Write-Ok "Creating web app ($PythonVersion)..."
    $create = Invoke-Pa -Method Post -Path '/webapps/' -Body @{ domain_name = $Domain; python_version = $PythonVersion }
    if ($create.Code -ne 201 -and $create.Code -ne 200) {
        Die "web app create failed (HTTP $($create.Code)): $($create.Body)"
    }
}

# ---- 3 + 4. Console-driven setup (clone / venv / pip) ------------------------
# If the clone (.git) AND venv (bin/python) already exist on PA, the console
# steps are no-ops, so skip them entirely — a web-app-only recovery then runs
# fully headless with no browser console init. NOTE: this skips git pull and pip
# install, so if you changed requirements.txt, delete the venv (or use a fresh
# console) to force a reinstall.
if ((Test-PaPath "$ProjectDir/.git") -and (Test-PaPath "$VenvDir/bin/python")) {
    Write-Info "Clone + virtualenv already present on PA — skipping console setup."
} else {
    Write-Info "Finding a usable bash console..."
    $consoles = (Invoke-Pa -Path '/consoles/').Body | ConvertFrom-Json
    $consoleId = $null
    foreach ($c in @($consoles)) { if ($c.executable -eq 'bash') { $consoleId = $c.id; break } }

    $needsBrowserClick = $false
    if (-not $consoleId) {
        Write-Host "    No existing bash console. Creating one..."
        $cc = Invoke-Pa -Method Post -Path '/consoles/' -Body @{ executable = 'bash'; arguments = '' }
        $consoleId = ($cc.Body | ConvertFrom-Json).id
        $needsBrowserClick = $true
    } else {
        Write-Host "    Reusing existing bash console (id=$consoleId)."
        # Existing consoles may still be uninitialized — PA returns 412 until the
        # console has been opened once in a browser.
        if ((Invoke-Pa -Path "/consoles/$consoleId/get_latest_output/").Code -ne 200) { $needsBrowserClick = $true }
    }

    if ($needsBrowserClick) {
        $consoleUrl = "https://www.pythonanywhere.com/user/$PaUsername/consoles/$consoleId/"
        Write-Host ""
        Write-Warn "!!! ONE-TIME MANUAL STEP !!!"
        Write-Host "    Open this URL in your browser, wait for the shell prompt to load, then come back:"
        Write-Host "    $consoleUrl"
        Write-Host ""
        Read-Host "    Press Enter once the console has loaded in your browser" | Out-Null
    }

    function Send-ConsoleInput {
        param([string]$Id, [string]$Cmd)
        # PA's send_input needs a trailing newline to actually press Enter.
        [void](Invoke-Pa -Method Post -Path "/consoles/$Id/send_input/" -Body @{ input = "$Cmd`n" })
    }

    # Wait until a unique marker shows up in the console output. The single quotes
    # around OK/FAIL in the sent command keep the echoed *input* line from matching
    # the markers we grep for — only the executed echo produces the bare marker.
    function Wait-Marker {
        param([string]$Id, [string]$Marker, [int]$TimeoutSec, [string]$Label)
        $elapsed = 0
        while ($elapsed -lt $TimeoutSec) {
            Start-Sleep -Seconds 3
            $elapsed += 3
            $out = ''
            try { $out = [string]((Invoke-Pa -Path "/consoles/$Id/get_latest_output/").Body | ConvertFrom-Json).output } catch { $out = '' }
            if ($out.Contains("${Marker}_FAIL")) {
                Write-Err "[$Label] failed on the PA console. Recent output:"
                ($out -split "`n" | Select-Object -Last 15) | ForEach-Object { Write-Host $_ }
                return $false
            }
            if ($out.Contains("${Marker}_OK")) { return $true }
        }
        Write-Err "[$Label] timed out waiting for marker."
        return $false
    }

    function Invoke-Remote {
        param([string]$Id, [string]$Label, [string]$Cmd, [int]$TimeoutSec = 180)
        $marker = "__PADEPLOY_$([DateTime]::UtcNow.Ticks)_${PID}__"
        Write-Host "    [$Label] running..."
        Send-ConsoleInput $Id "{ $Cmd; } && echo ${marker}_'OK' || echo ${marker}_'FAIL'"
        if (-not (Wait-Marker $Id $marker $TimeoutSec $Label)) { Die "[$Label] failed on PA." }
    }

    Invoke-Remote $consoleId 'git clone or pull' `
        "if [ -d $ProjectDir/.git ]; then cd $ProjectDir && git pull --ff-only; else git clone $CloneUrl $ProjectDir; fi" 120
    Invoke-Remote $consoleId 'create venv (if missing)' `
        "[ -d $VenvDir ] || python3.13 -m venv $VenvDir" 60
    Invoke-Remote $consoleId 'pip install requirements' `
        "$VenvDir/bin/pip install --upgrade pip && $VenvDir/bin/pip install -r $ProjectDir/requirements.txt" 300
}

# ---- 5. Upload .env to PA ---------------------------------------------------
Write-Info "Generating PA-side .env..."
$envLines = [System.Collections.Generic.List[string]]::new()
function Emit       { param([string]$K, [string]$V) $envLines.Add("$K=$V") }
function Emit-IfSet { param([string]$K) $v = Get-Cfg $K; if ($v) { Emit $K $v } }

Emit 'TELEGRAM_BOT_TOKEN' (Get-Cfg 'TELEGRAM_BOT_TOKEN')
Emit 'AI_API_KEY'         (Get-Cfg 'AI_API_KEY')
Emit 'AI_BASE_URL'        (Get-Cfg 'AI_BASE_URL' 'https://api.cerebras.ai/v1')
Emit 'AI_MODEL'           (Get-Cfg 'AI_MODEL' 'gpt-oss-120b')
Emit 'SQLITE_PATH'        (Get-Cfg 'SQLITE_PATH' "/home/$PaUsername/bot.db")
Emit 'WEBHOOK_URL'        (Get-Cfg 'WEBHOOK_URL' $WebhookUrlResolved)
Emit 'HOSTING_LABEL'      (Get-Cfg 'HOSTING_LABEL' 'PythonAnywhere')
Emit 'RATE_LIMIT'         (Get-Cfg 'RATE_LIMIT' '250')
Emit-IfSet 'WEBHOOK_SECRET'
Emit-IfSet 'ALLOWED_USERS'
Emit-IfSet 'HF_SPACE_ID'
Emit-IfSet 'HF_TOKEN'
Emit-IfSet 'DEPLOY_SECRET'
Emit-IfSet 'GNEWS_API_KEY'

# Write with LF endings (PA is Linux) — never CRLF.
$tmpEnv = Join-Path ([IO.Path]::GetTempPath()) ("pa_env_" + [Guid]::NewGuid().ToString('N') + ".tmp")
[IO.File]::WriteAllText($tmpEnv, (($envLines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))

Write-Info "Uploading .env to $ProjectDir/.env ..."
$up = Invoke-Pa -Method Post -Path "/files/path$ProjectDir/.env" -Form @{ content = Get-Item -LiteralPath $tmpEnv }
Remove-Item -LiteralPath $tmpEnv -ErrorAction SilentlyContinue
if ($up.Code -ne 200 -and $up.Code -ne 201) { Die ".env upload failed (HTTP $($up.Code))." }

# ---- 6. Upload the PA-side WSGI file ----------------------------------------
$wsgiContent = @"
import sys

project_home = "$ProjectDir"
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from pythonanywhere_wsgi import application  # noqa: F401
"@
$tmpWsgi = Join-Path ([IO.Path]::GetTempPath()) ("pa_wsgi_" + [Guid]::NewGuid().ToString('N') + ".py")
[IO.File]::WriteAllText($tmpWsgi, (($wsgiContent -replace "`r`n", "`n").TrimEnd() + "`n"), (New-Object Text.UTF8Encoding($false)))

Write-Info "Uploading WSGI file to $WsgiFile ..."
$uw = Invoke-Pa -Method Post -Path "/files/path$WsgiFile" -Form @{ content = Get-Item -LiteralPath $tmpWsgi }
Remove-Item -LiteralPath $tmpWsgi -ErrorAction SilentlyContinue
if ($uw.Code -ne 200 -and $uw.Code -ne 201) { Die "WSGI upload failed (HTTP $($uw.Code))." }

# ---- 7. Point the web app at source dir + virtualenv ------------------------
# PATCH the values AND read them back from the response: a 200 alone isn't proof
# the new values stuck (config drift to /var/www with a blank virtualenv is how
# the 2026-06 outage happened).
Write-Info "Configuring web app source + virtualenv..."
$patch = Invoke-Pa -Method Patch -Path "/webapps/$Domain/" -Body @{ source_directory = $ProjectDir; virtualenv_path = $VenvDir }
if ($patch.Code -ne 200) { Die "web app config failed (HTTP $($patch.Code)): $($patch.Body)" }
$cfg = $patch.Body | ConvertFrom-Json
Write-Host "    source_directory = $($cfg.source_directory)"
Write-Host "    virtualenv_path  = $($cfg.virtualenv_path)"
if ($cfg.source_directory -ne $ProjectDir -or $cfg.virtualenv_path -ne $VenvDir) {
    Die "web app config did not take effect (values above don't match what we set)."
}

# ---- 8. Reload --------------------------------------------------------------
Write-Info "Reloading web app..."
$reload = Invoke-Pa -Method Post -Path "/webapps/$Domain/reload/"
if ($reload.Code -ne 200) {
    Write-Warn "reload returned HTTP $($reload.Code). Try clicking Reload in the PA Web tab if the bot is silent."
}

# ---- 9. Smoke test, with automatic error-log diagnosis on failure -----------
# Reloads are async and a cold worker can take ~5-10s, so poll instead of a
# single shot. If it never comes up, pull the PA error log automatically.
Write-Info "Smoke-testing /api/health (polling up to ~45s)..."
$health = 0
for ($i = 0; $i -lt 9; $i++) {
    Start-Sleep -Seconds 5
    $health = (Invoke-Pa -NoAuth -Path "https://$Domain/api/health" -TimeoutSec 15).Code
    if ($health -eq 200) { break }
}
if ($health -eq 200) {
    Write-Ok "OK (200) — bot is live."
} else {
    Write-Err "/api/health returned $health after reload."
    Write-Info "Fetching PA error log to diagnose..."
    $log = (Invoke-Pa -Path "/files/path/var/log/$Domain.error.log").Body
    if ($log) {
        Write-Host "----- last 25 lines of /var/log/$Domain.error.log -----"
        ($log -split "`n" | Select-Object -Last 25) | ForEach-Object { Write-Host $_ }
        Write-Host "-------------------------------------------------------"
    } else {
        Write-Host "    (could not fetch the error log via API — check the PA Web tab.)"
    }
    Write-Host "    Re-running this script heals config/WSGI drift; if a dependency is"
    Write-Host "    missing, run once from a fresh PA bash console so pip can install it."
    exit 1
}

Write-Host ""
Write-Info "Done. Bot is live at https://$Domain"
Write-Host ""
Write-Host "    Send your bot a message on Telegram to try it."
Write-Host "    Updates from here on: just push to main (the GitHub Action auto-deploys)."
Write-Host "    Or re-run this script — it's idempotent."
