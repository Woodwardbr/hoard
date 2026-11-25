# Remove Hoard from Windows. Your data is kept unless you set $env:HOARD_PURGE = "1" first.
#
#   powershell -ExecutionPolicy Bypass -c "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; irm https://github.com/Woodwardbr/hoard/releases/latest/download/uninstall.ps1 | iex"

$ErrorActionPreference = "Continue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $bin = (uv tool dir --bin).Trim()
    $hoard = Join-Path $bin "hoard.exe"
    if (Test-Path $hoard) { & $hoard shortcuts --remove }
    uv tool uninstall hoard
}

$data = Join-Path $env:LOCALAPPDATA "hoard"
if ($env:HOARD_PURGE -eq "1") {
    if (Test-Path $data) {
        Remove-Item -Recurse -Force $data
        Write-Host "Deleted your Hoard data in $data"
    }
    Write-Host "Bank keys saved in Windows Credential Manager stay until you delete the 'hoard' entries there."
} else {
    Write-Host "Hoard removed. Your data is still in $data"
}
