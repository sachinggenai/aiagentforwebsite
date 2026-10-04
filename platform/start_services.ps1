<#
.SYNOPSIS
    Start the ANITA MEDICAL SYSTEMS platform: preflight, tunnel, API, Twilio.

.DESCRIPTION
    Brings up one instance in the order the platform actually requires:

        preflight -> kill stale -> tunnel -> write TUNNEL_HOST -> API -> Twilio

    **The tunnel comes before the API, and that ordering is not cosmetic.**
    Signature validation is computed against `https://<TUNNEL_HOST>`, read once
    at boot (assembly.py). A Cloudflare *quick* tunnel mints a new hostname on
    every restart, so writing TUNNEL_HOST and then starting the API is the only
    order in which the instance validates against the URL Twilio will call.
    Start the API first and every webhook is refused — the call connects and
    then dies, which looks like a carrier fault.

    Re-running IS the restart path: it kills stale holders on the ANITA ports
    first, so it is idempotent. There is no -Stop mode; Ctrl+C leaves the
    children running in their own hidden consoles, and re-running cleans up.

.PARAMETER SkipTwilio
    Do not re-point the Twilio number's webhooks at the new tunnel hostname.
    Use when you are not placing calls.

.PARAMETER NamedTunnel
    Use a stable named tunnel instead of a quick one, so the hostname survives
    a restart and Twilio does not need re-pointing. Requires `cloudflared
    tunnel login` to have been run once.

.PARAMETER TunnelName
    Name of the named tunnel. Default "anita-tunnel".

.PARAMETER WithStreamlit
    Also start the Streamlit surface on ANITA_STREAMLIT_PORT.

.PARAMETER SkipPreflight
    Skip the isolation preflight. Development only — the preflight is what
    refuses a boot that would share a database, collection or port with the
    sibling product.

.EXAMPLE
    .\start_services.ps1
    Full start: tunnel, API, Twilio webhooks re-pointed.

.EXAMPLE
    .\start_services.ps1 -NamedTunnel -SkipTwilio
    Stable hostname, no call routing (offline development).
#>

param(
    [switch]$SkipTwilio = $false,
    [switch]$NamedTunnel = $false,
    [string]$TunnelName = "anita-tunnel",
    [switch]$WithStreamlit = $false,
    [switch]$SkipPreflight = $false
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
$ProjectRoot = $PSScriptRoot
$EnvFile     = Join-Path $ProjectRoot ".env"
$LogDir      = Join-Path $ProjectRoot "logs"
$PythonExe   = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$StreamlitExe = Join-Path $ProjectRoot ".venv\Scripts\streamlit.exe"

$ESC = [char]27
$BOLD = "$ESC[1m"; $DIM = "$ESC[2m"; $GREEN = "$ESC[32m"; $YELLOW = "$ESC[33m"
$RED = "$ESC[31m"; $CYAN = "$ESC[36m"; $RESET = "$ESC[0m"

function Write-Step { Write-Host ("{0}{1}--- {2} ---{3}" -f "`n", $CYAN, ($args -join ' '), $RESET) }
function Write-OK   { Write-Host ("{0}  OK: {1}{2}" -f $GREEN, ($args -join ' '), $RESET) }
function Write-Warn { Write-Host ("{0}  WARN: {1}{2}" -f $YELLOW, ($args -join ' '), $RESET) }
function Write-Err  { Write-Host ("{0}  ERROR: {1}{2}" -f $RED, ($args -join ' '), $RESET) }

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

# ---------------------------------------------------------------------------
# .env helpers
#
# The platform loads .env itself (config._environment), so this script reads it
# only to learn the ports and to WRITE TUNNEL_HOST back. It is never the source
# of truth for the services.
# ---------------------------------------------------------------------------
function Get-DotEnvValue {
    param([string]$Key, [string]$Default = "")
    if (-not (Test-Path $EnvFile)) { return $Default }
    foreach ($line in (Get-Content $EnvFile -ErrorAction SilentlyContinue)) {
        $s = $line.Trim()
        if (-not $s -or $s.StartsWith('#') -or -not $s.Contains('=')) { continue }
        $i = $s.IndexOf('=')
        if ($s.Substring(0, $i).Trim() -ne $Key) { continue }
        $v = $s.Substring($i + 1).Trim().Trim('"').Trim("'")
        if ($v) { return $v }
        return $Default
    }
    return $Default
}

function Get-DotEnvPort {
    param([string]$Key, [int]$Default)
    $raw = Get-DotEnvValue -Key $Key -Default ""
    $parsed = 0
    if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -gt 0 -and $parsed -lt 65536) { return $parsed }
    if ($raw) { Write-Warn "$Key='$raw' is not a valid port; using $Default" }
    return $Default
}

