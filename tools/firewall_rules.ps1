# Run as Administrator. Lets PCs and phones on the SAME local network reach WorkLog
# (TCP 5000 = website/agent, UDP 5001 = agent auto-discovery). Nothing outside your
# local network (internet) can use these rules.
$ErrorActionPreference = 'Stop'
foreach ($n in 'WorkLog Server', 'WorkLog Server (web)', 'WorkLog Server (discovery)') {
    Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}
New-NetFirewallRule -DisplayName 'WorkLog Server (web)' -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort 5000 -RemoteAddress LocalSubnet -Profile Any | Out-Null
New-NetFirewallRule -DisplayName 'WorkLog Server (discovery)' -Direction Inbound -Action Allow -Protocol UDP `
    -LocalPort 5001 -RemoteAddress LocalSubnet -Profile Any | Out-Null
Write-Host 'Firewall rules added for WorkLog (local network only).' -ForegroundColor Green
Start-Sleep 3
