param(
    [string]$Message = "Update Naz AI Bot",
    [string]$VpsHost = "root@147.45.154.248",
    [string]$VpsPath = "/opt/naz-ai-bot",
    [switch]$SkipCommit,
    [switch]$SkipPrivate,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"

function Run($Command, $ArgsList) {
    Write-Host ">> $Command $($ArgsList -join ' ')" -ForegroundColor Cyan
    & $Command @ArgsList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command $($ArgsList -join ' ')"
    }
}

function Copy-IfExists($Path, $RemoteTarget) {
    if (Test-Path -LiteralPath $Path) {
        Run "scp" @("-r", $Path, $RemoteTarget)
    } else {
        Write-Host "skip missing: $Path" -ForegroundColor DarkGray
    }
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

Write-Host "Naz AI Bot deploy" -ForegroundColor Green

if (-not $SkipCommit) {
    $status = git status --short
    if ($status) {
        Run "git" @("add", ".")
        Run "git" @("commit", "-m", $Message)
    } else {
        Write-Host "git: nothing to commit" -ForegroundColor DarkGray
    }
    Run "git" @("push", "origin", "main")
}

if (-not $SkipPrivate) {
    Copy-IfExists ".env" "$VpsHost`:$VpsPath/.env"
    Copy-IfExists "naz_stories.md" "$VpsHost`:$VpsPath/naz_stories.md"
    Copy-IfExists "monitored_sources.json" "$VpsHost`:$VpsPath/monitored_sources.json"

    if (Test-Path -LiteralPath "content_inbox") {
        Run "ssh" @($VpsHost, "mkdir -p $VpsPath/content_inbox")
        Copy-IfExists "content_inbox\agent_content" "$VpsHost`:$VpsPath/content_inbox/"
    }
}

$remote = @"
set -e
cd $VpsPath
git pull --ff-only
.venv/bin/python -m py_compile main.py controller.py memory.py prompts.py
"@

if (-not $NoRestart) {
    $remote += @"

systemctl restart naz-ai-bot.service
sleep 3
systemctl is-active naz-ai-bot.service
"@
}

$tmp = New-TemporaryFile
$remote = $remote -replace "`r`n", "`n"
$remote = $remote -replace "`r", "`n"
[System.IO.File]::WriteAllText($tmp, $remote, [System.Text.Encoding]::ASCII)
$remoteScript = "/tmp/naz-ai-bot-deploy-$PID.sh"
try {
    Run "scp" @($tmp.FullName, "$VpsHost`:$remoteScript")
    Run "ssh" @($VpsHost, "bash $remoteScript; rc=`$?; rm -f $remoteScript; exit `$rc")
    if ($LASTEXITCODE -ne 0) {
        throw "Remote deploy failed"
    }
} finally {
    Remove-Item -LiteralPath $tmp -Force
}

Write-Host "Deploy done" -ForegroundColor Green
