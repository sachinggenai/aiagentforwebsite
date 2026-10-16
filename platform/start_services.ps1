<#
.SYNOPSIS
    Start the ANITA MEDICAL SYSTEMS platform: preflight, tunnel, API, Twilio.

.DESCRIPTION
    Brings up one instance in the order the platform actually requires:

        preflight -> kill stale -> tunnel -> write TUNNEL_HOST -> API -> Twilio
                 -> Streamlit

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

    **The Streamlit surface comes up last, and gets its own public hostname.**
    A Cloudflare quick tunnel serves exactly one origin, so the API's port and
    the chat's port cannot share a hostname — publishing the chat means a
    second cloudflared process and a second `<random>.trycloudflare.com` URL.
    The chat's hostname is printed and written to `logs\urls.txt` but
    deliberately NOT written to `.env`: nothing in the platform reads it, and a
    key set but never read is an INERT key, which the boot audit refuses.

    Streamlit has **no authentication**. Publishing it means anyone holding
    that URL can use the assistant and consume the GPU, so the surface is bound
    to 127.0.0.1 and reached only through the tunnel.

.PARAMETER Local
    Run everything locally and open no tunnel: no cloudflared, no TUNNEL_HOST
    rewrite, no Twilio re-pointing. This is the mode for "start the chatbot and
    try it" -- the chat and console answer on 127.0.0.1, and the box is not
    exposed to the internet.

    TUNNEL_HOST in .env is left exactly as it is, so a later full start (or a
    call already routed to the previous tunnel) is not silently broken by a
    local run.

.PARAMETER NoBrowser
    Do not open the chat page in the default browser. Only meaningful with
    -Local, which opens it by default.

.PARAMETER SkipTwilio
    Do not re-point the Twilio number's webhooks at the new tunnel hostname.
    Use when you are not placing calls.

.PARAMETER NamedTunnel
    Use a stable named tunnel instead of a quick one, so the hostname survives
    a restart and Twilio does not need re-pointing. Requires `cloudflared
    tunnel login` to have been run once.

.PARAMETER TunnelName
    Name of the named tunnel. Default "anita-tunnel".

.PARAMETER NoStreamlit
    Do not start the Streamlit surface. It starts by default — it is the
    surface a validation run actually uses, and a stack that comes up without
    the thing being validated is the failure this default removes. Served on
    ANITA_STREAMLIT_PORT; in tunnel mode it is also published on its own
    Cloudflare hostname.

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

.EXAMPLE
    .\start_services.ps1 -Local
    Local only: chat and console on 127.0.0.1, no tunnel, no public exposure.

.EXAMPLE
    .\start_services.ps1 -NoStreamlit
    Full start without the Streamlit surface — API, tunnel and Twilio only.
#>

param(
    [switch]$SkipTwilio = $false,
    [switch]$NamedTunnel = $false,
    [string]$TunnelName = "anita-tunnel",
    [switch]$NoStreamlit = $false,
    [switch]$SkipPreflight = $false,
    [switch]$Local = $false,
    [switch]$NoBrowser = $false
)

# Local mode never routes calls, so it can never want the Twilio re-point.
if ($Local) { $SkipTwilio = $true }

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
# Capture the whole run to a file.
#
# A launcher prints its summary once and exits. If the window scrolls, the
# session closes, or the operator is running it by right-click -> "Run with
# PowerShell" (which shuts the window the instant the script ends), every URL it
# printed is gone -- and the tunnel hostname cannot be recovered from anywhere
# else. The transcript is the answer to "the information did not display": it
# is on disk whether or not anyone was watching the console.
# ---------------------------------------------------------------------------
$TranscriptFile = Join-Path $LogDir "start_services.log"
try {
    Start-Transcript -Path $TranscriptFile -Force | Out-Null
} catch {
    # Already transcribing (nested run) or no host support -- never fatal.
    Write-Warn "Could not start a transcript; console output only"
}

function Exit-Script {
    # Every exit path goes through here, so the transcript is always closed.
    # A half-written transcript is worse than none: it looks complete.
    param([int]$Code = 0)
    try { Stop-Transcript | Out-Null } catch { }
    exit $Code
}

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
if (-not $NoStreamlit) { Write-Host ("  chat : {0}" -f $StreamlitPort) }
if ($Local) {
    Write-Host ("  mode : {0}LOCAL - no tunnel, no public exposure{1}" -f $YELLOW, $RESET)
} else {
    Write-Host ("  mode : tunnel + Twilio" -f $RESET)
}

