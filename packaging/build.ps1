# 一键打包：生成 exe（dist\DeepSeekLauncher）与安装包（dist\installer\*.exe）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    Write-Host "==> 生成图标"
    py "$PSScriptRoot\make_icon.py"

    Write-Host "==> PyInstaller 打包"
    py -m PyInstaller "$PSScriptRoot\DeepSeekLauncher.spec" --noconfirm --clean --distpath dist --workpath build
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）。请先关闭正在运行的 DeepSeekLauncher.exe 和 Ollama 后重试" }

    $version = (Get-Content "$root\VERSION" -Raw).Trim()
    Write-Host "==> 编译安装包 v$version"

    $iscc = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "未找到 Inno Setup 的 ISCC.exe，请先安装 Inno Setup 6" }

    & $iscc "/DMyAppVersion=$version" "$PSScriptRoot\setup.iss"
    if ($LASTEXITCODE -ne 0) { throw "安装包编译失败（ISCC 退出码 $LASTEXITCODE）" }

    Write-Host "==> 完成"
    Write-Host "程序目录: $root\dist\DeepSeekLauncher"
    Write-Host "安装包:   $root\dist\installer"
}
finally {
    Pop-Location
}
