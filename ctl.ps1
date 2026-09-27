# Foculet control script: status | stop | start
# Run from a PowerShell prompt, e.g.:
#   .\ctl.ps1 -Action status
param(
    [string]$Action = "status",
    [string]$ExtraArgs = ""
)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

function FoculetProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
        $_.CommandLine -match 'foculet\.py'
    }
}

switch ($Action) {
    "status" {
        $p = @(FoculetProcs)
        if ($p.Count -gt 0) { $p | ForEach-Object { "RUNNING pid=$($_.ProcessId)" } }
        else { "NOT RUNNING" }
    }
    "stop" {
        $p = @(FoculetProcs)
        if ($p.Count -eq 0) { "NOTHING TO STOP" }
        else {
            $p | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force
                "STOPPED pid=$($_.ProcessId)"
            }
        }
    }
    "start" {
        $p = @(FoculetProcs)
        if ($p.Count -gt 0) { "ALREADY RUNNING - stop first"; break }
        Remove-Item "$dir\foculet.log" -ErrorAction SilentlyContinue
        Remove-Item "$dir\foculet.err.log" -ErrorAction SilentlyContinue
        # pythonw = no console window, ever. The tray icon is the UI.
        $pyw = ""
        try {
            $pyexe = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim()
            $cand = Join-Path (Split-Path $pyexe) "pythonw.exe"
            if (Test-Path $cand) { $pyw = $cand }
        } catch {}
        if ($pyw -ne "") {
            $argList = @("$dir\foculet.py")
            if ($ExtraArgs -ne "") { $argList += $ExtraArgs.Split(" ") }
            Start-Process -FilePath $pyw -ArgumentList $argList
        } else {
            # fallback: hidden console window
            $argList = @("-3", "$dir\foculet.py")
            if ($ExtraArgs -ne "") { $argList += $ExtraArgs.Split(" ") }
            Start-Process -FilePath "py" -ArgumentList $argList -WindowStyle Hidden `
                -RedirectStandardOutput "$dir\foculet.log" `
                -RedirectStandardError "$dir\foculet.err.log"
        }
        "STARTED - look for the tray icon"
    }
}