if (-not (Test-Path $PythonExe)) {
    Write-Err ("Python not found at {0}" -f $PythonExe)
    Write-Err "Create the venv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    Exit-Script 1
}

# ---------------------------------------------------------------------------
# Step 1 - Preflight
#
# ANITA's signature gate. It refuses a boot that would share a database,
# collection or port with the sibling product, and it fails before any socket
# opens. Starting services around it would produce a stack that silently serves
# another product's data, which is the one failure worth refusing outright.
# ---------------------------------------------------------------------------
Write-Step "Step 1/9 - Preflight (config, isolation, corpus)"

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
        Exit-Script 1
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
Write-Step "Step 2/9 - Clearing stale processes"

if ($Local) {
    # Local mode starts no tunnel, so it has no business killing one.
    # `taskkill /IM cloudflared.exe` is machine-wide, and this box shares with
    # the sibling product -- whose own tunnels would die with it. A local test
    # run must not be able to break the other product.
    Write-OK "cloudflared left alone (-Local starts no tunnel)"
} else {
    # Scope the kill to tunnels pointed at THIS product's ports -- the API's and
    # the chat's. A stale ANITA tunnel from a previous run always names one of
    # them in its `--url`, so this still clears everything this script could
    # have started -- without taking down a tunnel that belongs to something
    # else on the machine.
    #
    # Both spellings are matched because the two tunnels do not use the same
    # one: the API tunnel passes `http://localhost:$ApiPort` (unchanged, and its
    # exact form is load-bearing for Twilio), the chat tunnel passes the
    # explicit `127.0.0.1` it is bound to. `\b` keeps 8100 from matching inside
    # a longer run of digits.
    $cfPattern = "(localhost|127\.0\.0\.1):($ApiPort|$StreamlitPort)\b"
    $cf = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match $cfPattern })
    if ($cf.Count -gt 0) {
        foreach ($proc in $cf) {
            cmd /c "taskkill /F /T /PID $($proc.ProcessId) 2>NUL" | Out-Null
        }
        Start-Sleep -Seconds 1
        Write-OK ("Stopped {0} stale cloudflared process(es) for port {1}" -f $cf.Count, $ApiPort)
    } else {
        $other = @(Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue)
        if ($other.Count -gt 0) {
            Write-Warn ("{0} cloudflared process(es) belong to another port - left running" -f $other.Count)
        }
    }
}

$ports = @($ApiPort)
if (-not $NoStreamlit) { $ports += $StreamlitPort }
foreach ($p in $ports) {
    if (Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue) {
        Write-Warn ("Port {0} in use - freeing" -f $p)
        [void](Stop-PortOwner -Port $p)
    }
}
Write-OK "ANITA ports clear"

# ---------------------------------------------------------------------------
# Step 3 - Platform health
#
# Two things here decide whether voice works, and neither shows up as a failed
# start:
#
#   * free VRAM against the sibling floor. `anita/capacity.py` samples the
#     device and walks a ladder down (voice_shed -> stt_off -> ...) when the
#     card is full. The sibling product's Ollama models live on this same card,
#     so the ladder sitting at `voice_shed` is an ordinary Tuesday here, not a
#     fault -- and a stack that starts cleanly while every call is refused as
#     busy is the confusing case this step exists to make legible.
#   * the models the config names actually being installed. A missing model
#     fails at the first turn, not at boot.
#
# Deliberately NO model pre-warm, unlike the sibling product's launcher. ANITA
# warms what it needs during assembly (`llm.backend.warm()`, and the TTS warmup
# in `voice.py`), so the first turn is already warm. Pulling a 9 GB generation
# model into VRAM from here would be worse than redundant: it is exactly what
# drops free memory under the sibling floor and sheds voice.
# ---------------------------------------------------------------------------
Write-Step "Step 3/9 - Platform health (GPU, Ollama, models)"

