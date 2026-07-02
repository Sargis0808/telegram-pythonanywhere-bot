<#
.SYNOPSIS
    Native Windows task runner for this repo — the PowerShell equivalent of the
    Makefile, so Windows users don't need `make`.

.DESCRIPTION
    Targets:
      install     Ensure git/gh/python are present (installing missing ones
                  via scoop), create the .venv virtualenv, install requirements.txt
      test        Run the test suite (pytest)
      run         Run the bot locally via polling (needs .env)
      deploy-pa   Deploy to PythonAnywhere (needs .env + PowerShell 7; see scripts\pa_deploy.ps1)
      claude      Connect Claude Code to the workshop gateway (passes args through)
      help        Show this list (default)

.EXAMPLE
    .\make.ps1 install

.EXAMPLE
    .\make.ps1 run

.EXAMPLE
    .\make.ps1 claude sk-your-key
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Target = 'help',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]$Rest
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'

function Show-Help {
    Write-Host "Usage: .\make.ps1 <target>" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  install     Ensure git/gh/python (via scoop), create .venv, install requirements.txt"
    Write-Host "  test        Run the test suite (pytest)"
    Write-Host "  run         Run the bot locally via polling (needs .env)"
    Write-Host "  deploy-pa   Deploy to PythonAnywhere (needs .env + PowerShell 7)"
    Write-Host "  claude      Connect Claude Code, e.g. .\make.ps1 claude sk-your-key"
    Write-Host "  help        Show this message"
}

function Assert-Venv {
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        Write-Host "ERROR: .venv not found. Run '.\make.ps1 install' first." -ForegroundColor Red
        exit 1
    }
}

function Assert-Env {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.env'))) {
        Write-Host "ERROR: .env not found. Copy .env.example to .env first." -ForegroundColor Red
        exit 1
    }
}

function Invoke-Native {
    # Run a native command; abort if it exits non-zero. Mirrors make's "first
    # failing recipe line stops the build" — $ErrorActionPreference = 'Stop'
    # does NOT cover native executables (only cmdlets), so check $LASTEXITCODE.
    param([Parameter(Mandatory)][scriptblock]$Cmd)
    & $Cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: command failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

function New-RepoVenv {
    # Create .venv using the first Python on PATH that actually produces an
    # interpreter at $VenvPy. Returns $true on success, $false if none worked.
    #
    # Deliberately "try it and check the result" rather than trusting
    # `Get-Command`: on Windows, `py`/`python` may resolve to a Microsoft
    # Store app-execution-alias stub (a 0-byte shim under
    # %LOCALAPPDATA%\Microsoft\WindowsApps). When no real Python backs it,
    # that stub exits 0 and creates NOTHING, so `Get-Command` finding it —
    # or even the venv command "succeeding" — proves nothing. The only
    # reliable signal is whether .venv\Scripts\python.exe actually appeared.
    # If `py` is such a stub we fall through to `python`/`python3` (e.g. a
    # scoop-installed Python), which is why a student whose `python --version`
    # works can still hit the old failure: the script tried `py` first.
    #
    # The stub also prints "Python was not found" to stderr, which
    # $ErrorActionPreference='Stop' would turn into a terminating error, so
    # each attempt is wrapped in try/catch.
    foreach ($name in 'py', 'python', 'python3') {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { continue }
        Remove-Item -LiteralPath (Join-Path $RepoRoot '.venv') -Recurse -Force -ErrorAction SilentlyContinue
        $venvArgs = if ($name -eq 'py') { @('-3', '-m', 'venv', '.venv') } else { @('-m', 'venv', '.venv') }
        Write-Host "Creating .venv using '$name'..." -ForegroundColor Cyan
        try { & $name @venvArgs 2>&1 | Out-Null } catch { }
        if (Test-Path -LiteralPath $VenvPy) { return $true }
        Write-Host "  '$name' produced no interpreter (likely a Store stub); trying next." -ForegroundColor DarkYellow
    }
    return $false
}

function Test-RealCommand {
    # True if $Name resolves to a real executable, NOT a Microsoft Store
    # 0-byte app-execution-alias stub under WindowsApps (see New-RepoVenv).
    param([string]$Name)
    $c = Get-Command $Name -ErrorAction SilentlyContinue
    return [bool]($c -and $c.Source -notlike '*\Microsoft\WindowsApps\*')
}

function Update-SessionPath {
    # Re-read PATH from the registry so tools scoop just installed are
    # visible in THIS session. scoop runs as a child process and persists
    # PATH to the registry; it cannot mutate our $env:Path directly.
    $parts = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine')
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) | Where-Object { $_ }
    $env:Path = $parts -join ';'
}

