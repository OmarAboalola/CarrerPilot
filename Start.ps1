param(
    [switch]$Live,
    [ValidateRange(1, 65535)][int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$CareerPilotPython = $null
$CareerPilotPrefix = @()
foreach ($candidate in @('.venv\Scripts\python.exe', '.conda\python.exe')) {
    $candidatePath = Join-Path $PSScriptRoot $candidate
    if (Test-Path -LiteralPath $candidatePath) {
        & $candidatePath -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $CareerPilotPython = $candidatePath; break }
    }
}
if (-not $CareerPilotPython -and (Get-Command py -ErrorAction SilentlyContinue)) {
    foreach ($version in @('-3.12', '-3')) {
        & py $version -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $CareerPilotPython = 'py'; $CareerPilotPrefix = @($version); break }
    }
}
if (-not $CareerPilotPython -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $CareerPilotPython = 'python' }
}
if (-not $CareerPilotPython) { throw 'Install Python 3.12+ (with pip), then run Start.ps1 again.' }
$CareerPilotArguments = @((Join-Path $PSScriptRoot 'run.py'), '--port', "$Port")
if ($Live) { $CareerPilotArguments += '--live' }
& $CareerPilotPython @CareerPilotPrefix @CareerPilotArguments
exit $LASTEXITCODE