$freeMiB = $null
if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
    $gpuLine = (cmd /c "nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader 2>&1" |
                Select-Object -First 1)
    if ($gpuLine -and ($gpuLine -notmatch "not found|error|failed")) {
        Write-OK ("GPU: {0}" -f $gpuLine.Trim())
        if ($gpuLine -match ",\s*(\d+)\s*MiB\s*$") { $freeMiB = [int]$Matches[1] }
    } else {
        Write-Warn "nvidia-smi is present but the query failed - the GPU may be unavailable"
    }
} else {
    Write-Warn "nvidia-smi not found - no CUDA device; STT and TTS fall back to CPU (~0.6x real time)"
    Write-Warn "  the effective call ceiling becomes CPU_CONCURRENT_CALLS, not the configured value"
}

# The floor is configuration, so read the file the capacity policy reads rather
# than restating the number here and letting the two drift apart.
$FloorMiB = 6000
$ConfigFile = Join-Path $ProjectRoot "config\anita.yaml"
if (Test-Path $ConfigFile) {
    $floorMatch = Select-String -Path $ConfigFile -Pattern "^\s*sibling_floor_mib:\s*(\d+)" -ErrorAction SilentlyContinue |
                  Select-Object -First 1
    if ($floorMatch) { $FloorMiB = [int]$floorMatch.Matches[0].Groups[1].Value }
}
if ($null -ne $freeMiB) {
    if ($freeMiB -lt $FloorMiB) {
        Write-Warn ("free VRAM {0} MiB is BELOW the sibling floor of {1} MiB" -f $freeMiB, $FloorMiB)
        Write-Warn "  the capacity ladder will shed voice - calls are refused as busy while text keeps working"
        Write-Warn "  that is the ladder doing its job, not a fault; free the card (ollama stop) for voice"
    } else {
        Write-OK ("free VRAM {0} MiB is above the sibling floor of {1} MiB - voice stays available" -f $freeMiB, $FloorMiB)
    }
}

$OllamaUrl = Get-DotEnvValue -Key "OLLAMA_URL" -Default "http://127.0.0.1:11434"
$tagsJson = [string](curl.exe -s --max-time 5 "$OllamaUrl/api/tags" 2>$null)
if ($tagsJson -match '"models"') {
    Write-OK ("Ollama reachable at {0}" -f $OllamaUrl)
    foreach ($key in @("OLLAMA_MODEL", "EMBED_MODEL")) {
        $want = Get-DotEnvValue -Key $key -Default ""
        if (-not $want) { continue }
        if ($tagsJson -match [regex]::Escape($want.Split(":")[0])) {
            Write-OK ("  {0} = {1} (installed)" -f $key, $want)
        } else {
            Write-Warn ("  {0} = {1} is NOT installed - pull it before the first turn" -f $key, $want)
        }
    }
} else {
    Write-Warn ("Ollama not reachable at {0}" -f $OllamaUrl)
    Write-Warn "  prose answers degrade to the deterministic path; commercial answers never needed it"
}

# ---------------------------------------------------------------------------
# Step 4 - Start the tunnel
#
# Before the API, deliberately: TUNNEL_HOST is read once at boot.
# ---------------------------------------------------------------------------
Write-Step "Step 4/9 - Cloudflare tunnel"

$TunnelHost = ""
# Declared here, not inside the branch below: -Local skips the tunnel entirely,
# and the liveness check at the end and the summary both reference these. An
# undefined name would not raise (no Set-StrictMode here), but a stale value
# carried in from a previous section would.
$TunnelLog = ""
$CloudflaredProcess = $null
$StreamlitProcess = $null
$StreamlitLog = ""
# The chat's own tunnel. Separate process, separate log, separate hostname --
# one quick tunnel serves one origin, so these cannot be shared with the API's.
$StreamlitTunnelHost = ""
$StreamlitTunnelLog = ""
$StreamlitTunnelProcess = $null
if ($Local) {
    Write-Warn "Skipped (-Local) - the instance is reachable on 127.0.0.1 only"
} elseif ($NamedTunnel) {
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
        Exit-Script 1
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
        Exit-Script 1
    }
    Write-OK ("Tunnel host: {0}" -f $TunnelHost)
    Write-Warn "Quick tunnels change hostname on every restart - use -NamedTunnel for stability"
}

