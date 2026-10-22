$ErrorActionPreference = "Stop"

# Alfresco's search webscript does not send a WWW-Authenticate challenge, and
# Invoke-RestMethod -Credential only authenticates after one, so send Basic
# preemptively.
$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("admin:admin"))
$headers = @{ Authorization = "Basic $basic" }

Write-Host "--- repo ready probe"
(Invoke-RestMethod "http://localhost:8080/alfresco/api/-default-/public/alfresco/versions/1/probes/-ready-").entry.message

Write-Host "--- elasticsearch indices"
Invoke-RestMethod "http://localhost:9200/_cat/indices?v" | Write-Host

Write-Host "--- AFTS smoke test"
$body = '{"query":{"query":"TYPE:\"cm:content\"","language":"afts"},"paging":{"maxItems":5}}'
$r = Invoke-RestMethod -Method Post `
    -Uri "http://localhost:8080/alfresco/api/-default-/public/search/versions/1/search" `
    -Headers $headers -ContentType "application/json" -Body $body
Write-Host ("totalItems: " + $r.list.pagination.totalItems)
$r.list.entries.entry | Select-Object name, id | Format-Table
