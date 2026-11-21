# Duka-Yangu HTTP smoke test: one command against the real running server.
#
# Run from anywhere (paths resolve from this script's location):
#   powershell -ExecutionPolicy Bypass -File backend/scripts/smoke_http.ps1
#
# What it does: picks a throwaway database file in the system temp directory
# (never backend/duka.db) and a free localhost port, starts `uvicorn app:app`
# itself as a child process with DUKA_ENV=development, a random
# DUKA_SESSION_SECRET and DUKA_ALLOWED_ORIGINS set to the origin below, waits
# for /api/health, then exercises auth, isolation, passports, CSRF and
# throttling over real HTTP. The server is ALWAYS stopped and the temp files
# deleted at the end, even on failure (see the finally block).
#
# No Meta API key is needed and none is ever called: the only endpoints that
# reach the AI parser or transcriber are /api/entry and /api/transcribe, and
# this script calls them solely WITHOUT a session (expecting 401, which is
# raised before any AI code runs) or with a bad Origin (expecting 403, also
# raised first). /api/credit/explain is never called because even anonymous
# reads can consume AI budget. Test rows are written with plain SQL by
# backend/scripts/smoke_seed.py.
#
# Staying under the app's own rate limits (so the script never fails itself):
# - Register (10/hour per IP): exactly 5 counted register hits from 127.0.0.1
#   (Shop A, Shop B, reused-code, wrong-code, Shop C). The weak-PIN attempt
#   returns 422 before the register limiter runs, so it is not counted.
# - Invite guesses (10/hour per IP): exactly 2 failed guesses (reused code,
#   wrong code). The weak PIN never reaches the invite check.
# - Login (20/minute per IP): 1 explicit login (Shop A, cookie-flag check)
#   plus 4 lockout-probe logins (Shop C) = 5 total.
# - Per-phone 24h ceiling (30 failures): the lockout probe burns only 4.
# - Per-endpoint guards: entry 0 admitted calls (limit 20/min), transcribe 0
#   (limit 10/min), explain 0 (limit 10/min), passport creates 2
#   (limit 10/min), public lender views 3 (limit 30/min). The missing-Origin
#   probes (Invoke-WebRequest and HttpClient alike) are rejected at the
#   Origin check before any per-endpoint guard, AI charge or write, so they
#   consume no quota either (proved by the AI counter check in section 9).
# - Status codes work on both Windows PowerShell 5.1 and PowerShell 7+:
#   PS7 uses -SkipHttpErrorCheck so statuses arrive on the response object;
#   5.1 reads them from the exception, with a message-text fallback. A -1
#   is never silent: the exception message and response body are printed.
# - One check per behavior: no loops, no retries that could amplify traffic.
#
# Secrets hygiene: invite codes and the lender token are printed only masked
# (first 4 chars plus length); PINs and hashes are never printed at all.

$ErrorActionPreference = "Stop"

