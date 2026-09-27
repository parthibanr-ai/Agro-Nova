<#
  Moves the Android emulator window onto the visible screen and zooms it out so the whole phone fits.
  The Pixel_10 emulator opens partly above the top edge on this PC (its window is taller than the display).

  Usage (with the emulator already running):
      .\tools\fit_emulator.ps1              # zoom out until the window fits the screen (default)
      .\tools\fit_emulator.ps1 -ZoomOut 2   # zoom out exactly this many steps instead of measuring
#>
param([int]$ZoomOut = 0)

Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System; using System.Runtime.InteropServices;
public struct RECT { public int Left, Top, Right, Bottom; }
public class EmuWin {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
}
'@

$p = Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
if (-not $p) { Write-Error "No running emulator window found. Start it first: flutter emulators --launch Pixel_10"; exit 1 }

$h = $p.MainWindowHandle
$work = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$targetX = 20
$targetY = 20
$maxHeight = $work.Height - $targetY - 20   # leave a margin at the bottom

[EmuWin]::ShowWindow($h, 9) | Out-Null                              # restore if minimised
[EmuWin]::SetWindowPos($h, [IntPtr]::Zero, $targetX, $targetY, 0, 0, 0x0005) | Out-Null  # move only (SWP_NOSIZE|SWP_NOZORDER)
[EmuWin]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 800

if ($ZoomOut -gt 0) {
    1..$ZoomOut | ForEach-Object { [System.Windows.Forms.SendKeys]::SendWait('^{DOWN}'); Start-Sleep -Milliseconds 600 }
    Write-Host "Emulator window moved into view and zoomed out $ZoomOut step(s)."
    exit 0
}

$steps = 0
$maxSteps = 15
while ($steps -lt $maxSteps) {
    $rect = New-Object RECT
    [EmuWin]::GetWindowRect($h, [ref]$rect) | Out-Null
    $height = $rect.Bottom - $rect.Top
    if ($height -le $maxHeight) { break }
    [System.Windows.Forms.SendKeys]::SendWait('^{DOWN}')
    Start-Sleep -Milliseconds 600
    $steps++
}

if ($steps -ge $maxSteps) {
    Write-Warning "Zoomed out $steps steps but the window may still not fully fit. Press Ctrl+Down manually to shrink it further."
} else {
    Write-Host "Emulator window moved into view and zoomed out $steps step(s) to fit your $($work.Width)x$($work.Height) screen."
}
