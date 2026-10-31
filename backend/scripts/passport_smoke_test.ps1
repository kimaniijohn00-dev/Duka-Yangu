# Business Passport smoke test: end-to-end flow against a running server.
#
# Run from the project root in one go (server must already be up):
#   powershell -ExecutionPolicy Bypass -File backend/scripts/passport_smoke_test.ps1
#
# The raw share token is NEVER printed in full: only its first 6 chars + "...".
# Expiry-by-clock is covered by the pytest suite (backend/tests/test_passport.py),
# which controls the clock directly; this script covers everything else live.

$base = "http://127.0.0.1:8000"

$pass = 0
$fail = 0
$publicCalls = 0  # every GET /api/p/* counts toward the 30 req/min IP limit

function Show-Token($token) {
    return $token.Substring(0, [Math]::Min(6, $token.Length)) + "..."
}

function Report($name, $expected, $actual) {
    if ($expected -eq $actual) {
        $script:pass++
        Write-Output "PASS  $name -> $actual (expected $expected)"
    } else {
        $script:fail++
        Write-Output "FAIL  $name -> $actual (expected $expected)"
    }
}

function Get-ErrorStatus($err) {
    $resp = $err.Exception.Response
    if ($null -ne $resp -and $null -ne $resp.StatusCode) {
        return [int]$resp.StatusCode
    }
    return -1
}

