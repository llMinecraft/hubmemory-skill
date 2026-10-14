# hubmemory bootstrap for Windows - delegates to bootstrap.py
param([string]$Python = 'python')
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& $Python (Join-Path $ScriptDir 'bootstrap.py') $args