# ---------------------------------------------------------------------------
# Step 5 - Record the hostname
# ---------------------------------------------------------------------------
Write-Step "Step 5/9 - TUNNEL_HOST"

if ($Local) {
    # Read-only in local mode. Writing here would clobber the hostname of a
    # tunnel that is still up and still routed to by Twilio, so a five-minute
    # local test would silently break inbound calls.
    $TunnelHost = Get-DotEnvValue -Key "TUNNEL_HOST" -Default ""
    if ($TunnelHost) {
        Write-Warn ("Unchanged (left as configured): {0}" -f $TunnelHost)
    } else {
        Write-Warn "No TUNNEL_HOST configured - correct for local-only use"
    }
} elseif ($TunnelHost) {
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
        Exit-Script 1
    }
    Write-OK ("Using configured TUNNEL_HOST: {0}" -f $TunnelHost)
}

# ---------------------------------------------------------------------------
# Step 6 - Start the API
# ---------------------------------------------------------------------------
Write-Step "Step 6/9 - Platform API (port $ApiPort)"

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
    Exit-Script 1
}

# ---------------------------------------------------------------------------
# Step 7 - Public reachability and webhook
# ---------------------------------------------------------------------------
Write-Step "Step 7/9 - Public ingress"

if ($Local) {
    Write-Warn "Skipped (-Local) - nothing is published and no webhook is re-pointed"
} else {

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
        # Print the updater's WHOLE report. The earlier filter
        # ("Verified|->|number") dropped every line that did not match -- which
        # included the WhatsApp webhook URL and any read-back mismatch, i.e. the
        # two things an operator reads this step for.
        if ($twilioExit -eq 0) {
            $result | ForEach-Object { Write-OK $_ }
        } else {
            Write-Warn "Twilio update did not complete:"
            $result | ForEach-Object { Write-Warn ("  {0}" -f $_) }
            Write-Warn ("Set it by hand - Voice URL: https://{0}/twilio/voice (POST)" -f $TunnelHost)
        }
    } else {
        Write-Warn ("Twilio updater not found: {0}" -f $TwilioScript)
        Write-Warn ("Set manually - Voice URL: https://{0}/twilio/voice (POST)" -f $TunnelHost)
    }
}

}   # end of the not-$Local public-ingress branch

# ---------------------------------------------------------------------------
# Step 8 - Streamlit surface, and its own public hostname
# ---------------------------------------------------------------------------
$StreamlitProcess = $null
Write-Step "Step 8/9 - Streamlit surface"

