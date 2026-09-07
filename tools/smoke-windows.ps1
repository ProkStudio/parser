$ErrorActionPreference = "Stop"
$msi = (Resolve-Path "dist/Parser-0.2.0-x64.msi").Path
$installed = Join-Path $env:LOCALAPPDATA "Programs/ProkStudio/Parser"
$data = Join-Path $env:RUNNER_TEMP "parser-msi-smoke-020"
$retained = Join-Path $env:LOCALAPPDATA "Parser"
New-Item -ItemType Directory -Force $data, $retained | Out-Null
Set-Content (Join-Path $retained "retain-test.txt") "Synthetic user data; installer must not remove this"
function Run-Msi([string]$operation, [string]$log) {
    $process = Start-Process msiexec.exe -ArgumentList "$operation `"$msi`" /qn /norestart /l*v `"$log`"" -PassThru
    if (-not $process.WaitForExit(180000)) { $process.Kill(); throw "MSI operation timed out" }
    if ($process.ExitCode -notin @(0,3010)) { Get-Content $log -Tail 80; throw "MSI failed: $($process.ExitCode)" }
}
Run-Msi "/i" (Join-Path $env:RUNNER_TEMP "parser-install.log")
try {
    $exe = Join-Path $installed "Parser.exe"
    if (-not (Test-Path $exe)) { throw "Installed executable not found" }
    foreach ($mode in @("--smoke-test", "--window-smoke")) {
        $process = Start-Process $exe -ArgumentList "$mode --data-dir `"$data`"" -PassThru
        if (-not $process.WaitForExit(90000)) { $process.Kill(); throw "Installed application $mode timed out" }
        if ($process.ExitCode -ne 0) { throw "Installed application $mode failed: $($process.ExitCode)" }
    }
    foreach ($name in @("smoke-test.json", "window-smoke.json")) {
        $report = Get-Content (Join-Path $data $name) -Raw | ConvertFrom-Json
        if (-not $report.ok) { throw "Installed application self-test failed: $name" }
        Copy-Item (Join-Path $data $name) (Join-Path "dist" $name)
    }
    $shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Parser/Parser.lnk"
    if (-not (Test-Path $shortcut)) { throw "Start menu shortcut not found" }
} finally {
    Run-Msi "/x" (Join-Path $env:RUNNER_TEMP "parser-uninstall.log")
}
if (Test-Path (Join-Path $installed "Parser.exe")) { throw "Executable remained after uninstall" }
if (-not (Test-Path (Join-Path $retained "retain-test.txt"))) { throw "Uninstaller removed personal data" }
$hash = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
Set-Content -Encoding ascii "dist/SHA256SUMS.txt" "$hash  Parser-0.2.0-x64.msi"
Write-Output "MSI install, native WebView bridge/three tabs, shortcut, uninstall and retained data: PASS"
