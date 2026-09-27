<#
  Moves the Android emulator window onto the visible screen and zooms it out so the whole phone fits.
  The Pixel_10 emulator opens partly above the top edge on this PC (its window is taller than the display).

  Usage (with the emulator already running):
      .\tools\fit_emulator.ps1            # zoom out 3 steps (default)
      .\tools\fit_emulator.ps1 -ZoomOut 2 # fewer steps = bigger phone
#>
param([int]$ZoomOut = 3)

Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System; using System.Runtime.InteropServices;
public class EmuWin {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
'@

$p = Get-Process qemu-system-x86_64 -ErrorAction SilentlyContinue | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
if (-not $p) { Write-Error "No running emulator window found. Start it first: flutter emulators --launch Pixel_10"; exit 1 }

$h = $p.MainWindowHandle
[EmuWin]::ShowWindow($h, 9) | Out-Null                              # restore if minimised
[EmuWin]::SetWindowPos($h, [IntPtr]::Zero, 150, 20, 0, 0, 0x0005) | Out-Null  # move only (SWP_NOSIZE|SWP_NOZORDER)
[EmuWin]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 800
1..$ZoomOut | ForEach-Object { [System.Windows.Forms.SendKeys]::SendWait('^{DOWN}'); Start-Sleep -Milliseconds 600 }
Write-Host "Emulator window moved into view and zoomed out $ZoomOut step(s)."
