param(
    [string]$SshHost = 'dana',
    [string]$Container = 'parkingplan_mongo',
    [int]$LocalPort = 27018,
    [switch]$Reconnect
)
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[A-Za-z0-9._-]+$' -or $Container -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Use a simple SSH alias and container name.'
}
$taskRemoteCommand = 'docker inspect --format ''{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'' ' + $Container
$taskAddresses = & ssh -o BatchMode=yes -o ConnectTimeout=10 $SshHost $taskRemoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Unable to discover MongoDB container address.' }
$taskAddress = ($taskAddresses.Trim() -split '\s+')[0]
if ($taskAddress -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'No valid IPv4 container address returned.' }
Write-Host "Forwarding localhost:$LocalPort through $SshHost. Keep this terminal open; Ctrl+C stops it."
$taskAttempt = 0
do {
    & ssh -N -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L "127.0.0.1:${LocalPort}:${taskAddress}:27017" $SshHost
    $taskAttempt++
    if (-not $Reconnect -or $LASTEXITCODE -eq 0 -or $taskAttempt -ge 5) { break }
    Write-Host "SSH disconnected; reconnecting ($taskAttempt/5)."
    Start-Sleep -Seconds 2
} while ($true)
