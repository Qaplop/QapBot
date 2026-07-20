# Usage:
#   .\run_tests.ps1              — fast local tests only (skips @pytest.mark.live)
#   .\run_tests.ps1 -Full        — all tests including live Discord smoke tests
#   .\run_tests.ps1 -k my_test   — pass extra pytest filters (still skips live)
#   .\run_tests.ps1 -Full -k foo — full run with additional filter
param(
    [switch]$Full,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$ExtraArgs
)

$markFilter = if ($Full) { @() } else { @("-m", "not live") }

## Use script-local paths so absolute paths are not committed to the repo
$pythonExe = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }
$testsDir = Join-Path $PSScriptRoot 'tests'

& $pythonExe -m pytest $testsDir -x -q --tb=short `
    --deselect "tests/unit/test_war_file_lifecycle.py::TestLoadAllTempWarStats::test_loads_from_json" `
    --deselect "tests/unit/test_db_errors_phase5.py::TestLoadAllTempWarStats::test_loads_from_json" `
    @markFilter `
    @ExtraArgs
