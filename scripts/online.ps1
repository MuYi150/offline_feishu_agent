# Uses the existing feishu-api environment. Pass all arguments to the online CLI.
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$condaExecutable = if ($env:CONDA_EXE) { $env:CONDA_EXE } elseif (Get-Command conda -ErrorAction SilentlyContinue) { 'conda' } elseif (Test-Path -LiteralPath 'E:\tools\conda\Scripts\conda.exe') { 'E:\tools\conda\Scripts\conda.exe' } else { throw 'Cannot find Conda. Set CONDA_EXE to your existing Conda executable.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $projectDirectory
try {
    & $condaExecutable run --no-capture-output -n feishu-api python -m wiki_review_v2.online @args
    $jobExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $jobExitCode
