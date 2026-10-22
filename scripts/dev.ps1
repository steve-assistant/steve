# Common development tasks. Usage:  .\scripts\dev.ps1 <task>
param([Parameter(Position = 0)][string]$Task = "check")

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Py = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

switch ($Task) {
    "check"   { & $Py -m ruff check .; if ($?) { & $Py -m pytest -q } }
    "test"    { & $Py -m pytest -q }
    "gate"    { & $Py -m pytest tests/gate -q }
    "lint"    { & $Py -m ruff check . }
    "fmt"     { & $Py -m ruff check --fix .; & $Py -m ruff format . }
    "run"     { & $Py -m core.supervisor }
    "frozen"  { & $Py -m core.policy }
    "relock"  { & $Py -m core.policy --write-lock }
    "secrets" { & $Py -m core.secrets status }
    "night"   { & $Py -m agent.night }
    "relay"   { & $Py -m relay.server --port 8080 }
    "token"   { & $Py -c "from agent.link import new_token; print(new_token())" }
    "signal"  { & $Py -m channels.signal status }
    "link"    { & $Py -m channels.signal link }
    "db"      { & $Py -m core.db }
    default   {
        Write-Output "tasks: check test gate lint fmt run frozen relock secrets db"
        exit 1
    }
}