function Set-DotEnvValue {
    <#
      Rewrite one key in .env, preserving every other line byte-for-byte.

      TUNNEL_HOST changes on every quick-tunnel restart, so this runs often and
      must not disturb the credentials sitting in the same file.
    #>
    param([string]$Key, [string]$Value)

    $lines = @(Get-Content $EnvFile -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { $lines = @("# ANITA platform configuration") }

    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $s = $lines[$i].Trim()
        if ($s.StartsWith('#') -or -not $s.Contains('=')) { continue }
        $eq = $s.IndexOf('=')
        if ($s.Substring(0, $eq).Trim() -eq $Key) {
            $lines[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) {
        $lines += ""
        $lines += "# ---- Public ingress (written by start_services.ps1) ----"
        $lines += "$Key=$Value"
    }
    Set-Content -Path $EnvFile -Value $lines -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------
function Stop-PortOwner {
    <#
      Free a port, including the case the naive kill misses.

      A uvicorn supervisor hands its listening socket to a worker and then
      exits, so Get-NetTCPConnection reports a PID that no longer exists while
      the socket is still held by a descendant. Killing the reported PID alone
      silently fails and the next start hits Errno 10048.
    #>
    param([int]$Port)

    foreach ($round in 1..3) {
        $connections = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        $owners = @($connections.OwningProcess | Select-Object -Unique | Where-Object { $_ -gt 0 })
        if ($owners.Count -eq 0) { return $true }

        $killed = $false
        foreach ($procId in $owners) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                try {
                    $proc | Stop-Process -Force -ErrorAction Stop
                    Write-OK ("Killed {0} (PID {1}) on port {2}" -f $proc.ProcessName, $procId, $Port)
                    $killed = $true
                } catch {
                    Write-Warn ("Could not kill PID {0} on port {1}: {2}" -f $procId, $Port, $_.Exception.Message)
                }
                continue
            }

            Write-Warn ("Port {0} held by PID {1}, which no longer exists - socket inherited" -f $Port, $procId)
            $killable = @()
            $frontier = @($procId)
            for ($depth = 0; $depth -lt 4 -and $frontier.Count -gt 0; $depth++) {
                $filter = ($frontier | ForEach-Object { "ParentProcessId=$_" }) -join " or "
                $children = @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)
                if ($children.Count -eq 0) { break }
                foreach ($child in $children) {
                    if (-not (Get-Process -Id $child.ParentProcessId -ErrorAction SilentlyContinue)) {
                        $killable += $child
                    }
                }
                $frontier = @($children.ProcessId)
            }

            if ($killable.Count -eq 0) {
                Write-Warn ("  No orphaned holder found for port {0} - it may belong to another session" -f $Port)
                continue
            }
            foreach ($child in $killable) {
                cmd /c "taskkill /F /T /PID $($child.ProcessId) 2>NUL" | Out-Null
                Write-OK ("Killed inherited-socket holder {0} (PID {1}) on port {2}" -f $child.Name, $child.ProcessId, $Port)
                $killed = $true
            }
        }

        Start-Sleep -Seconds 1
        if (-not (Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue)) { return $true }
        if (-not $killed) { break }
    }
    Write-Warn ("Port {0} is STILL busy" -f $Port)
    return $false
}

