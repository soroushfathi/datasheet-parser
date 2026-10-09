param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [string]$Output = 'outputs/result.json'
)
$ErrorActionPreference = 'Stop'
# Launch from the project root. Run only one worker per RunId.
& uv run datasheet resume $RunId
if ($LASTEXITCODE -ne 0) { throw 'Extraction stopped; completed stages are preserved. Inspect its logs before resuming.' }
& uv run datasheet export $RunId $Output
if ($LASTEXITCODE -ne 0) { throw 'Final export failed; inspect the validation error.' }
