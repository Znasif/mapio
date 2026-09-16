# Stop the MapIO Uno Q demo (pipeline on the board + camera stream server on the laptop).
# Frees the HUE webcam so `mapio.py --camera 1` can use it directly.
$adb = "C:\Users\qc_de\AppData\Local\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe"
try { & $adb shell "bash /home/arduino/unoq_run_pipeline.sh stop" } catch { Write-Host "[board] not reachable, skipping" }

# Kill every camera_stream_server, whichever Python launched it. (Get-CimInstance, not
# Get-Process: the latter has no CommandLine property in Windows PowerShell 5.1.)
$servers = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "camera_stream_server" }
foreach ($s in $servers) { Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item "$env:TEMP\mapio_stream.pid" -ErrorAction SilentlyContinue

Start-Sleep 1
$left = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match "camera_stream_server" }
if ($left) { Write-Host "[laptop] WARNING: stream server still running (PID $($left.ProcessId))" }
else       { Write-Host "[laptop] stream server stopped ($($servers.Count) killed). Camera is free." }
