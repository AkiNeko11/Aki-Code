[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Before', 'Start', 'After', 'Check', 'Where')]
    [string]$Action = 'Start',
    [switch]$CopyTask
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$starter = Join-Path $PSScriptRoot 'starter'
$runBase = Join-Path $repoRoot '.task_outputs/demos/markdown-link-checker'
$currentFile = Join-Path $runBase 'current.json'
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'

function Get-DemoFileHash {
    param([string]$Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Assert-PlainPath {
    param([string]$Path)
    $probe = [IO.Path]::GetFullPath($Path)
    while ($probe) {
        if (Test-Path -LiteralPath $probe) {
            $item = Get-Item -LiteralPath $probe -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not supported: $probe"
            }
        }
        $parent = [IO.Directory]::GetParent($probe)
        if ($null -eq $parent) { break }
        $probe = $parent.FullName
    }
}

function Assert-PlainTree {
    param([string]$Directory)
    Assert-PlainPath $Directory
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Directory)
    while ($pending.Count -gt 0) {
        $folder = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $folder -Force) {
            if ($item.Name -in @('.test-tmp', '__pycache__')) { continue }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse points are not supported: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}

function Copy-StarterTree {
    param([string]$Source, [string]$Destination)
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        if ($item.Name -in @('.test-tmp', '__pycache__')) { continue }
        $target = Join-Path $Destination $item.Name
        if ($item.PSIsContainer) {
            $null = New-Item -ItemType Directory -Path $target
            Copy-StarterTree -Source $item.FullName -Destination $target
        }
        else {
            Copy-Item -LiteralPath $item.FullName -Destination $target
        }
    }
}

function Invoke-DemoCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$Display, [ref]$Transcript)
    # PowerShell 7 can turn an expected native exit code into a terminating error.
    $PSNativeCommandUseErrorActionPreference = $false
    if (-not $Display) { $Display = ((@($Executable) + @($Arguments)) -join ' ') }
    Write-Host ('> ' + $Display) -ForegroundColor Cyan
    if ($null -ne $Transcript) {
        $lines = New-Object 'System.Collections.Generic.List[string]'
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $Executable @Arguments 2>&1 | ForEach-Object {
                # Windows PowerShell wraps stderr lines (including blank lines).
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $line = [string]$_.Exception.Message
                }
                else { $line = [string]$_ }
                $lines.Add($line)
                Write-Host $line
            }
            $code = $LASTEXITCODE
        }
        finally { $ErrorActionPreference = $previousPreference }
        $Transcript.Value = $lines -join "`n"
        return $code
    }
    & $Executable @Arguments | Out-Host
    return $LASTEXITCODE
}

function Invoke-DemoGit {
    param([string]$Workspace, [string[]]$Arguments)
    # An inherited GIT_DIR or GIT_INDEX_FILE must never redirect a demo mutation.
    $savedGit = @{}
    foreach ($entry in Get-ChildItem Env:) {
        if ($entry.Name.StartsWith('GIT_', [StringComparison]::OrdinalIgnoreCase)) {
            $savedGit[$entry.Name] = $entry.Value
        }
    }
    try {
        foreach ($name in @($savedGit.Keys)) {
            Remove-Item -LiteralPath ('Env:' + $name)
        }
        $gitArgs = @(
            '-c', 'user.name=Aki-Code Demo',
            '-c', 'user.email=demo@example.invalid',
            '-c', 'commit.gpgsign=false',
            '-c', 'core.fsmonitor=false',
            '-c', 'core.autocrlf=false',
            '-c', 'core.pager=cat',
            '-c', ('core.hooksPath=' + (Join-Path $Workspace '.git/info/no-hooks')),
            '-c', 'init.templateDir=',
            '-C', $Workspace
        ) + @($Arguments)
        $code = Invoke-DemoCommand -Executable 'git' -Arguments $gitArgs -Display ('git ' + ($Arguments -join ' '))
        if ($code -ne 0) { throw "Demo git command failed with exit code $code." }
    }
    finally {
        foreach ($name in @($savedGit.Keys)) {
            [Environment]::SetEnvironmentVariable($name, $savedGit[$name], 'Process')
        }
    }
}

