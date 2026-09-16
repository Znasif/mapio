# Start the MapIO Uno Q demo: laptop streams the HUE webcam -> Uno Q perception -> speech in Ray-Ban Meta.
#   .\tools\unoq_start.ps1            (camera index defaults to 1 = HUE HD Pro; 0 is the laptop webcam)
#   .\tools\unoq_start.ps1 -Camera 0
#   .\tools\unoq_start.ps1 -Remap       (only when the BART tactile map is under the camera; the NY map needs no remap)
# Dashboard: http://127.0.0.1:5001/      Stop with .\tools\unoq_stop.ps1
param([int]$Camera = 1, [switch]$Remap)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$adb  = "C:\Users\qc_de\AppData\Local\Arduino15\packages\arduino\tools\adb\32.0.0\adb.exe"

Write-Host "[1/4] checking Uno Q over adb..."
if (-not ((& $adb devices) -match "device$")) { throw "Uno Q not found by adb - is its USB-C cable plugged in?" }

Write-Host "[2/4] starting camera stream server (camera $Camera) ..."
# Windows .venv on purpose: the webcam is a Windows USB device, WSL cannot see it.
$stream = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "tools\camera_stream_server.py","--camera",$Camera,"--width","640","--height","480","--fps","20" `
    -WorkingDirectory $root -PassThru -WindowStyle Minimized
Set-Content "$env:TEMP\mapio_stream.pid" $stream.Id
Start-Sleep 4
& $adb forward tcp:5001 tcp:5001 | Out-Null

Write-Host "[3/4] pushing pipeline to the board..."
& $adb push "$root\tools\unoq_perception_pipeline.py" /home/arduino/unoq_perception_pipeline.py | Out-Null
& $adb push "$root\tools\unoq_run_pipeline.sh"        /home/arduino/unoq_run_pipeline.sh | Out-Null
# The board runs mapio's own graph engine; ship just the packages it needs (no UI/LLM code).
foreach ($d in "config","utils","graph","position") { & $adb push "$root\src\$d" /home/arduino/src/$d | Out-Null }
& $adb push "$root\src\modules_repository.py" /home/arduino/src/modules_repository.py | Out-Null
& $adb push "$root\models\new_york\new_york.json" /home/arduino/models/new_york/new_york.json | Out-Null

Write-Host "[4/4] starting perception + speech on the board..."
$extra = if ($Remap) { "--remap" } else { "" }
& $adb shell "bash /home/arduino/unoq_run_pipeline.sh start $extra"
Write-Host ""
Write-Host "Running. Dashboard: http://127.0.0.1:5001/   (you should hear 'Uno Q ready' in the glasses in ~20 s)"
Write-Host "Board log:  & '$adb' shell 'bash /home/arduino/unoq_run_pipeline.sh log'"
Write-Host "Stop:       .\tools\unoq_stop.ps1"
