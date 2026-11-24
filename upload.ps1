# 上传项目到 VPS（在本机 Windows 上运行）
# 用法：pwsh -File scripts/upload.ps1 -Ip 100.x.x.x
# 原理：tar 打包（排除 .venv/.git/临时文件等本地垃圾）→ scp → VPS 上解包
param([string]$Ip = "")

if (-not $Ip) {
    $Ip = Read-Host "VPS 的 Tailscale IP（tailscale ip -4 查看）"
}

$root = Split-Path -Parent $PSScriptRoot      # jarvis-hub 目录
$parent = Split-Path -Parent $root            # 上级目录
$tarball = Join-Path $parent "jarvis-hub-upload.tar.gz"

Write-Host "打包 jarvis-hub（排除 .venv/.tmp/.git/backups/.env 等）..."
tar czf $tarball `
  --exclude=.venv --exclude=.tmp --exclude=.git --exclude=backups `
  --exclude=.env --exclude="pytest-cache-files-*" --exclude=".pytest-*" `
  --exclude="*.egg-info" --exclude=__pycache__ -C $parent jarvis-hub
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

Write-Host "上传到 root@${Ip}:/root/ ..."
scp $tarball "root@${Ip}:/root/jarvis-hub-upload.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "上传失败" }
Remove-Item $tarball

Write-Host ""
Write-Host "上传完成。SSH 登录 VPS 后执行："
Write-Host "  ssh root@${Ip}"
Write-Host "  mkdir -p /root/jarvis-hub && tar xzf /root/jarvis-hub-upload.tar.gz -C /root/jarvis-hub --strip-components=1 && rm /root/jarvis-hub-upload.tar.gz"
Write-Host "  cd /root/jarvis-hub && bash scripts/deploy.sh"