# This session can resolve paths with the \\?\ long-path prefix, which the
# Windows PowerShell 5.1 path cmdlets (Join-Path, Test-Path) do not accept.
# Strip it once, up front.
function Strip-LongPathPrefix($p) {
    if ([string]::IsNullOrEmpty($p)) { return $p }
    if ($p.StartsWith("\\?\UNC\")) { return "\\" + $p.Substring(8) }
    if ($p.StartsWith("\\?\")) { return $p.Substring(4) }
    return $p
}
$ScriptDir = Strip-LongPathPrefix (Split-Path -Parent $PSCommandPath)
if ([string]::IsNullOrEmpty($ScriptDir)) { $ScriptDir = Strip-LongPathPrefix (Get-Location).Path }
# Walk up to the repo root (the folder holding backend/app.py), starting from
# this script's folder and falling back to the current directory.
$RepoRoot = $null
$node = $ScriptDir
while ($node) {
    if (Test-Path (Join-Path $node "backend\app.py")) { $RepoRoot = $node; break }
    if ((Split-Path -Leaf $node) -eq "backend") {
        $parent = Split-Path -Parent $node
        if (Test-Path (Join-Path $parent "backend\app.py")) { $RepoRoot = $parent; break }
    }
    $parent = Split-Path -Parent $node
    if ([string]::IsNullOrEmpty($parent) -or $parent -eq $node) { break }
    $node = $parent
}
if ([string]::IsNullOrEmpty($RepoRoot)) {
    throw "Cannot locate the repo root (no backend/app.py above $ScriptDir)."
}
$BackendDir = Join-Path $RepoRoot "backend"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) { $Python = $VenvPython } else { $Python = "python" }

$TempDir = [System.IO.Path]::GetTempPath()
$TempDb = Join-Path $TempDir ("duka-smoke-{0}.db" -f [Guid]::NewGuid().ToString("N"))
$ServerLog = Join-Path $TempDir ("duka-smoke-{0}.out.log" -f [Guid]::NewGuid().ToString("N"))
$ServerErr = Join-Path $TempDir ("duka-smoke-{0}.err.log" -f [Guid]::NewGuid().ToString("N"))

$script:pass = 0
$script:fail = 0

function Report($name, $expected, $actual) {
    if ("$expected" -eq "$actual") {
        $script:pass++
        Write-Output "PASS  $name -> $actual (expected $expected)"
    } else {
        $script:fail++
        Write-Output "FAIL  $name -> $actual (expected $expected)"
    }
}

function Mask($s) {
    if ([string]::IsNullOrEmpty($s)) { return "<empty>" }
    $n = [Math]::Min(4, $s.Length)
    return $s.Substring(0, $n) + "...<$($s.Length) chars>"
}

# Invoke-WebRequest gained -SkipHttpErrorCheck in PowerShell 7: with it,
# HTTP error statuses come back on the response object instead of as
# exceptions, so no status can be lost in exception plumbing. 5.1 has no
# such switch and always throws on 4xx/5xx.
$script:SkipHttpErrors = (Get-Command Invoke-WebRequest).Parameters.ContainsKey("SkipHttpErrorCheck")

function Invoke-SmokeRequest($params) {
    if ($script:SkipHttpErrors) { $params["SkipHttpErrorCheck"] = $true }
    return Invoke-WebRequest @params
}

function Get-Status($err) {
    # Real HTTP status from the exception, on both Windows PowerShell 5.1
    # (WebException + HttpWebResponse) and PowerShell 7+
    # (HttpResponseException + HttpResponseMessage).
    try {
        $resp = $err.Exception.Response
        if ($null -ne $resp -and $null -ne $resp.StatusCode) {
            return [int]$resp.StatusCode
        }
    } catch {}
    # Fallback: both engines carry the code in the message, e.g.
    # "(403) Forbidden" (5.1) or "...success: 403 (Forbidden)" (7+).
    try {
        $msg = $err.Exception.Message
        if ($msg -match '\((\d{3})\)') { return [int]$Matches[1] }
    } catch {}
    return -1
}

function New-TransportResult($err, $uri) {
    # The catch path for requests that threw. A recovered status (direct
    # read or message fallback) returns quietly; a true -1 is NEVER
    # silent: the exception message and whatever body arrived are printed,
    # and the caller reports -1 so the check FAILs with a clear label.
    $status = Get-Status $err
    if ($status -ne -1) {
        return @{ status = $status; body = (Try-Json (Get-ErrBody $err)); raw = $null;
            detail = (Get-Detail $err) }
    }
    $msg = ""
    try { $msg = $err.Exception.Message } catch {}
    if ([string]::IsNullOrEmpty($msg)) { $msg = $err.ToString() }
    $bodyText = Get-ErrBody $err
    if ([string]::IsNullOrEmpty($bodyText)) { $bodyText = "<empty>" }
    if ($bodyText.Length -gt 500) { $bodyText = $bodyText.Substring(0, 500) + "..." }
    Write-Output "      NO HTTP STATUS for $uri (transport or unknown error): $msg"
    Write-Output "      response body: $bodyText"
    return @{ status = -1; body = $null; raw = $null; detail = $null }
}

function Get-Detail($err) {
    # FastAPI error bodies are JSON like {"detail": "..."}; the web cmdlets
    # stash the response body in ErrorDetails.Message.
    try {
        $msg = $err.ErrorDetails.Message
        if ([string]::IsNullOrEmpty($msg)) { return $null }
        return ($msg | ConvertFrom-Json).detail
    } catch { return $null }
}

function Try-Json($content) {
    try { return ($content | ConvertFrom-Json) } catch { return $null }
}

function Send-Get($uri, $session = $null) {
    $params = @{ Uri = $uri; UseBasicParsing = $true; TimeoutSec = 10 }
    if ($null -ne $session) { $params["WebSession"] = $session }
    try {
        $r = Invoke-SmokeRequest $params
        return @{ status = [int]$r.StatusCode; body = (Try-Json $r.Content); raw = $r }
    } catch {
        return (New-TransportResult $_ $uri)
    }
}

function Get-ErrBody($err) {
    try { return $err.ErrorDetails.Message } catch { return $null }
}

function Send-PostJson($uri, $obj, $session = $null, $origin = $null) {
    $json = ($obj | ConvertTo-Json -Compress)
    $headers = @{}
    if ($null -ne $origin) { $headers["Origin"] = $origin }
    try {
        $params = @{ Uri = $uri; Method = "Post"; ContentType = "application/json";
            Body = $json; Headers = $headers; UseBasicParsing = $true; TimeoutSec = 10 }
        if ($null -ne $session) { $params["WebSession"] = $session }
        $r = Invoke-SmokeRequest $params
        return @{ status = [int]$r.StatusCode; body = (Try-Json $r.Content); raw = $r }
    } catch {
        return (New-TransportResult $_ $uri)
    }
}

function Send-HcPost($client, $base, $path, $content) {
    # POST through System.Net.Http.HttpClient, which sends exactly what it
    # is given: no Origin, no Referer, no default request headers. Used to
    # cross-check the Invoke-WebRequest probes below.
    try {
        $resp = $client.PostAsync(($base + $path), $content).Result
        return [int]$resp.StatusCode
    } catch {
        $inner = $_.Exception
        while ($inner -is [System.AggregateException] -and $inner.InnerException) {
            $inner = $inner.InnerException
        }
        Write-Output "      NO HTTP STATUS for $path via HttpClient (transport or unknown error): $($inner.Message)"
        return -1
    }
}

function Send-PostEmpty($uri, $session = $null, $origin = $null) {
    # Bodiless POSTs (revoke, logout): same status handling as the rest.
    $headers = @{}
    if ($null -ne $origin) { $headers["Origin"] = $origin }
    $params = @{ Uri = $uri; Method = "Post"; Headers = $headers;
        UseBasicParsing = $true; TimeoutSec = 10 }
    if ($null -ne $session) { $params["WebSession"] = $session }
    try {
        $r = Invoke-SmokeRequest $params
        return @{ status = [int]$r.StatusCode; body = (Try-Json $r.Content); raw = $r }
    } catch {
        return (New-TransportResult $_ $uri)
    }
}

function New-InviteCode($label) {
    $out = & $Python (Join-Path $BackendDir "scripts\make_invite.py") --label $label --db $TempDb 2>&1
    if ($LASTEXITCODE -ne 0) { throw "make_invite failed for '$label': $out" }
    $raw = @($out | Where-Object { $_ -match '^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$' }) | Select-Object -First 1
    if (-not $raw) { throw "could not parse invite code from: $out" }
    return $raw.ToString().Trim()
}

# --- Start the server -------------------------------------------------------
# A free local port: try random high ports until one binds. The pre-bind
# trick (TcpListener on port 0) is deliberately avoided: it needs an extra
# permission some sandboxes deny, and the port can be stolen between the
# probe and the server start anyway. uvicorn exiting at once means the
# candidate was taken, so the next one is tried.
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$Secret = [Convert]::ToBase64String($bytes)

$Base = $null
$Origin = $null
$Port = $null

# Remember the parent environment so it can be restored afterwards.
$oldEnv = @{}
foreach ($name in @("DUKA_ENV", "DUKA_SESSION_SECRET", "DUKA_ALLOWED_ORIGINS", "DUKA_DB_PATH")) {
    $oldEnv[$name] = [System.Environment]::GetEnvironmentVariable($name)
}
$env:DUKA_ENV = "development"
$env:DUKA_SESSION_SECRET = $Secret
$env:DUKA_ALLOWED_ORIGINS = $Origin
$env:DUKA_DB_PATH = $TempDb

$proc = $null
$exitCode = 0
try {
    $up = $false
    for ($attempt = 1; $attempt -le 5 -and -not $up; $attempt++) {
        $Port = Get-Random -Minimum 20000 -Maximum 40000
        $Base = "http://127.0.0.1:$Port"
        $Origin = $Base
        $env:DUKA_ALLOWED_ORIGINS = $Origin
        if ($proc -ne $null -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
        $proc = Start-Process -FilePath $Python `
            -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "$Port") `
            -WorkingDirectory $BackendDir `
            -RedirectStandardOutput $ServerLog -RedirectStandardError $ServerErr `
            -WindowStyle Hidden -PassThru
        for ($i = 0; $i -lt 60; $i++) {
            if ($proc.HasExited) { break }
            try {
                $h = Invoke-WebRequest -Uri "$Base/api/health" -UseBasicParsing -TimeoutSec 3
                if ([int]$h.StatusCode -eq 200) { $up = $true; break }
            } catch {}
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $up) {
        Write-Output "FAIL  server did not answer /api/health on any of 5 candidate ports (expected 200)"
        $script:fail++
        Write-Output ""
        Write-Output "Summary: PASS $script:pass, FAIL $script:fail"
        $exitCode = 1
        if ($proc -ne $null -and $proc.HasExited) {
            $proc.Refresh()
            Write-Output "--- server exit code: $($proc.ExitCode); stderr tail: ---"
            if (Test-Path $ServerErr) { Get-Content $ServerErr -Tail 20 | Write-Output }
        }
    } else {
        Write-Output "server up at $Base (throwaway db, development mode)"

        # --- 1. Invite codes + registration over HTTP -----------------------
        $codeA = New-InviteCode "smoke-a"
        $codeB = New-InviteCode "smoke-b"
        Write-Output "created 2 single-use invite codes: $(Mask $codeA), $(Mask $codeB)"

        $sessA = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $sessB = New-Object Microsoft.PowerShell.Commands.WebRequestSession

        $phoneA = "0711111111"; $pinA = "482917"
        $phoneB = "0722222222"; $pinB = "739536"

        $regA = Send-PostJson "$Base/api/auth/register" `
            @{ phone = $phoneA; pin = $pinA; shop_name = "Smoke Shop A"; invite_code = $codeA } `
            $sessA $Origin
        Report "register Shop A" 200 $regA.status
        $regB = Send-PostJson "$Base/api/auth/register" `
            @{ phone = $phoneB; pin = $pinB; shop_name = "Smoke Shop B"; invite_code = $codeB } `
            $sessB $Origin
        Report "register Shop B" 200 $regB.status
        $shopA = $regA.body.shop_id
        $shopB = $regB.body.shop_id
        Write-Output "      shopA=$shopA shopB=$shopB"

        # --- 2. Seed test rows directly (no AI parser involved) -------------
        $seedOut = & $Python (Join-Path $BackendDir "scripts\smoke_seed.py") `
            --db $TempDb --shop-a $shopA --shop-b $shopB 2>&1
        if ($LASTEXITCODE -ne 0) { throw "smoke_seed failed: $seedOut" }
        $seed = ($seedOut | Select-Object -Last 1 | ConvertFrom-Json)
        $draftA = $seed.draft_a
        $draftB = $seed.draft_b
        Report "seed Shop A confirmed rows" 180 $seed.shop_a_confirmed
        Write-Output "      draftA=$draftA draftB=$draftB (unconfirmed)"

        # --- 3. Anonymous requests to login-required endpoints -> 401 -------
        $anonEntry = Send-PostJson "$Base/api/entry" @{ text = "sold sugar" } $null $Origin
        Report "anon POST /api/entry" 401 $anonEntry.status
        $anonConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = 1 } $null $Origin
        Report "anon POST /api/transactions/confirm" 401 $anonConfirm.status
        $boundary = "----smoke7d4a"
        $mpBody = "--$boundary`r`nContent-Disposition: form-data; name=`"audio`"; filename=`"clip.wav`"`r`nContent-Type: audio/wav`r`n`r`nnot-a-real-wav`r`n--$boundary--`r`n"
        try {
            $txParams = @{ Method = "Post"; Uri = "$Base/api/transcribe";
                ContentType = "multipart/form-data; boundary=$boundary"; Body = $mpBody;
                Headers = @{ Origin = $Origin }; UseBasicParsing = $true; TimeoutSec = 10 }
            $txResp = Invoke-SmokeRequest $txParams
            $anonTx = [int]$txResp.StatusCode
        } catch { $anonTx = (New-TransportResult $_ "$Base/api/transcribe").status }
        Report "anon POST /api/transcribe" 401 $anonTx
        Report "anon GET /api/ledger" 401 (Send-Get "$Base/api/ledger").status
        $anonPCreate = Send-PostJson "$Base/api/passports" `
            @{ lender_name = "Anon"; expires_in_days = 7 } $null $Origin
        Report "anon POST /api/passports" 401 $anonPCreate.status
        Report "anon GET /api/passports" 401 (Send-Get "$Base/api/passports").status
        $anonRevoke = (Send-PostEmpty "$Base/api/passports/1/revoke" $null $Origin).status
        Report "anon POST /api/passports/1/revoke" 401 $anonRevoke
        Report "anon GET /api/passports/1/activity" 401 (Send-Get "$Base/api/passports/1/activity").status
        Write-Output "      anonymous writes are impossible: every write above is 401"

        # --- 4. Anonymous demo reads -> 200 with the demo shop --------------
        $demoSummary = Send-Get "$Base/api/credit/summary"
        Report "anon GET /api/credit/summary" 200 $demoSummary.status
        $demoName = $demoSummary.body.shop_name
        if ($demoName -like "*Demo*") { $script:pass++; Write-Output "PASS  anon summary serves demo shop -> $demoName" }
        else { $script:fail++; Write-Output "FAIL  anon summary serves demo shop -> $demoName" }
        $demoWeekly = Send-Get "$Base/api/dashboard/weekly"
        Report "anon GET /api/dashboard/weekly" 200 $demoWeekly.status
        if ($demoWeekly.body.shop_name -like "*Demo*") { $script:pass++; Write-Output "PASS  anon weekly serves demo shop -> $($demoWeekly.body.shop_name)" }
        else { $script:fail++; Write-Output "FAIL  anon weekly serves demo shop -> $($demoWeekly.body.shop_name)" }
        # A logged-in caller sees their OWN shop, not the demo.
        $ownSummary = Send-Get "$Base/api/credit/summary" $sessA
        if ($ownSummary.status -eq 200 -and $ownSummary.body.shop_id -eq $shopA) {
            $script:pass++; Write-Output "PASS  authed summary serves own shop -> id $($ownSummary.body.shop_id)"
        } else {
            $script:fail++; Write-Output "FAIL  authed summary serves own shop -> status $($ownSummary.status) shop $($ownSummary.body.shop_id) (expected id $shopA)"
        }

        # --- 5. Ledger isolation --------------------------------------------
        $ledgerA = Send-Get "$Base/api/ledger" $sessA
        Report "Shop A reads own ledger" 200 $ledgerA.status
        $idsA = @($ledgerA.body | ForEach-Object { $_.id })
        if ($idsA.Count -eq 180 -and -not ($idsA -contains $draftB)) {
            $script:pass++; Write-Output "PASS  A ledger has 180 own rows, no B rows"
        } else {
            $script:fail++; Write-Output "FAIL  A ledger has $($idsA.Count) rows (expected 180), contains B draft: $($idsA -contains $draftB)"
        }
        $ledgerB = Send-Get "$Base/api/ledger" $sessB
        Report "Shop B reads own ledger" 200 $ledgerB.status
        $idsB = @($ledgerB.body | ForEach-Object { $_.id })
        if ($idsB.Count -eq 0 -and -not ($idsB | Where-Object { $idsA -contains $_ })) { $script:pass++; Write-Output "PASS  B ledger is empty (drafts never listed) and holds no A rows" }
        else { $script:fail++; Write-Output "FAIL  B ledger has $($idsB.Count) rows (expected 0)" }

        # --- 6. Confirm isolation -------------------------------------------
        $crossConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = $draftA } $sessB $Origin
        Report "B confirms A's draft" 404 $crossConfirm.status
        $missingConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = 999999999 } $sessB $Origin
        Report "B confirms missing id" 404 $missingConfirm.status
        if ($crossConfirm.status -eq 404 -and $missingConfirm.status -eq 404) {
            $script:pass++; Write-Output "PASS  cross-shop confirm is indistinguishable from missing (both 404)"
        } else {
            $script:fail++; Write-Output "FAIL  cross-shop confirm differs from missing: $($crossConfirm.status) vs $($missingConfirm.status)"
        }
        $ownConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = $draftA } $sessA $Origin
        Report "A confirms own draft" 200 $ownConfirm.status
        $ledgerA2 = Send-Get "$Base/api/ledger" $sessA
        if ($ledgerA2.status -eq 200 -and @($ledgerA2.body).Count -eq 181) {
            $script:pass++; Write-Output "PASS  A ledger grows to 181 after own confirm"
        } else {
            $script:fail++; Write-Output "FAIL  A ledger has $(@($ledgerA2.body).Count) rows after confirm (expected 181)"
        }

        # --- 7. Passport isolation + public lender link ----------------------
        $pc = Send-PostJson "$Base/api/passports" `
            @{ lender_name = "Smoke Lender"; expires_in_days = 7 } $sessA $Origin
        Report "A creates passport" 200 $pc.status
        $passId = $pc.body.id
        $passToken = $pc.body.token
        Write-Output "      passport id=$passId token=$(Mask $passToken)"
        $listB = Send-Get "$Base/api/passports" $sessB
        Report "B lists passports" 200 $listB.status
        $bIds = @($listB.body | ForEach-Object { $_.id })
        if ($bIds -notcontains $passId) { $script:pass++; Write-Output "PASS  B list omits A's passport" }
        else { $script:fail++; Write-Output "FAIL  B list contains A's passport id $passId" }
        $listA = Send-Get "$Base/api/passports" $sessA
        if ($listA.status -eq 200 -and (@($listA.body) | ForEach-Object { $_.id }) -contains $passId) {
            $script:pass++; Write-Output "PASS  A list contains its passport"
        } else { $script:fail++; Write-Output "FAIL  A list misses passport $passId" }
        $bRevoke = (Send-PostEmpty "$Base/api/passports/$passId/revoke" $sessB $Origin).status
        Report "B revokes A's passport" 404 $bRevoke
        Report "B reads A's passport activity" 404 (Send-Get "$Base/api/passports/$passId/activity" $sessB).status
        $pub1 = Send-Get "$Base/api/p/$passToken"
        Report "public lender link works" 200 $pub1.status
        $aRevoke = (Send-PostEmpty "$Base/api/passports/$passId/revoke" $sessA $Origin).status
        Report "A revokes own passport" 200 $aRevoke
        Report "revoked lender link" 410 (Send-Get "$Base/api/p/$passToken").status
        Report "unknown lender token" 404 (Send-Get "$Base/api/p/no-such-token").status

        # --- 8. Client-supplied shop_id is ignored (query and body) ----------
        $ledgerSpoof = Send-Get "$Base/api/ledger?shop_id=$shopB" $sessA
        Report "ledger with ?shop_id=B" 200 $ledgerSpoof.status
        $spoofIds = @($ledgerSpoof.body | ForEach-Object { $_.id })
        $sameAsOwn = ($spoofIds.Count -eq 181) -and -not ($spoofIds -contains $draftB)
        if ($sameAsOwn) { $script:pass++; Write-Output "PASS  ?shop_id=B still returns A's 181 rows" }
        else { $script:fail++; Write-Output "FAIL  ?shop_id=B returned $($spoofIds.Count) rows (expected A's 181)" }
        $sumSpoof = Send-Get "$Base/api/credit/summary?shop_id=$shopB" $sessA
        if ($sumSpoof.status -eq 200 -and $sumSpoof.body.shop_id -eq $shopA) {
            $script:pass++; Write-Output "PASS  summary with ?shop_id=B still serves shop $shopA"
        } else {
            $script:fail++; Write-Output "FAIL  summary with ?shop_id=B served shop $($sumSpoof.body.shop_id) (expected $shopA)"
        }
        $pcSpoof = Send-PostJson "$Base/api/passports" `
            @{ lender_name = "Spoof Lender"; expires_in_days = 7; shop_id = $shopB } $sessA $Origin
        Report "passport create with shop_id=B in body" 200 $pcSpoof.status
        $spoofId = $pcSpoof.body.id
        $listB2 = Send-Get "$Base/api/passports" $sessB
        $listA2 = Send-Get "$Base/api/passports" $sessA
        $bIds2 = @($listB2.body | ForEach-Object { $_.id })
        $aIds2 = @($listA2.body | ForEach-Object { $_.id })
        if (($bIds2 -notcontains $spoofId) -and ($aIds2 -contains $spoofId)) {
            $script:pass++; Write-Output "PASS  body shop_id ignored: passport $spoofId belongs to A only"
        } else {
            $script:fail++; Write-Output "FAIL  body shop_id leaked: in B list: $($bIds2 -contains $spoofId), in A list: $($aIds2 -contains $spoofId)"
        }

        # --- 9. Origin gate on state-changing requests ------------------------
        # Rejected requests must die at the Origin check: before any AI
        # call, quota charge or database write. Snapshot the AI counters
        # first, then prove they did not move.
        $aiBefore = (& $Python (Join-Path $BackendDir "scripts\smoke_counters.py") `
            --db $TempDb 2>&1 | Select-Object -Last 1 | ConvertFrom-Json).ai_calls_total
        $noOrigin = Send-PostJson "$Base/api/entry" @{ text = "sold sugar" } $sessA $null
        Report "POST /api/entry with no Origin" 403 $noOrigin.status
        $noOriginConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = $draftA } $sessA $null
        Report "POST /api/transactions/confirm with no Origin" 403 $noOriginConfirm.status
        try {
            $noOriginTxParams = @{ Method = "Post"; Uri = "$Base/api/transcribe";
                ContentType = "multipart/form-data; boundary=$boundary"; Body = $mpBody;
                WebSession = $sessA; UseBasicParsing = $true; TimeoutSec = 10 }
            $noOriginTxResp = Invoke-SmokeRequest $noOriginTxParams
            $noOriginTx = [int]$noOriginTxResp.StatusCode
        } catch { $noOriginTx = (New-TransportResult $_ "$Base/api/transcribe").status }
        Report "POST /api/transcribe with no Origin" 403 $noOriginTx
        $evilConfirm = Send-PostJson "$Base/api/transactions/confirm" @{ id = $draftA } $sessA "https://evil.example"
        Report "POST /api/transactions/confirm with foreign Origin" 403 $evilConfirm.status
        # --- 9b. Same probes with NO implicit headers possible ---------------
        # The Invoke-WebRequest probes above pass an empty @{} hashtable (entry,
        # confirm) or no -Headers at all (transcribe), plus the login cookie
        # from the session jar. Print exactly that, then repeat all three
        # probes through System.Net.Http.HttpClient, which cannot add Origin
        # or Referer on its own. If IWR and HttpClient disagree, the extra
        # header came from the IWR stack; if both pass, it came from the
        # path between client and server.
        Write-Output "      IWR no-Origin probe headers: no Origin key (empty @{}), no Referer key; entry/confirm/transcribe alike"
        Write-Output "      WebSession default headers on sessA: $($sessA.Headers.Count) (cookies live in the jar, not headers)"
        Add-Type -AssemblyName System.Net.Http
        $hcHandler = New-Object System.Net.Http.HttpClientHandler
        $hcJar = New-Object System.Net.CookieContainer
        $hcHandler.CookieContainer = $hcJar
        $hcHandler.UseCookies = $true
        $hcHandler.AllowAutoRedirect = $false
        $hcClient = New-Object System.Net.Http.HttpClient($hcHandler)
        $hcUri = New-Object System.Uri($Base)
        foreach ($cc in $sessA.Cookies.GetCookies($hcUri)) { $hcJar.Add($hcUri, $cc) }
        Write-Output "      HttpClient default request headers: $($hcClient.DefaultRequestHeaders.Count) (must be 0)"
        Write-Output "      cookies copied to HttpClient jar: $($hcJar.Count)"
        $hcEntryContent = New-Object System.Net.Http.StringContent('{"text":"sold sugar"}', [System.Text.Encoding]::UTF8, "application/json")
        Write-Output "      HttpClient entry content headers: $($hcEntryContent.Headers.ToString().Replace("`r`n", '; '))"
        $hcEntry = Send-HcPost $hcClient $Base "/api/entry" $hcEntryContent
        Report "POST /api/entry with no Origin (HttpClient)" 403 $hcEntry
        $hcConfirmContent = New-Object System.Net.Http.StringContent(('{"id":' + $draftA + '}'), [System.Text.Encoding]::UTF8, "application/json")
        Write-Output "      HttpClient confirm content headers: $($hcConfirmContent.Headers.ToString().Replace("`r`n", '; '))"
        $hcConfirm = Send-HcPost $hcClient $Base "/api/transactions/confirm" $hcConfirmContent
        Report "POST /api/transactions/confirm with no Origin (HttpClient)" 403 $hcConfirm
        $hcMp = New-Object System.Net.Http.MultipartFormDataContent
        $hcBytes = New-Object System.Net.Http.ByteArrayContent([System.Text.Encoding]::UTF8.GetBytes("not-a-real-wav"))
        $hcMp.Add($hcBytes, "audio", "clip.wav")
        Write-Output "      HttpClient transcribe content headers: $($hcMp.Headers.ToString().Replace("`r`n", '; '))"
        $hcTx = Send-HcPost $hcClient $Base "/api/transcribe" $hcMp
        Report "POST /api/transcribe with no Origin (HttpClient)" 403 $hcTx
        $hcClient.Dispose()
        $aiAfter = (& $Python (Join-Path $BackendDir "scripts\smoke_counters.py") `
            --db $TempDb 2>&1 | Select-Object -Last 1 | ConvertFrom-Json).ai_calls_total
        if ($aiAfter -eq $aiBefore) {
            $script:pass++; Write-Output "PASS  AI usage counters unchanged by rejected requests ($aiBefore -> $aiAfter)"
        } else {
            $script:fail++; Write-Output "FAIL  AI usage counters moved on rejected requests ($aiBefore -> $aiAfter, expected no change)"
        }

        # --- 10. Invite codes: reuse, wrong code, weak PIN --------------------
        # Fresh phones so each attempt reaches the invite check (a duplicate
        # phone would 409 before the code is examined).
        $reuse = Send-PostJson "$Base/api/auth/register" `
            @{ phone = "0744444444"; pin = "295184"; shop_name = "Smoke Reuse"; invite_code = $codeA } `
            $null $Origin
        Report "reused invite code" 403 $reuse.status
        $wrong = Send-PostJson "$Base/api/auth/register" `
            @{ phone = "0755555555"; pin = "295184"; shop_name = "Smoke Wrong"; invite_code = "WRONG-CODE-123" } `
            $null $Origin
        Report "wrong invite code" 403 $wrong.status
        if ($reuse.detail -eq "Invalid invite code." -and $wrong.detail -eq $reuse.detail) {
            $script:pass++; Write-Output "PASS  reuse and wrong code share the generic 403 detail"
        } else {
            $script:fail++; Write-Output "FAIL  invite error details differ: '$($reuse.detail)' vs '$($wrong.detail)'"
        }
        $codeC = New-InviteCode "smoke-c"
        Write-Output "      fresh code C for weak-PIN test: $(Mask $codeC)"
        $weak = Send-PostJson "$Base/api/auth/register" `
            @{ phone = "0733333333"; pin = "111111"; shop_name = "Smoke Shop C"; invite_code = $codeC } `
            $null $Origin
        Report "weak PIN rejected" 422 $weak.status
        $regC = Send-PostJson "$Base/api/auth/register" `
            @{ phone = "0733333333"; pin = "295184"; shop_name = "Smoke Shop C"; invite_code = $codeC } `
            $null $Origin
        Report "same code works with a strong PIN (not consumed by weak PIN)" 200 $regC.status

        # --- 11. Cookie flags on login ---------------------------------------
        $sessLogin = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $loginParams = @{ Uri = "$Base/api/auth/login"; Method = "Post";
            ContentType = "application/json";
            Body = (@{ phone = $phoneA; pin = $pinA } | ConvertTo-Json -Compress);
            Headers = @{ Origin = $Origin }; WebSession = $sessLogin;
            UseBasicParsing = $true; TimeoutSec = 10 }
        try {
            $loginResp = Invoke-SmokeRequest $loginParams
            $loginStatus = [int]$loginResp.StatusCode
            $setCookie = @($loginResp.Headers["Set-Cookie"]) -join "`n"
        } catch {
            $loginStatus = (New-TransportResult $_ "$Base/api/auth/login").status
            $setCookie = ""
        }
        Report "login Shop A" 200 $loginStatus
        $flagsOk = ($setCookie -match "(?i)httponly") -and ($setCookie -match "(?i)samesite=lax")
        if ($flagsOk) { $script:pass++; Write-Output "PASS  Set-Cookie has HttpOnly and SameSite=Lax" }
        else { $script:fail++; Write-Output "FAIL  Set-Cookie flags wrong: $setCookie" }
        if ($setCookie -notmatch "(?i); secure") { $script:pass++; Write-Output "PASS  Set-Cookie has no Secure flag (development mode only)" }
        else { $script:fail++; Write-Output "FAIL  Set-Cookie unexpectedly carries Secure in development" }
        $rawCookie = $null
        if ($setCookie -match "duka_session=([^;]+)") { $rawCookie = $Matches[1] }

        # --- 12. Logout invalidates the session ------------------------------
        $logoutStatus = (Send-PostEmpty "$Base/api/auth/logout" $sessLogin $Origin).status
        Report "logout Shop A" 200 $logoutStatus
        # Replay the raw cookie value: the server must reject it (revoked).
        $meAfter = Send-Get "$Base/api/auth/me"
        Report "anon GET /api/auth/me" 401 $meAfter.status
        if ($null -ne $rawCookie) {
            try {
                $replayParams = @{ Uri = "$Base/api/auth/me";
                    Headers = @{ Cookie = "duka_session=$rawCookie" };
                    UseBasicParsing = $true; TimeoutSec = 10 }
                $replayResp = Invoke-SmokeRequest $replayParams
                $replay = [int]$replayResp.StatusCode
            } catch { $replay = (New-TransportResult $_ "$Base/api/auth/me").status }
            Report "replayed session cookie after logout" 401 $replay
        } else {
            $script:fail++; Write-Output "FAIL  no session cookie captured at login, replay not tested"
        }

        # --- 13. Login lockout (dedicated Shop C phone) -----------------------
        $lockStatuses = @()
        for ($k = 0; $k -lt 3; $k++) {
            $bad = Send-PostJson "$Base/api/auth/login" `
                @{ phone = "0733333333"; pin = "000001" } $null $Origin
            $lockStatuses += $bad.status
        }
        Report "3 wrong PINs for Shop C" "401,401,401" ($lockStatuses -join ",")
        $lockedGood = Send-PostJson "$Base/api/auth/login" `
            @{ phone = "0733333333"; pin = "295184" } $null $Origin
        Report "correct PIN refused while locked" 401 $lockedGood.status

        Write-Output ""
        Write-Output "Summary: PASS $script:pass, FAIL $script:fail"
        if ($script:fail -gt 0) { $exitCode = 1 }
    }
} finally {
    # ALWAYS stop the server and delete the throwaway files, even on failure.
    try {
        if ($proc -ne $null -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    foreach ($name in @("DUKA_ENV", "DUKA_SESSION_SECRET", "DUKA_ALLOWED_ORIGINS", "DUKA_DB_PATH")) {
        if ($null -eq $oldEnv[$name]) {
            Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
        } else {
            [System.Environment]::SetEnvironmentVariable($name, $oldEnv[$name])
        }
    }
    Remove-Item $TempDb -Force -ErrorAction SilentlyContinue
    Remove-Item "$TempDb-journal" -Force -ErrorAction SilentlyContinue
    Remove-Item $ServerLog -Force -ErrorAction SilentlyContinue
    Remove-Item $ServerErr -Force -ErrorAction SilentlyContinue
}
exit $exitCode



