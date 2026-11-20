# Builds feedline.apk without Gradle: aapt2 + javac + d8 + zipalign + apksigner from the Android SDK.
# Needs: a JDK (11+) and the SDK with "platforms;android-34" and "build-tools;34.0.0".
#   powershell -File build.ps1 [-Sdk C:\path\to\android-sdk]
param(
  [string]$Sdk = "$env:USERPROFILE\android-sdk",
  [string]$Platform = "android-34",
  [string]$BuildTools = "35.0.0",     # 34.0.0's d8 crashes on classes compiled by JDK 24
  [string]$Keystore = "$env:USERPROFILE\.android\feedline.jks"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$bt = Join-Path $Sdk "build-tools\$BuildTools"
$jar = Join-Path $Sdk "platforms\$Platform\android.jar"
$out = Join-Path $here "build"
if (Test-Path $out) { Remove-Item -Recurse -Force $out -Confirm:$false }
New-Item -ItemType Directory -Force "$out\classes" | Out-Null

function Run($exe, $argv) { & $exe @argv; if ($LASTEXITCODE -ne 0) { throw "$exe failed ($LASTEXITCODE)" } }

Run "$bt\aapt2.exe" @("compile", "--dir", "$here\res", "-o", "$out\res.zip")
Run "$bt\aapt2.exe" @("link", "-o", "$out\base.apk", "-I", $jar, "--manifest", "$here\AndroidManifest.xml",
  "--min-sdk-version", "24", "--target-sdk-version", "34", "--version-code", "1", "--version-name", "1.0", "$out\res.zip")
$src = Get-ChildItem "$here\src" -Recurse -Filter *.java | ForEach-Object { $_.FullName }
Run "javac" (@("--release", "11", "-Xlint:-options", "-cp", $jar, "-d", "$out\classes") + $src)
$cls = Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName }
Run "$bt\d8.bat" (@("--lib", $jar, "--release", "--output", $out) + $cls)

Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open("$out\base.apk", "Update")
[void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$out\classes.dex", "classes.dex")
$zip.Dispose()

Run "$bt\zipalign.exe" @("-f", "4", "$out\base.apk", "$out\aligned.apk")
if (-not (Test-Path $Keystore)) {
  New-Item -ItemType Directory -Force (Split-Path $Keystore) | Out-Null
  Run "keytool" @("-genkeypair", "-keystore", $Keystore, "-alias", "feedline", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
    "-storepass", "feedline", "-keypass", "feedline", "-dname", "CN=Feedline")
}
Run "$bt\apksigner.bat" @("sign", "--ks", $Keystore, "--ks-pass", "pass:feedline", "--key-pass", "pass:feedline", "--out", "$here\feedline.apk", "$out\aligned.apk")
Run "$bt\apksigner.bat" @("verify", "$here\feedline.apk")
"built $here\feedline.apk ($([int]((Get-Item "$here\feedline.apk").Length/1KB)) KB)"