function Invoke-Scoop {
    # Run scoop, tolerating the progress it writes to stderr (which
    # $ErrorActionPreference='Stop' would otherwise treat as fatal).
    param([string[]]$ScoopArgs)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & scoop @ScoopArgs 2>&1 | ForEach-Object { Write-Host $_ } }
    catch { Write-Host "  scoop: $_" -ForegroundColor DarkYellow }
    finally { $ErrorActionPreference = $old }
}

function Install-Scoop {
    # Install scoop (per-user, no admin) if missing. Returns $true if usable.
    if (Test-RealCommand 'scoop') { return $true }
    Write-Host "scoop not found - installing it (per-user, no admin)..." -ForegroundColor Cyan
    if ("$(Get-ExecutionPolicy -Scope CurrentUser)" -in @('Restricted', 'AllSigned')) {
        Write-Host "  Setting CurrentUser execution policy to RemoteSigned (scoop needs it)." -ForegroundColor DarkYellow
        try { Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force }
        catch { Write-Host "  Could not set execution policy: $_" -ForegroundColor Red }
    }
    try { Invoke-RestMethod -Uri 'https://get.scoop.sh' | Invoke-Expression }
    catch { Write-Host "  scoop install failed: $_" -ForegroundColor Red; return $false }
    Update-SessionPath
    return (Test-RealCommand 'scoop')
}

function Install-Toolchain {
    # Best-effort: make sure git, gh and python are on PATH before building
    # the venv, installing any that are missing via scoop. Only bootstraps
    # scoop when something is actually missing, so users who already have
    # these (Git for Windows, python.org, winget) are not forced onto scoop.
    # Non-fatal by design - python is still enforced later by New-RepoVenv,
    # and a gh/git hiccup should not block the whole install. Idempotent.
    $need = @()
    if (-not (Test-RealCommand 'git')) { $need += 'git' }
    if (-not (Test-RealCommand 'gh')) { $need += 'gh' }
    if (-not (Test-RealCommand 'python') -and -not (Test-RealCommand 'py')) { $need += 'python' }
    if ($need.Count -eq 0) {
        Write-Host "Toolchain OK: git, gh, python all present." -ForegroundColor Green
        return
    }
    Write-Host ("Missing: {0} - installing via scoop..." -f ($need -join ', ')) -ForegroundColor Cyan
    if (-not (Install-Scoop)) {
        Write-Host "WARNING: scoop unavailable; could not auto-install $($need -join ', ')." -ForegroundColor Yellow
        Write-Host "  Get them from scoop.sh / python.org / cli.github.com if a later step needs them." -ForegroundColor Yellow
        return
    }
    Invoke-Scoop (@('install') + $need)
    Update-SessionPath
    foreach ($pkg in $need) {
        $ok = if ($pkg -eq 'python') { (Test-RealCommand 'python') -or (Test-RealCommand 'py') }
        else { Test-RealCommand $pkg }
        if ($ok) { Write-Host "  installed $pkg" -ForegroundColor Green }
        else { Write-Host "  WARNING: '$pkg' still not on PATH - open a new terminal if a later step needs it." -ForegroundColor Yellow }
    }
}

switch ($Target.ToLower()) {
    'install' {
        Install-Toolchain
        if (-not (New-RepoVenv)) {
            Write-Host "ERROR: Could not create a virtualenv with any Python on PATH." -ForegroundColor Red
            Write-Host "  A Microsoft Store 'python'/'py' stub does not count - it does nothing." -ForegroundColor Red
            Write-Host "  Install Python 3.12+ from https://python.org (tick 'Add python.exe to PATH')," -ForegroundColor Yellow
            Write-Host "  or run 'scoop install python', then open a NEW PowerShell window and re-run." -ForegroundColor Yellow
            exit 1
        }
        Invoke-Native { & $VenvPy -m pip install --upgrade pip }
        Invoke-Native { & $VenvPy -m pip install -r requirements.txt }
    }
    'test' {
        Assert-Venv
        Invoke-Native { & $VenvPy -m pytest tests\ -v }
    }
    'run' {
        Assert-Venv
        Assert-Env
        & $VenvPy run_local.py
    }
    'deploy-pa' {
        Assert-Env
        $deploy = Join-Path $RepoRoot 'scripts\pa_deploy.ps1'
        # pa_deploy.ps1 needs PowerShell 7. If we're on 5.1 but pwsh exists, use it.
        if ($PSVersionTable.PSVersion.Major -lt 7 -and (Get-Command pwsh -ErrorAction SilentlyContinue)) {
            Invoke-Native { & pwsh -NoProfile -File $deploy }
        } else {
            # In-process .ps1 call: its own `exit <code>` propagates the failure.
            & $deploy
        }
    }
    'claude' {
        $connect = Join-Path $RepoRoot 'setup\connect-claude-code.ps1'
        & $connect @Rest
    }
    default { Show-Help }
}
