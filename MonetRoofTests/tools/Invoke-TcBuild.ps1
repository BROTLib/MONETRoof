<#
.SYNOPSIS
  Runs TcBuild.exe under the machine-wide TwinCAT lock, retrying while XAE is busy.

.DESCRIPTION
  TcBuild drives XAE over COM, which is shared by CI jobs, the test scripts (Run-Tests.ps1, Install-TcUnit.ps1)
  and local use. A second user fails with RPC_E_SERVERCALL_RETRYLATER (exit 3). This takes the same named mutex
  as those scripts ('Global\BROT-TwinCAT-UmRT'), so a build waits for a running test instead of colliding with it.
  The mutex is released when the script ends, and taken over if the previous holder died. Exit 3 is still retried
  as a fallback for an XAE that something outside this lock holds (for example one the user has open).

  Usage:  Invoke-TcBuild.ps1 build MonetRoofTests.sln
          Invoke-TcBuild.ps1 install MonetRoof.sln -x MONETroof -p MonetRoof -l $file

  Exit code: TcBuild's own (0 ok, 1 built with warnings = success, 2 compile errors, 3 still busy after the
  retries), or 6 if the lock could not be taken in time (4/5 are TcBuild's own). The caller decides what counts as a failure.
#>
# No param block on purpose: every argument goes to TcBuild unchanged. A param block would capture TcBuild's own
# -l / -x / -p (PowerShell matches parameter name prefixes). The overrides below exist for testing the script.
$TcBuildPath         = if ($env:BROT_TCBUILD_PATH) { $env:BROT_TCBUILD_PATH } else { 'C:\Program Files\Industrial Brains B.V\TcBuild\TcBuild.exe' }
$LockTimeoutMinutes  = if ($env:BROT_TCBUILD_LOCK_MIN) { [int]$env:BROT_TCBUILD_LOCK_MIN } else { 60 }
$Tries               = 4
$RetryDelaySeconds   = if ($env:BROT_TCBUILD_RETRY_S) { [int]$env:BROT_TCBUILD_RETRY_S } else { 30 }
$ErrorActionPreference = 'Stop'
$lock = New-Object System.Threading.Mutex($false, 'Global\BROT-TwinCAT-UmRT')
$locked = $false
try { $locked = $lock.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
if (-not $locked) {
    Write-Host "Another TwinCAT job is in progress, waiting up to $LockTimeoutMinutes min for it to finish..."
    try { $locked = $lock.WaitOne([TimeSpan]::FromMinutes($LockTimeoutMinutes)) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
}
if (-not $locked) {
    Write-Host "Another TwinCAT job still held the lock after $LockTimeoutMinutes min."
    exit 6
}
try {
    for ($i = 1; $i -le $Tries; $i++) {
        & $TcBuildPath @args
        if ($LASTEXITCODE -ne 3) { break }
        if ($i -lt $Tries) { Start-Sleep -Seconds $RetryDelaySeconds }
    }
    exit $LASTEXITCODE
}
finally { $lock.ReleaseMutex(); $lock.Dispose() }