function Post-Json($uri, $body) {
    try {
        $r = Invoke-RestMethod -Method Post -Uri $uri `
            -ContentType "application/json" -Body ($body | ConvertTo-Json)
        return @{ status = 200; body = $r }
    } catch {
        return @{ status = (Get-ErrorStatus $_); body = $null }
    }
}

function Public-Get($token) {
    $script:publicCalls++
    try {
        $r = Invoke-RestMethod -Uri "$base/api/p/$token"
        return @{ status = 200; body = $r }
    } catch {
        return @{ status = (Get-ErrorStatus $_); body = $null }
    }
}

# 0. Server must be up before anything else runs.
try {
    $list = Invoke-RestMethod -Uri "$base/api/passports"
    Report "server reachable (list passports)" 200 200
} catch {
    Report "server reachable (list passports)" 200 (Get-ErrorStatus $_)
    Write-Output ""
    Write-Output "Summary: PASS $pass, FAIL $fail"
    Write-Output "Is the server running? See step 1 of the chat instructions."
    exit 1
}

# 1. Create a passport (defaults to the demo shop).
$created = Post-Json "$base/api/passports" @{ lender_name = "Equity Bank"; expires_in_days = 7 }
Report "create passport" 200 $created.status
$token = $created.body.token
$id = $created.body.id
Write-Output "      id=$id token=$(Show-Token $token) share_path=/p/$(Show-Token $token)"

# 2. The lender link works and carries every section by default.
$view = Public-Get $token
Report "open lender link $(Show-Token $token)" 200 $view.status
$hasAll = $view.status -eq 200 -and $null -ne $view.body.score `
    -and $null -ne $view.body.loan -and $null -ne $view.body.performance `
    -and $null -ne $view.body.shop_name -and $null -ne $view.body.disclaimer
if ($hasAll) { $script:pass++; Write-Output "PASS  snapshot has shop_name, disclaimer, score, loan, performance" }
else { $script:fail++; Write-Output "FAIL  snapshot has shop_name, disclaimer, score, loan, performance" }

# 3. Scoped passport: unchosen sections are omitted.
$scoped = Post-Json "$base/api/passports" `
    @{ lender_name = "KCB"; expires_in_days = 30; sections = @("score") }
Report "create scoped passport (score only)" 200 $scoped.status
$scopedView = Public-Get $scoped.body.token
Report "open scoped link $(Show-Token $scoped.body.token)" 200 $scopedView.status
$scopedOk = $scopedView.status -eq 200 -and $null -ne $scopedView.body.score `
    -and $null -eq $scopedView.body.loan -and $null -eq $scopedView.body.performance
if ($scopedOk) { $script:pass++; Write-Output "PASS  scoped snapshot omits loan and performance" }
else { $script:fail++; Write-Output "FAIL  scoped snapshot omits loan and performance" }

# 4. List shows the passport with a view count, and no token material.
try {
    $listed = Invoke-RestMethod -Uri "$base/api/passports"
    Report "list passports" 200 200
    $mine = @($listed) | Where-Object { $_.id -eq $id }
    if ($mine.view_count -ge 1) { $script:pass++; Write-Output "PASS  view_count >= 1 -> $($mine.view_count) (expected >= 1)" }
    else { $script:fail++; Write-Output "FAIL  view_count >= 1 -> $($mine.view_count) (expected >= 1)" }
    $dump = $listed | ConvertTo-Json -Depth 5
    if ($dump -notmatch [regex]::Escape($token)) { $script:pass++; Write-Output "PASS  list output contains no token" }
    else { $script:fail++; Write-Output "FAIL  list output contains no token" }
} catch {
    Report "list passports" 200 (Get-ErrorStatus $_)
}

# 5. Activity lists the view timestamps.
try {
    $activity = Invoke-RestMethod -Uri "$base/api/passports/$id/activity"
    Report "passport activity" 200 200
    if (@($activity.views).Count -ge 1) { $script:pass++; Write-Output "PASS  activity views >= 1 -> $(@($activity.views).Count) (expected >= 1)" }
    else { $script:fail++; Write-Output "FAIL  activity views >= 1 -> $(@($activity.views).Count) (expected >= 1)" }
} catch {
    Report "passport activity" 200 (Get-ErrorStatus $_)
}

# 6. Revoke: the link dies immediately with 410.
try {
    $revoked = Invoke-RestMethod -Method Post -Uri "$base/api/passports/$id/revoke"
    Report "revoke passport" 200 200
} catch {
    Report "revoke passport" 200 (Get-ErrorStatus $_)
}
$dead = Public-Get $token
Report "revoked link $(Show-Token $token)" 410 $dead.status

# 7. Revoking again is idempotent (still 200).
try {
    Invoke-RestMethod -Method Post -Uri "$base/api/passports/$id/revoke" | Out-Null
    Report "revoke again (idempotent)" 200 200
} catch {
    Report "revoke again (idempotent)" 200 (Get-ErrorStatus $_)
}

# 8. Unknown token is a generic 404.
$missing = Public-Get "no-such-token"
Report "unknown token" 404 $missing.status

# 9. Bad input is rejected with 422.
$bad1 = Post-Json "$base/api/passports" @{ lender_name = "   "; expires_in_days = 7 }
Report "blank lender_name rejected" 422 $bad1.status
$bad2 = Post-Json "$base/api/passports" @{ lender_name = "KCB"; expires_in_days = 10 }
Report "expires_in_days=10 rejected" 422 $bad2.status
$bad3 = Post-Json "$base/api/passports" `
    @{ lender_name = "KCB"; expires_in_days = 7; sections = @("everything") }
Report "unknown section rejected" 422 $bad3.status

# 10. Rate limit: a fresh link serves 200s, then the 31st request in the
#     per-minute IP window answers 429. Earlier public calls above already
#     consumed part of the window, so count them.
$fresh = Post-Json "$base/api/passports" @{ lender_name = "Rate Test"; expires_in_days = 7 }
Report "create rate-limit passport" 200 $fresh.status
$remaining = 30 - $script:publicCalls
$ok200 = 0
$got429At = 0
for ($i = 1; $i -le ($remaining + 1); $i++) {
    $r = Public-Get $fresh.body.token
    if ($r.status -eq 200) { $ok200++ }
    elseif ($r.status -eq 429) { $got429At = $i; break }
    else { break }
}
if ($ok200 -eq $remaining -and $got429At -eq ($remaining + 1)) {
    $script:pass++
    Write-Output "PASS  rate limit -> 429 on window request number 31 (expected 429)"
} else {
    $script:fail++
    Write-Output "FAIL  rate limit -> 200 x $ok200 then 429 at #$got429At (expected 200 x $remaining then 429)"
}

Write-Output ""
Write-Output "Summary: PASS $pass, FAIL $fail"
if ($fail -gt 0) { exit 1 } else { exit 0 }