if ($NoStreamlit) {
    Write-Warn "Skipped (-NoStreamlit)"
} elseif (-not (Test-Path $StreamlitExe)) {
    Write-Warn ("Streamlit not installed at {0}" -f $StreamlitExe)
    Write-Warn "  install it: .venv\Scripts\pip install streamlit"
} else {
    $StreamlitLog = Join-Path $LogDir "streamlit.log"
    # `--server.address 127.0.0.1` is deliberate, and pairs with the tunnel URL
    # below. Streamlit binds every interface by default -- its banner advertises
    # a 192.168.x.x "Network URL" -- which would put an unauthenticated chat
    # surface on the office LAN as well as on the internet. Loopback-only leaves
    # exactly one way in, and naming the same literal address on both sides
    # avoids the Windows `localhost` -> ::1 mismatch that would otherwise have
    # cloudflared dial an address nothing is listening on.
    $slArgs = @{
        FilePath               = $StreamlitExe
        ArgumentList           = "run", "streamlit_app.py",
                                 "--server.port", "$StreamlitPort",
                                 "--server.address", "127.0.0.1",
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

    # --- publish the surface -----------------------------------------------
    # Same shape as the API tunnel in Step 4, for the same reason: `2>&1` goes
    # inside the cmd string and stdout is captured by Start-Process, because
    # cloudflared prints its banner to stderr.
    #
    # A *quick* tunnel even under -NamedTunnel. A named tunnel serves one origin
    # unless it is handed an ingress config and a second DNS route, and making
    # that a prerequisite would mean the chat silently vanished in named mode.
    # Nothing reads this hostname -- it is not in `.env` -- so ephemeral costs
    # nothing here the way it would for TUNNEL_HOST, which Twilio depends on.
    if ($Local) {
        Write-Warn "No tunnel (-Local) - the Streamlit surface is local only"
    } else {
        $cfExe = Find-CloudflaredExe
        if (-not $cfExe) {
            Write-Warn "cloudflared not found - the Streamlit surface stays local"
        } else {
            $StreamlitTunnelLog = Join-Path $LogDir "cloudflared-streamlit.log"
            Remove-Item $StreamlitTunnelLog -ErrorAction SilentlyContinue
            $slTunnelArgs = @{
                FilePath               = "cmd"
                ArgumentList           = "/c", "`"$cfExe`" tunnel --url http://127.0.0.1:$StreamlitPort --no-autoupdate 2>&1"
                WindowStyle            = "Hidden"
                PassThru               = $true
                RedirectStandardOutput = $StreamlitTunnelLog
            }
            $StreamlitTunnelProcess = Start-Process @slTunnelArgs

            for ($i = 1; $i -le 20; $i++) {
                Start-Sleep -Seconds 3
                if (Test-Path $StreamlitTunnelLog) {
                    $m = Select-String -Path $StreamlitTunnelLog -Pattern "https://([a-zA-Z0-9\-]+\.trycloudflare\.com)" -ErrorAction SilentlyContinue |
                         Select-Object -First 1
                    if ($m) { $StreamlitTunnelHost = $m.Matches[0].Groups[1].Value; break }
                }
            }
            if ($StreamlitTunnelHost) {
                Write-OK ("Streamlit public host: {0}" -f $StreamlitTunnelHost)
            } else {
                # Warn, never abort. A chat URL that did not mint must not take
                # down the API and the Twilio routing that already came up.
                Write-Warn "Could not capture a Streamlit tunnel hostname"
                Write-Warn ("  log: {0}" -f $StreamlitTunnelLog)
                Write-Warn ("  the surface still works locally on 127.0.0.1:{0}" -f $StreamlitPort)
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Step 9 - Summary
# ---------------------------------------------------------------------------
Write-Step "Step 9/9 - Summary"

$number = Get-DotEnvValue -Key "TWILIO_PHONE_NUMBER" -Default "(unset)"

# ---------------------------------------------------------------------------
# Both URLs, on every run -- and whether the public one actually answers.
#
# Whichever mode ran, an operator wants the same two things: where this instance
# is on this machine, and where (if anywhere) it is on the internet. In local
# mode the public host is whatever .env already holds -- possibly a tunnel from
# an earlier run -- so it is PROBED and labelled, never assumed. Printing it
# only in tunnel mode is what made "the Cloudflare URL is missing" happen: a
# local run rewrote the file and took the public link down with it.
# ---------------------------------------------------------------------------
$PublicHost = $TunnelHost
$PublicLive = $false
if ($PublicHost) {
    $probe = curl.exe -s -o NUL -w "%{http_code}" --max-time 8 "https://$PublicHost/health" 2>$null
    $PublicLive = ($probe -eq "200")
}

Write-Host ""
if ($PublicHost) {
    Write-Host ("{0}  Public (Cloudflare){1}" -f $BOLD, $RESET)
    Write-Host ("    chat (API)       : https://{0}/" -f $PublicHost)
    if ($PublicLive) {
        Write-Host ("    {0}reachable now{1}" -f $GREEN, $RESET)
    } else {
        Write-Host ("    {0}NOT reachable - stale host, or its tunnel is not running{1}" -f $RED, $RESET)
    }
    if ($StreamlitTunnelHost) {
        Write-Host ("    chat (Streamlit) : https://{0}" -f $StreamlitTunnelHost)
        Write-Host ("    {0}no login - anyone with this link can use the assistant{1}" -f $YELLOW, $RESET)
    }
    if (-not $Local -and -not $NamedTunnel) {
        Write-Host ("    {0}ephemeral - these URLs change when the tunnel restarts{1}" -f $YELLOW, $RESET)
    }
    Write-Host ""
    Write-Host ("{0}  Telephony (Twilio -> this instance){1}" -f $BOLD, $RESET)
    Write-Host ("    dial            : {0}" -f $number)
    Write-Host ("    voice webhook   : https://{0}/twilio/voice         (POST)" -f $PublicHost)
    Write-Host ("    status callback : https://{0}/twilio/status        (POST)" -f $PublicHost)
    Write-Host ("    media stream    : wss://{0}/twilio/media-stream" -f $PublicHost)
    Write-Host ("    whatsapp        : https://{0}/twilio/whatsapp      (POST)" -f $PublicHost)
    Write-Host ""
} else {
    Write-Host ("{0}  Public (Cloudflare){1}" -f $BOLD, $RESET)
    Write-Host ("    {0}none - no tunnel minted this run and none configured in .env{1}" -f $DIM, $RESET)
    Write-Host ""
}

# Always printed, in both modes. In tunnel mode these are the operator's local
# view of a publicly-reachable instance -- the console in particular is where an
# escalation or a failed send actually gets looked at.
Write-Host ("{0}  Local (this machine){1}" -f $BOLD, $RESET)
Write-Host ("    chat            : http://127.0.0.1:{0}/" -f $ApiPort)
Write-Host ("    console         : http://127.0.0.1:{0}/console            (bearer token)" -f $ApiPort)
Write-Host ("    api contract    : http://127.0.0.1:{0}/contract" -f $ApiPort)
if (-not $NoStreamlit) { Write-Host ("    streamlit       : http://127.0.0.1:{0}" -f $StreamlitPort) }
Write-Host ""
Write-Host ("{0}  Endpoints{1}" -f $BOLD, $RESET)
Write-Host ("    buyer   : GET  /                      POST /api/chat/message" -f $RESET)
Write-Host ("              GET  /api/chat/history      POST /api/voice/ticket")
Write-Host ("              WS   /voice/browser           (mic, token in first frame)")
Write-Host ("    admin   : GET  /api/admin/{0}" -f "channels|conversations|metrics|handoffs|sends")
Write-Host ("              GET  /api/admin/conversation  POST /api/admin/gate")
Write-Host ("    ops     : GET  /health                GET  /contract")
Write-Host ""
Write-Host ("{0}  Logs{1}" -f $BOLD, $RESET)
Write-Host ("    api       : {0}" -f $ApiLog)
Write-Host ("    api err   : {0}.err" -f $ApiLog)
if (-not $Local) { Write-Host ("    tunnel    : {0}" -f $TunnelLog) }
if (-not $NoStreamlit) { Write-Host ("    streamlit : {0}" -f $StreamlitLog) }
if ($StreamlitTunnelLog) { Write-Host ("    chat tun. : {0}" -f $StreamlitTunnelLog) }
Write-Host ""

# ---------------------------------------------------------------------------
# The same summary, written to a file.
#
# A launcher prints its summary once. The window scrolls, the session closes,
# and the one value that cannot be recovered from anywhere else -- an ephemeral
# tunnel hostname -- is gone with it. Writing it down costs nothing and makes
# "what was the URL?" answerable an hour later.
# ---------------------------------------------------------------------------
$SummaryFile = Join-Path $LogDir "urls.txt"
$OllamaUrl = if ($OllamaUrl) { $OllamaUrl } else { "http://127.0.0.1:11434" }
$lines = @()
$lines += "ANITA MEDICAL SYSTEMS - platform"
$lines += "started  : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$lines += "mode     : $(if ($Local) { 'local (no tunnel)' } else { 'tunnel + Twilio' })"
$lines += ""
$lines += "PUBLIC  (Cloudflare - open this from anywhere)"
if ($PublicHost) {
    $lines += "  chat (API)       https://$PublicHost/"
    if ($PublicLive) {
        $lines += "  status: reachable now (/health answered 200)"
    } else {
        $lines += "  status: NOT REACHABLE - stale host, or its tunnel is not running"
    }
    if ($StreamlitTunnelHost) {
        $lines += "  chat (Streamlit) https://$StreamlitTunnelHost"
        $lines += "  note  : Streamlit has no login - anyone with this link can use it"
    }
    if (-not $Local -and -not $NamedTunnel) {
        $lines += "  note  : ephemeral - these hostnames change when the tunnel restarts"
    }
} else {
    $lines += "  none - no tunnel minted this run and none configured in .env"
}
$lines += ""
$lines += "LOCAL  (this machine - open this on the box itself)"
$lines += "  chat            http://127.0.0.1:$ApiPort/"
$lines += "  console         http://127.0.0.1:$ApiPort/console         (bearer token)"
$lines += "  api contract    http://127.0.0.1:$ApiPort/contract"
$lines += "  health          http://127.0.0.1:$ApiPort/health"
if (-not $NoStreamlit) { $lines += "  streamlit       http://127.0.0.1:$StreamlitPort" }
$lines += ""
if ($PublicHost) {
    $lines += "TELEPHONY  (Twilio -> this instance)"
    $lines += "  dial            $number"
    $lines += "  voice webhook   https://$PublicHost/twilio/voice        (POST)"
    $lines += "  status callback https://$PublicHost/twilio/status       (POST)"
    $lines += "  media stream    wss://$PublicHost/twilio/media-stream"
    $lines += "  whatsapp        https://$PublicHost/twilio/whatsapp     (POST)"
    $lines += "                  (sender setting - Messaging -> Senders in the Twilio Console)"
    $lines += ""
}
$lines += "ENDPOINTS"
$lines += "  buyer  GET  /                    POST /api/chat/message"
$lines += "         GET  /api/chat/history    POST /api/voice/ticket"
$lines += "         WS   /voice/browser       (mic, token in first frame)"
$lines += "  admin  GET  /api/admin/channels|conversations|conversation|metrics|handoffs|sends"
$lines += "         POST /api/admin/gate"
$lines += "  ops    GET  /health              GET  /contract"
$lines += ""
$lines += "LOGS"
$lines += "  api       $ApiLog"
$lines += "  api err   $ApiLog.err"
if (-not $Local) { $lines += "  tunnel    $TunnelLog" }
if (-not $NoStreamlit) { $lines += "  streamlit $StreamlitLog" }
if ($StreamlitTunnelLog) { $lines += "  chat tunnel $StreamlitTunnelLog" }
if (-not $Local) {
    $lines += ""
    $lines += "NOTE  a quick tunnel's hostname changes on every restart. Re-run this"
    $lines += "      script to mint new ones and re-point Twilio, or use -NamedTunnel"
    $lines += "      (which stabilises the API's hostname only -- the chat's is always"
    $lines += "      a quick tunnel, because a named one serves a single origin)."
}
Set-Content -Path $SummaryFile -Value $lines -Encoding UTF8
Write-Host ("    urls      : {0}" -f $SummaryFile)
Write-Host ""

# The universityDemo launcher ends with a copy-pasteable probe; it is the
# fastest way to answer "is the tunnel actually serving, or just registered?"
if ($PublicHost) {
    Write-Host ("{0}  Test it{1}" -f $BOLD, $RESET)
    Write-Host ("    curl https://{0}/health" -f $PublicHost)
    Write-Host ""
}
if ($StreamlitTunnelHost) {
    Write-Host ("{0}  Open the Streamlit chat{1}" -f $BOLD, $RESET)
    Write-Host ("    https://{0}" -f $StreamlitTunnelHost)
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
$running = @($ApiProcess) + @($CloudflaredProcess) + @($StreamlitProcess) + @($StreamlitTunnelProcess) |
           Where-Object { $_ }
$dead = @($running | Where-Object { $_.HasExited })
if ($dead.Count -gt 0) {
    foreach ($d in $dead) { Write-Err ("{0} (PID {1}) has already exited" -f $d.ProcessName, $d.Id) }
    Exit-Script 1
}

Write-OK ("Stack is up. {0} service(s) running." -f $running.Count)
Write-Host ("{0}  Re-run this script to restart; it clears stale port holders first.{1}" -f $DIM, $RESET)

# Local mode exists to answer "does the chatbot work?" -- so put the chatbot in
# front of the operator rather than making them copy a URL out of the summary.
if ($Local -and -not $NoBrowser) {
    $chatUrl = "http://127.0.0.1:{0}/" -f $ApiPort
    try {
        Start-Process $chatUrl | Out-Null
        Write-OK ("Opened {0} in the default browser" -f $chatUrl)
    } catch {
        Write-Warn ("Could not open a browser - go to {0}" -f $chatUrl)
    }
}
Exit-Script 0