function Find-CloudflaredExe {
    $onPath = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\cloudflared\cloudflared.exe")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Wait-Http {
    param([string]$Url, [int]$Attempts = 30, [int]$DelaySeconds = 2)
    for ($i = 1; $i -le $Attempts; $i++) {
        $code = curl.exe -s -o NUL -w "%{http_code}" $Url 2>$null
        if ($code -eq "200") { return $true }
        Start-Sleep -Seconds $DelaySeconds
    }
    return $false
}

# ---------------------------------------------------------------------------
# Resolve ports and paths
# ---------------------------------------------------------------------------
$ApiPort       = Get-DotEnvPort -Key "ANITA_API_PORT" -Default 8100
$StreamlitPort = Get-DotEnvPort -Key "ANITA_STREAMLIT_PORT" -Default 8085
$ServeScript   = Join-Path $ProjectRoot "scripts\serve.py"

Write-Host ""
Write-Host ("{0}{1}ANITA MEDICAL SYSTEMS - platform start{2}" -f $BOLD, $CYAN, $RESET)
Write-Host ("  root : {0}" -f $ProjectRoot)
Write-Host ("  api  : {0}" -f $ApiPort)
if ($WithStreamlit) { Write-Host ("  chat : {0}" -f $StreamlitPort) }

if (-not (Test-Path $PythonExe)) {
    Write-Err ("Python not found at {0}" -f $PythonExe)
    Write-Err "Create the venv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

# ---------------------------------------------------------------------------
# Step 1 - Preflight
#
# ANITA's signature gate. It refuses a boot that would share a database,
# collection or port with the sibling product, and it fails before any socket
# opens. Starting services around it would produce a stack that silently serves
# another product's data, which is the one failure worth refusing outright.
# ---------------------------------------------------------------------------
Write-Step "Step 1/8 - Preflight (config, isolation, corpus)"

if ($SkipPreflight) {
    Write-Warn "Preflight SKIPPED (-SkipPreflight) - development only"
} else {
    Push-Location $ProjectRoot
    try {
        # `cmd /c` with the merge INSIDE the string, not `& $py ... 2>&1`.
        # PowerShell 5.1 wraps each stderr line of a native command in an
        # ErrorRecord, and with $ErrorActionPreference = "Stop" that becomes a
        # terminating error -- so a service logging to stderr (which every
        # uvicorn/httpx logger does) would abort this script at Step 1 even
        # though the command succeeded.
        $preflight = cmd /c "`"$PythonExe`" `"$ServeScript`" --check 2>&1"
        $preflightExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($preflightExit -ne 0) {
        Write-Err "Preflight REFUSED - not starting."
        $preflight | ForEach-Object { Write-Host ("    {0}" -f $_) }
        exit 1
    }
    $degraded = $preflight | Select-String -Pattern "^\s+-\s" | ForEach-Object { $_.Line.Trim() }
    if ($degraded) {
        Write-Warn "Preflight passed, with degraded capability:"
        $degraded | ForEach-Object { Write-Warn ("  {0}" -f $_) }
    } else {
        Write-OK "Preflight clean - no degraded capability"
    }
}

# ---------------------------------------------------------------------------
# Step 2 - Clear stale processes
# ---------------------------------------------------------------------------
Write-Step "Step 2/8 - Clearing stale processes"

$cf = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if ($cf) {
    $cf | Stop-Process -Force -ErrorAction SilentlyContinue
    cmd /c "taskkill /F /IM cloudflared.exe 2>NUL" | Out-Null
    Start-Sleep -Seconds 1
    Write-OK "Stopped $($cf.Count) stale cloudflared process(es)"
}

$ports = @($ApiPort)
if ($WithStreamlit) { $ports += $StreamlitPort }
foreach ($p in $ports) {
    if (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) {
        Write-Warn ("Port {0} in use - freeing" -f $p)
        [void](Stop-PortOwner -Port $p)
    }
}
Write-OK "ANITA ports clear"

# ---------------------------------------------------------------------------
# Step 3 - Start the tunnel
#
# Before the API, deliberately: TUNNEL_HOST is read once at boot.
# ---------------------------------------------------------------------------
Write-Step "Step 3/8 - Cloudflare tunnel"

$TunnelHost = ""
if ($NamedTunnel) {
    $cfExe = Find-CloudflaredExe
    if (-not $cfExe) { Write-Err "cloudflared not found"; exit 1 }
    $existing = & $cfExe tunnel list 2>$null | Select-String -Pattern $TunnelName
    if (-not $existing) {
        Write-Warn ("Named tunnel '{0}' does not exist - creating" -f $TunnelName)
        & $cfExe tunnel create $TunnelName 2>&1 | Out-Null
    }
    $TunnelLog = Join-Path $LogDir "cloudflared.log"
    Remove-Item $TunnelLog -ErrorAction SilentlyContinue
    # NOT `$args` -- that is PowerShell's automatic unbound-arguments variable,
    # and assigning to it makes the splat behave unpredictably.
    $cfArgs = @{
        FilePath               = "cmd"
        ArgumentList           = "/c", "`"$cfExe`" tunnel run --url http://localhost:$ApiPort $TunnelName 2>&1"
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $TunnelLog
    }
    $CloudflaredProcess = Start-Process @cfArgs
    Write-OK ("Named tunnel '{0}' starting" -f $TunnelName)
    Write-Warn "Named tunnels keep a stable hostname - TUNNEL_HOST should already be set in .env"
} else {
    $cfExe = Find-CloudflaredExe
    if (-not $cfExe) {
        Write-Err "cloudflared not found. Install: winget install --id Cloudflare.cloudflared"
        exit 1
    }
    $TunnelLog = Join-Path $LogDir "cloudflared.log"
    Remove-Item $TunnelLog -ErrorAction SilentlyContinue
    # `2>&1` goes INSIDE the cmd string and stdout is captured by
    # Start-Process, not by a `>` redirect: cmd mangles a quoted redirect
    # target that contains spaces, and the log silently never appears.
    # cloudflared prints its banner to stderr, so the merge is what makes the
    # hostname reach the file at all.
    $cfArgs = @{
        FilePath               = "cmd"
        ArgumentList           = "/c", "`"$cfExe`" tunnel --url http://localhost:$ApiPort --no-autoupdate 2>&1"
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $TunnelLog
    }
    $CloudflaredProcess = Start-Process @cfArgs

    for ($i = 1; $i -le 20; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Path $TunnelLog) {
            $m = Select-String -Path $TunnelLog -Pattern "https://([a-zA-Z0-9\-]+\.trycloudflare\.com)" -ErrorAction SilentlyContinue |
                 Select-Object -First 1
            if ($m) { $TunnelHost = $m.Matches[0].Groups[1].Value; break }
        }
    }
    if (-not $TunnelHost) {
        Write-Err "Could not capture a tunnel hostname. Log: $TunnelLog"
        exit 1
    }
    Write-OK ("Tunnel host: {0}" -f $TunnelHost)
    Write-Warn "Quick tunnels change hostname on every restart - use -NamedTunnel for stability"
}

# ---------------------------------------------------------------------------
# Step 4 - Record the hostname
# ---------------------------------------------------------------------------
Write-Step "Step 4/8 - TUNNEL_HOST"

if ($TunnelHost) {
    $previous = Get-DotEnvValue -Key "TUNNEL_HOST" -Default "(unset)"
    Set-DotEnvValue -Key "TUNNEL_HOST" -Value $TunnelHost
    if ($previous -ne $TunnelHost) {
        Write-OK ("TUNNEL_HOST {0} -> {1}" -f $previous, $TunnelHost)
    } else {
        Write-OK ("TUNNEL_HOST unchanged: {0}" -f $TunnelHost)
    }
} else {
    $TunnelHost = Get-DotEnvValue -Key "TUNNEL_HOST" -Default ""
    if (-not $TunnelHost) {
        Write-Err "No tunnel host configured and no named tunnel - inbound calls cannot work"
        exit 1
    }
    Write-OK ("Using configured TUNNEL_HOST: {0}" -f $TunnelHost)
}

# ---------------------------------------------------------------------------
# Step 5 - Start the API
# ---------------------------------------------------------------------------
Write-Step "Step 5/8 - Platform API (port $ApiPort)"

$ApiLog = Join-Path $LogDir "instance.log"
$ApiArgs = @{
    FilePath               = $PythonExe
    ArgumentList           = "scripts\serve.py"
    # Set explicitly: the platform resolves config/anita.yaml and the corpus
    # relative to the project root, so inheriting the caller's directory works
    # only when invoked from here.
    WorkingDirectory       = $ProjectRoot
    WindowStyle            = "Hidden"
    PassThru               = $true
    RedirectStandardOutput = $ApiLog
    # serve.py sends tracebacks to stderr; discarding it hides the exact failure
    # the health poll is about to time out on.
    RedirectStandardError  = "$ApiLog.err"
}
$ApiProcess = Start-Process @ApiArgs

if (Wait-Http -Url ("http://127.0.0.1:{0}/health" -f $ApiPort) -Attempts 30 -DelaySeconds 2) {
    Write-OK ("API healthy on http://127.0.0.1:{0}" -f $ApiPort)
} else {
    if ($ApiProcess.HasExited) {
        Write-Err ("API exited during startup (code {0}) - port {1} may be held elsewhere" -f $ApiProcess.ExitCode, $ApiPort)
    } else {
        Write-Err "API did not become healthy in 60s"
    }
    Write-Err ("Logs: {0} / {1}" -f $ApiLog, "$ApiLog.err")
    exit 1
}

# ---------------------------------------------------------------------------
# Step 6 - Public reachability and webhook
# ---------------------------------------------------------------------------
Write-Step "Step 6/8 - Public ingress"

if (Wait-Http -Url ("https://{0}/health" -f $TunnelHost) -Attempts 10 -DelaySeconds 3) {
    Write-OK ("Reachable at https://{0}" -f $TunnelHost)
} else {
    Write-Warn ("https://{0}/health did not answer - DNS may still be propagating" -f $TunnelHost)
}

if ($SkipTwilio) {
    Write-Warn "Twilio webhooks NOT updated (-SkipTwilio)"
} else {
    $TwilioScript = Join-Path $ProjectRoot "scripts\update_twilio_webhook.py"
    if (Test-Path $TwilioScript) {
        Push-Location $ProjectRoot
        try {
            # Same `cmd /c` shape as the preflight, for the same reason.
            $result = cmd /c "`"$PythonExe`" `"$TwilioScript`" $TunnelHost 2>&1"
            $twilioExit = $LASTEXITCODE
        } finally {
            Pop-Location
        }
        if ($twilioExit -eq 0) {
            $result | Where-Object { $_ -match "Verified|->|number" } | ForEach-Object { Write-OK $_ }
        } else {
            Write-Warn "Twilio update did not complete:"
            $result | ForEach-Object { Write-Warn ("  {0}" -f $_) }
        }
    } else {
        Write-Warn ("Twilio updater not found: {0}" -f $TwilioScript)
        Write-Warn ("Set manually - Voice URL: https://{0}/twilio/voice (POST)" -f $TunnelHost)
    }
}

# ---------------------------------------------------------------------------
# Step 7 - Optional Streamlit surface
# ---------------------------------------------------------------------------
$StreamlitProcess = $null
Write-Step "Step 7/8 - Streamlit surface"

if (-not $WithStreamlit) {
    Write-Warn "Skipped (-WithStreamlit not given)"
} elseif (-not (Test-Path $StreamlitExe)) {
    Write-Warn ("Streamlit not installed at {0}" -f $StreamlitExe)
} else {
    $StreamlitLog = Join-Path $LogDir "streamlit.log"
    $slArgs = @{
        FilePath               = $StreamlitExe
        ArgumentList           = "run", "streamlit_app.py",
                                 "--server.port", "$StreamlitPort",
                                 "--server.headless", "true",
                                 "--browser.gatherUsageStats", "false"
        WorkingDirectory       = $ProjectRoot
        WindowStyle            = "Hidden"
        PassThru               = $true
        RedirectStandardOutput = $StreamlitLog
        RedirectStandardError  = "$StreamlitLog.err"
    }
    $StreamlitProcess = Start-Process @slArgs
    if (Wait-Http -Url ("http://127.0.0.1:{0}/_stcore/health" -f $StreamlitPort) -Attempts 30 -DelaySeconds 2) {
        Write-OK ("Streamlit on http://127.0.0.1:{0}" -f $StreamlitPort)
    } else {
        Write-Warn ("Streamlit did not become healthy - log: {0}" -f $StreamlitLog)
    }
}

# ---------------------------------------------------------------------------
# Step 8 - Summary
# ---------------------------------------------------------------------------
Write-Step "Step 8/8 - Summary"

$number = Get-DotEnvValue -Key "TWILIO_PHONE_NUMBER" -Default "(unset)"

Write-Host ""
Write-Host ("{0}  Public{1}" -f $BOLD, $RESET)
Write-Host ("    https://{0}" -f $TunnelHost)
if (-not $NamedTunnel) {
    Write-Host ("    {0}ephemeral - this URL changes when the tunnel restarts{1}" -f $YELLOW, $RESET)
}
Write-Host ""
Write-Host ("{0}  Telephony{1}" -f $BOLD, $RESET)
Write-Host ("    dial           : {0}" -f $number)
Write-Host ("    voice webhook  : https://{0}/twilio/voice  (POST)" -f $TunnelHost)
Write-Host ("    status callback: https://{0}/twilio/status (POST)" -f $TunnelHost)
Write-Host ("    media stream   : wss://{0}/twilio/media-stream" -f $TunnelHost)
Write-Host ""
Write-Host ("{0}  Local{1}" -f $BOLD, $RESET)
Write-Host ("    api    : http://127.0.0.1:{0}" -f $ApiPort)
Write-Host ("    console: http://127.0.0.1:{0}/console" -f $ApiPort)
if ($WithStreamlit) { Write-Host ("    chat   : http://127.0.0.1:{0}" -f $StreamlitPort) }
Write-Host ""
Write-Host ("{0}  Logs{1}" -f $BOLD, $RESET)
Write-Host ("    api       : {0}" -f $ApiLog)
Write-Host ("    tunnel    : {0}" -f $TunnelLog)
if ($WithStreamlit) { Write-Host ("    streamlit : {0}" -f $StreamlitLog) }
Write-Host ""

# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
$running = @($ApiProcess) + @($CloudflaredProcess) + @($StreamlitProcess) |
           Where-Object { $_ }
$dead = @($running | Where-Object { $_.HasExited })
if ($dead.Count -gt 0) {
    foreach ($d in $dead) { Write-Err ("{0} (PID {1}) has already exited" -f $d.ProcessName, $d.Id) }
    exit 1
}

Write-OK ("Stack is up. {0} service(s) running." -f $running.Count)
Write-Host ("{0}  Re-run this script to restart; it clears stale port holders first.{1}" -f $DIM, $RESET)
exit 0
