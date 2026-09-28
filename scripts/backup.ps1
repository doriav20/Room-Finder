# Backs up RoomFinder's database without stopping it, for Windows hosts (see HOSTING.md).
# Keeps 30 days of daily copies. Run it from Task Scheduler, or by hand:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup.ps1
param(
    [string]$Folder = (Join-Path $HOME "RoomFinderBackups"),
    [int]$KeepDays = 30
)

New-Item -ItemType Directory -Force -Path $Folder | Out-Null

# SQLite's backup API gives a consistent copy while the app keeps writing
docker exec roomfinder python -c "import sqlite3; sqlite3.connect('/data/roomfinder.db').backup(sqlite3.connect('/data/backup.db'))"
if ($LASTEXITCODE -ne 0) { throw "RoomFinder backup failed: is the container running?" }

$target = Join-Path $Folder ("roomfinder-{0:yyyy-MM-dd}.db" -f (Get-Date))
docker cp roomfinder:/data/backup.db $target
if ($LASTEXITCODE -ne 0) { throw "Copying the RoomFinder backup out of the container failed" }

Get-ChildItem -Path $Folder -Filter "roomfinder-*.db" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item
