# Install or update Hoard on Windows.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://github.com/Woodwardbr/hoard/releases/latest/download/install.ps1 | iex"
#
# Installs uv (https://docs.astral.sh/uv/) if needed, then the latest Hoard release
# with its own Python, and adds Hoard to the Desktop and Start menu (plus an
# "Update Hoard" shortcut that runs this again). Your data is kept.
# Set $env:HOARD_VERSION = "v0.1.0" first to install a specific release.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "Woodwardbr/hoard"

function Say($msg) { Write-Host $msg -ForegroundColor Cyan }

try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say "Installing uv (the tool that installs Hoard and its Python)..."
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv was installed but can't be found. Close this window, open a new one and run the installer again."
        }
    }

    if ($env:HOARD_WHEEL) {
        $wheel = $env:HOARD_WHEEL  # testing: install a locally built wheel
        $tag = "(local build)"
    } else {
        if ($env:HOARD_VERSION) {
            $api = "https://api.github.com/repos/$Repo/releases/tags/$env:HOARD_VERSION"
        } else {
            $api = "https://api.github.com/repos/$Repo/releases/latest"
        }
        Say "Finding the latest Hoard release..."
        $release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "hoard-installer" }
        $wheel = ($release.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1).browser_download_url
        if (-not $wheel) { throw "Couldn't find a Hoard release to download from $api" }
        $tag = $release.tag_name
    }

    Say "Installing Hoard $tag..."
    uv tool install --force --python ">=3.13" $wheel
    if ($LASTEXITCODE -ne 0) { throw "uv couldn't install Hoard (exit code $LASTEXITCODE)." }

    $bin = (uv tool dir --bin).Trim()
    Say "Adding Hoard to the Desktop and Start menu..."
    & (Join-Path $bin "hoard.exe") shortcuts
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create the shortcuts (exit code $LASTEXITCODE)." }

    Write-Host ""
    Write-Host "Done! Open Hoard from the Desktop or the Start menu." -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "Hoard couldn't be installed: $($_.Exception.Message)" -ForegroundColor Red
}

# When started from the "Update Hoard" shortcut, keep the window open to read.
if ($Host.Name -eq "ConsoleHost" -and -not $env:CI) {
    Write-Host ""
    Read-Host "Press Enter to close"
}
