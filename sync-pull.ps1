Set-Location "$env:USERPROFILE\alice-company"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Pull latest from Mac
git pull origin main 2>&1 | Out-File -Append "$env:USERPROFILE\alice-company\sync.log"

# Push Windows changes to Mac
git add -A 2>&1 | Out-File -Append "$env:USERPROFILE\alice-company\sync.log"
$status = git status --porcelain
if ($status) {
    git commit -m "sync(windows): $timestamp" 2>&1 | Out-File -Append "$env:USERPROFILE\alice-company\sync.log"
    git push origin main 2>&1 | Out-File -Append "$env:USERPROFILE\alice-company\sync.log"
}