function Get-CurrentRun {
    Assert-PlainPath $currentFile
    if (-not (Test-Path -LiteralPath $currentFile -PathType Leaf)) {
        throw 'No prepared workspace. Run this script with -Action Prepare first.'
    }
    $run = [IO.File]::ReadAllText($currentFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $workspacePath = [IO.Path]::GetFullPath([string]$run.workspace)
    $parentPath = [IO.Path]::GetDirectoryName($workspacePath)
    if (($parentPath -ne [IO.Path]::GetFullPath($runBase)) -or
        (-not [IO.Path]::GetFileName($workspacePath).StartsWith('run-'))) {
        throw 'The saved workspace is outside the demo run directory.'
    }
    Assert-PlainPath $workspacePath
    if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) {
        throw "The saved workspace no longer exists: $workspacePath"
    }
    Assert-PlainPath (Join-Path $workspacePath '.git')
    if (-not (Test-Path -LiteralPath (Join-Path $workspacePath '.git') -PathType Container)) {
        throw 'The demo workspace must have its own local Git repository.'
    }
    return $run
}

function Invoke-InDemoEnvironment {
    param([string]$Workspace, [scriptblock]$Body)
    $stateRoot = Join-Path $Workspace '.aki-demo-state'
    Assert-PlainPath $stateRoot
    $tempRoot = Join-Path $stateRoot 'tmp'
    Assert-PlainPath $tempRoot
    $null = New-Item -ItemType Directory -Path $tempRoot -Force
    $settings = @{
        'PYTHONPATH' = $repoRoot
        'PATH' = (Join-Path $repoRoot '.venv/Scripts') + [IO.Path]::PathSeparator + $env:PATH
        'PYTHONUTF8' = '1'
        'PYTHONIOENCODING' = 'utf-8'
        'PYTHONDONTWRITEBYTECODE' = '1'
        'PYTHONUNBUFFERED' = '1'
        'PYTHON_DOTENV_DISABLED' = '0'
        'TEMP' = $tempRoot
        'TMP' = $tempRoot
        'TMPDIR' = $tempRoot
        'SKILLS_DIR' = (Join-Path $repoRoot 'skills')
        'MEMORY_DIR' = (Join-Path $stateRoot 'memory')
        'TASKS_DIR' = (Join-Path $stateRoot 'tasks')
        'SCHEDULES_FILE' = (Join-Path $stateRoot 'schedules.json')
        'MAILBOX_DIR' = (Join-Path $stateRoot 'mailboxes')
        'WORKTREES_DIR' = (Join-Path $stateRoot 'worktrees')
        'WORKFLOWS_DIR' = (Join-Path $stateRoot 'workflows')
        'TRANSCRIPTS_DIR' = (Join-Path $stateRoot 'transcripts')
        'TOOL_OUTPUTS_DIR' = (Join-Path $stateRoot 'tool-results')
    }
    $previous = @{}
    foreach ($name in $settings.Keys) {
        $previous[$name] = @{
            exists = (Test-Path -LiteralPath ('Env:' + $name))
            value = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
    }
    $previousInputEncoding = [Console]::InputEncoding
    $previousConsoleEncoding = [Console]::OutputEncoding
    $previousOutputEncoding = $OutputEncoding
    $pushed = $false
    try {
        foreach ($name in $settings.Keys) {
            [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [Console]::InputEncoding = $utf8
        [Console]::OutputEncoding = $utf8
        $OutputEncoding = $utf8
        Push-Location -LiteralPath $Workspace
        $pushed = $true
        & $Body
    }
    finally {
        if ($pushed) { Pop-Location }
        foreach ($name in $previous.Keys) {
            if ($previous[$name].exists) {
                [Environment]::SetEnvironmentVariable($name, $previous[$name].value, 'Process')
            }
            else {
                Remove-Item -LiteralPath ('Env:' + $name) -ErrorAction SilentlyContinue
            }
        }
        [Console]::InputEncoding = $previousInputEncoding
        [Console]::OutputEncoding = $previousConsoleEncoding
        $OutputEncoding = $previousOutputEncoding
    }
}

if ($CopyTask -and $Action -ne 'Start') {
    throw '-CopyTask can only be used with -Action Start.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create the repository .venv and install requirements.txt before using this demo.'
}

if ($Action -eq 'Prepare') {
    if (-not (Test-Path -LiteralPath $starter -PathType Container)) {
        throw "The starter project is missing: $starter"
    }
    Assert-PlainTree $starter
    Assert-PlainPath $runBase
    Assert-PlainPath $currentFile
    $null = New-Item -ItemType Directory -Path $runBase -Force
    $runName = 'run-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $workspace = Join-Path $runBase $runName
    $null = New-Item -ItemType Directory -Path $workspace
    Copy-StarterTree -Source $starter -Destination $workspace
    $hashes = [ordered]@{}
    foreach ($file in Get-ChildItem -LiteralPath $workspace -Recurse -File -Force) {
        $relative = $file.FullName.Substring($workspace.Length).TrimStart([char[]]'\/').Replace([IO.Path]::DirectorySeparatorChar, [char]'/')
        if (($file.Extension -eq '.md') -or ($relative -eq 'tests/test_checker.py')) {
            $hashes[$relative] = Get-DemoFileHash -Path $file.FullName
        }
    }
    if (-not $hashes.Contains('tests/test_checker.py')) {
        throw 'The starter must include tests/test_checker.py.'
    }
    Invoke-DemoGit -Workspace $workspace -Arguments @('init', '--quiet')
    $null = New-Item -ItemType Directory -Path (Join-Path $workspace '.git/info') -Force
    $excludePath = Join-Path $workspace '.git/info/exclude'
    $excludeText = "/.aki-demo-state/`n__pycache__/`n*.pyc`n"
    [IO.File]::WriteAllText($excludePath, $excludeText, (New-Object Text.UTF8Encoding($false)))
    Invoke-DemoGit -Workspace $workspace -Arguments @('add', '--', '.')
    Invoke-DemoGit -Workspace $workspace -Arguments @('commit', '--quiet', '-m', 'Demo baseline: reproduce relative Markdown link bug')
    $record = [ordered]@{
        schema = 1
        workspace = $workspace
        created_utc = [DateTime]::UtcNow.ToString('o')
        baseline_sha256 = $hashes
    }
    [IO.File]::WriteAllText($currentFile, ($record | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Prepared workspace: $workspace" -ForegroundColor Green
    Write-Host 'Previous runs are preserved. Next: -Action Before, then -Action Start.'
    return
}

$run = Get-CurrentRun
$workspace = [string]$run.workspace

if ($Action -eq 'Where') {
    Write-Host "Workspace: $workspace"
    Write-Host "Current run record: $currentFile"
    return
}

if ($Action -eq 'Before' -or $Action -eq 'After') {
    if ($Action -eq 'After') {
        foreach ($entry in $run.baseline_sha256.PSObject.Properties) {
            $fixture = Join-Path $workspace $entry.Name
            Assert-PlainPath $fixture
            if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
                throw "An original fixture was removed: $($entry.Name)"
            }
            if ((Get-DemoFileHash -Path $fixture) -ne $entry.Value) {
                throw "An original fixture was changed: $($entry.Name). Restore it before validation."
            }
        }
        Write-Host 'Original tests and Markdown fixtures are unchanged.' -ForegroundColor Green
    }
    Invoke-InDemoEnvironment -Workspace $workspace -Body {
        $cliCode = Invoke-DemoCommand -Executable 'python' -Arguments @('-B', '-m', 'linkcheck', 'docs/guide.md')
        $testOutput = ''
        $testCode = Invoke-DemoCommand -Executable 'python' -Arguments @('-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v') -Transcript ([ref]$testOutput)
        $expectedCode = 1
        if ($Action -eq 'After') {
            $expectedCode = 0
            Invoke-DemoGit -Workspace $workspace -Arguments @('status', '--short')
            Invoke-DemoGit -Workspace $workspace -Arguments @('diff', '--no-ext-diff', '--no-color')
        }
        if (($cliCode -ne $expectedCode) -or ($testCode -ne $expectedCode)) {
            throw "Expected both exit codes to be $expectedCode; CLI=$cliCode, tests=$testCode."
        }
        if ($Action -eq 'Before') {
            if (($testOutput -notmatch '(?m)^Ran 8 tests in ') -or
                ($testOutput -notmatch '(?m)^FAILED \(failures=3\)\s*$')) {
                throw 'Expected exactly 8 tests with 3 assertion failures and no test errors. Review the output above.'
            }
            Write-Host 'The original bug is reproduced (8 tests, 3 assertion failures).' -ForegroundColor Yellow
        }
        else {
            if ($testOutput -notmatch '(?m)^OK\s*$') {
                throw 'The test runner did not report OK. Review the output above.'
            }
            Write-Host 'Validation passed (both exit codes are 0).' -ForegroundColor Green
        }
    }
    return
}

if ($Action -eq 'Check') {
    Invoke-InDemoEnvironment -Workspace $workspace -Body {
        $checkCode = @'
import importlib.util
import os
from pathlib import Path
import sys
from dotenv import load_dotenv

repo, workspace = (Path(value).resolve() for value in sys.argv[1:3])
load_dotenv(repo / '.env', override=False)
from aki_code import config

assert Path.cwd().resolve() == workspace, 'Incorrect demo working directory'
spec = importlib.util.find_spec('aki_code.main')
assert spec and Path(spec.origin).resolve() == repo / 'aki_code' / 'main.py', 'Incorrect package source'
assert Path(config.SKILLS_DIR).resolve() == repo / 'skills', 'Incorrect skills directory'
state = workspace / '.aki-demo-state'
for key, name in {
    'MEMORY_DIR': 'memory', 'TASKS_DIR': 'tasks', 'SCHEDULES_FILE': 'schedules.json',
    'MAILBOX_DIR': 'mailboxes', 'WORKTREES_DIR': 'worktrees', 'WORKFLOWS_DIR': 'workflows',
    'TRANSCRIPTS_DIR': 'transcripts', 'TOOL_OUTPUTS_DIR': 'tool-results',
}.items():
    assert Path(getattr(config, key)).resolve() == state / name, 'Incorrect state path: ' + key
assert Path(sys.executable).resolve() == repo / '.venv' / 'Scripts' / 'python.exe', 'Incorrect interpreter'
assert sys.flags.utf8_mode, 'Python UTF-8 mode is disabled'
for key in ('TEMP', 'TMP', 'TMPDIR'):
    assert Path(os.environ[key]).resolve() == state / 'tmp', 'Incorrect temporary directory: ' + key
print('Offline checks passed: source imports, workspace, skills, private state, Python, UTF-8.')
print('No AgentRuntime was initialized and no model request was made.')
'@
        & $python -X utf8 -B -c $checkCode $repoRoot $workspace
        if ($LASTEXITCODE -ne 0) { throw 'Offline launcher validation failed.' }
    }
    return
}

if ($Action -eq 'Start') {
    if ($CopyTask) {
        $taskFile = Join-Path $PSScriptRoot 'task.txt'
        Assert-PlainPath $taskFile
        $task = [IO.File]::ReadAllText($taskFile, [Text.Encoding]::UTF8).Trim()
        if ($task.Contains("`n") -or $task.Contains("`r")) {
            throw 'task.txt must contain a single-line prompt for the interactive terminal.'
        }
        $clipboardCommand = Get-Command -Name 'Set-Clipboard' -ErrorAction SilentlyContinue
        if ($null -eq $clipboardCommand) {
            Write-Host "Clipboard command is unavailable. Open $taskFile and paste its single-line prompt after startup." -ForegroundColor Yellow
        }
        else {
            try {
                & $clipboardCommand -Value $task
                Write-Host 'The task prompt was copied to the clipboard.'
            }
            catch {
                Write-Host "Could not copy the task. Open $taskFile and paste its single-line prompt after startup." -ForegroundColor Yellow
            }
        }
    }
    Invoke-InDemoEnvironment -Workspace $workspace -Body {
        $startCode = @'
from pathlib import Path
import runpy
import sys
from dotenv import load_dotenv

load_dotenv(Path(sys.argv[1]) / '.env', override=False)
sys.argv = ['aki_code.main']
runpy.run_module('aki_code.main', run_name='__main__', alter_sys=True)
'@
        Write-Host "Workspace: $workspace" -ForegroundColor Cyan
        Write-Host 'Starting the interactive Agent. Submitted tasks use your configured model API.'
        & $python -X utf8 -B -c $startCode $repoRoot
        if ($LASTEXITCODE -ne 0) { throw "The interactive Agent exited with code $LASTEXITCODE." }
    }
}
