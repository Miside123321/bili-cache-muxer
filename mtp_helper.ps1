param(
    [Parameter(Mandatory=$true)][string]$ArgsFile,
    [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 抑制 Windows 在 MTP 设备掉线时弹出的系统错误对话框
# SEM_FAILCRITICALERRORS=0x0001, SEM_NOOPENFILEERRORBOX=0x8000
try {
    $sig = @'
using System;
using System.Runtime.InteropServices;
public static class NM {
    [DllImport("kernel32.dll")] public static extern uint SetErrorMode(uint uMode);
}
'@
    Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue
    [void][NM]::SetErrorMode(0x0001 -bor 0x8000)
} catch {}

$script:entryList = New-Object System.Collections.Generic.List[string]
$script:entryCount = 0

function Write-Result($obj) {
    $json = $obj | ConvertTo-Json -Depth 12 -Compress
    [IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($true)))
}

function Fail($code, $msg) {
    Write-Result ([pscustomobject]@{ ok = $false; code = $code; message = $msg })
    exit 0
}

function Walk-Tree($folder, $rel, $depth, $progFile) {
    if ($depth -gt 8) { return }
    $items = @($folder.Items())
    foreach ($it in $items) {
        if (-not $it.IsFolder) {
            if ($it.Name -eq 'entry.json') {
                $script:entryList.Add($rel)
                $script:entryCount++
                try {
                    [IO.File]::WriteAllText($progFile, $script:entryCount.ToString(),
                        (New-Object System.Text.UTF8Encoding($false)))
                } catch {}
            }
            continue
        }
        $name = [string]$it.Name
        if ($name -eq 'cache' -or $name -eq 'temp' -or $name -eq 'thumb') { continue }
        $sub = if ($rel) { $rel + '/' + $name } else { $name }
        try {
            Walk-Tree $it.GetFolder() $sub ($depth + 1) $progFile
        } catch {
            # 单个目录访问失败（系统保护目录等）跳过，不中断整体遍历
        }
    }
}

try {
    $raw = [IO.File]::ReadAllText($ArgsFile, [System.Text.Encoding]::UTF8)
    $a = $raw | ConvertFrom-Json

    $shell = New-Object -ComObject Shell.Application

    function Get-Device($name) {
        if ([string]::IsNullOrWhiteSpace($name)) { return $null }
        $name = $name.Trim()
        foreach ($it in $shell.NameSpace(17).Items()) {
            if ($null -eq $it) { continue }
            $n = [string]$it.Name
            if ($n.Trim() -eq $name) { return $it }
        }
        return $null
    }

    function Get-DeviceList {
        $list = @()
        foreach ($it in $shell.NameSpace(17).Items()) {
            if ($null -eq $it) { continue }
            if ($it.IsFolder -and -not [string]::IsNullOrWhiteSpace([string]$it.Name)) {
                $list += [string]$it.Name
            }
        }
        return ,$list
    }

    # Storage name match built from char codes so this script stays pure ASCII
    # (matches Chinese names like 内部存储设备/内部存储/手机存储/SD卡 and English variants)
    function Get-Storage($deviceFolder) {
        $cnNeibu  = [string]([char]0x5185) + [char]0x90E8 + [char]0x5B58 + [char]0x50A8
        $cnShouji = [string]([char]0x624B) + [char]0x673A + [char]0x5B58 + [char]0x50A8
        $sdKa     = 'SD' + [string]([char]0x5361)
        $rx = $cnNeibu + '|Internal shared storage|Internal storage|Phone storage|' + $cnShouji + '|' + $sdKa
        $fallback = $null
        foreach ($it in $deviceFolder.Items()) {
            if (-not $it.IsFolder) { continue }
            if ($fallback -eq $null) { $fallback = $it }
            if ($it.Name -match $rx) { return $it }
        }
        return $fallback
    }

    # Navigate from storage root through $segs; returns a Folder object
    function Nav-Path($device, $segs) {
        $df = $device.GetFolder()
        if ($df.Items().Count -eq 0) {
            Fail 'DEVICE_LOCKED' 'Device root is empty. Unlock the phone and set USB mode to File Transfer (MTP).'
        }
        $st = Get-Storage $df
        if ($null -eq $st) { Fail 'NO_STORAGE' 'No storage volume found on the device.' }
        $cur = $st.GetFolder()
        $walked = @($st.Name)
        foreach ($seg in $segs) {
            $next = $null
            foreach ($it in $cur.Items()) {
                if ($it.Name -eq $seg) { $next = $it; break }
            }
            if ($null -eq $next) {
                Fail 'PATH_NOT_FOUND' ("Path not found: " + ($walked -join '/') + "  missing: " + $seg)
            }
            if (-not $next.IsFolder) {
                Fail 'NOT_FOLDER' ("Not a folder: " + $seg)
            }
            $cur = $next.GetFolder()
            $walked += $seg
        }
        return $cur
    }

    # 递归收集所有 entry.json 所在文件夹（对象+相对路径），供 metadata 模式使用
    function Walk-EntryFolders($folder, $rel, $depth, $acc, $progFile) {
        if ($depth -gt 8) { return }
        $items = @($folder.Items())
        $hasEntry = $false
        foreach ($it in $items) {
            if (-not $it.IsFolder -and $it.Name -eq 'entry.json') { $hasEntry = $true; break }
        }
        if ($hasEntry) {
            $acc.Add([pscustomobject]@{ rel = $rel; folder = $folder }) | Out-Null
            try { [IO.File]::WriteAllText($progFile, $acc.Count.ToString(),
                (New-Object System.Text.UTF8Encoding($false))) } catch {}
            return
        }
        foreach ($it in $items) {
            if (-not $it.IsFolder) { continue }
            $name = [string]$it.Name
            if ($name -eq 'cache' -or $name -eq 'temp' -or $name -eq 'thumb') { continue }
            $sub = if ($rel) { $rel + '/' + $name } else { $name }
            try { Walk-EntryFolders $it.GetFolder() $sub ($depth + 1) $acc $progFile } catch {}
        }
    }

    function Item-Info($it) {
        $sz = $null
        try { if (-not $it.IsFolder) { $sz = [int64]$it.Size } } catch {}
        return [pscustomobject]@{ name = $it.Name; isFolder = [bool]$it.IsFolder; size = $sz }
    }

    function Wait-Destination($destDir, $leaf, $timeoutSec) {
        $deadline = (Get-Date).AddSeconds($timeoutSec)
        $last = -1; $stable = 0; $seen = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 1200
            $p = Join-Path $destDir $leaf
            if (Test-Path -LiteralPath $p) {
                $seen = $true
                $files = @(Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue)
                $busy = @($files | Where-Object { $_.Name -like '*.tmp' -or $_.Name -like '~*' -or $_.Name -like '*.partial' -or $_.Name -like '*.crdownload' })
                $cnt = $files.Count
                if ($busy.Count -gt 0) { $stable = 0; continue }
                if ($cnt -eq $last) {
                    $stable++
                    if ($stable -ge 2) { return $true }
                } else { $stable = 0; $last = $cnt }
            }
        }
        return $seen
    }

    function Wait-Destination-Mtp($destFolder, $leaf, $timeoutSec) {
        $deadline = (Get-Date).AddSeconds($timeoutSec)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 1500
            try {
                foreach ($it in $destFolder.Items()) {
                    if ($it.Name -eq $leaf) { return $true }
                }
            } catch {}
        }
        return $false
    }

    function Copy-Here-Wait($srcItem, $destDir, $leaf, $timeoutSec) {
        if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
        $dest = $shell.NameSpace($destDir)
        if ($null -eq $dest) { Fail 'BAD_DEST' ("Cannot open local destination: " + $destDir) }
        # 4=no progress UI, 16=yes to all, 512=no confirm, 1024=no error UI
        $dest.CopyHere($srcItem, 1556)
        $ok = Wait-Destination $destDir $leaf $timeoutSec
        if (-not $ok) { Fail 'COPY_TIMEOUT' ("Timed out copying: " + $leaf) }
        return (Join-Path $destDir $leaf)
    }

    $mode = $a.mode

    switch ($mode) {

        'devices' {
            $list = @()
            # MTP/portable devices have virtual shell paths like '::{GUID}';
            # fixed drives have paths like 'C:\' and must be filtered out.
            foreach ($it in $shell.NameSpace(17).Items()) {
                if ($it.IsFolder -and ($it.Path -like '::*')) { $list += $it.Name }
            }
            Write-Result ([pscustomobject]@{ ok = $true; devices = $list })
        }

        'list' {
            $device = Get-Device $a.device
            if ($null -eq $device) { Fail 'DEVICE_NOT_FOUND' ("Device not found: " + $a.device) }
            $folder = Nav-Path $device @($a.segs)
            $items = @()
            foreach ($it in $folder.Items()) { $items += Item-Info $it }
            Write-Result ([pscustomobject]@{ ok = $true; items = $items })
        }

        'treewalk' {
            $device = Get-Device $a.device
            if ($null -eq $device) { Fail 'DEVICE_NOT_FOUND' ("Device not found: " + $a.device) }
            $base = Nav-Path $device @($a.segs)
            $progFile = $OutFile + '.prog'
            Walk-Tree $base '' 0 $progFile
            Write-Result ([pscustomobject]@{ ok = $true; entries = @($script:entryList); count = $script:entryCount })
        }

        'metadata' {
            $device = Get-Device $a.device
            if ($null -eq $device) { Fail 'DEVICE_NOT_FOUND' ("Device not found: " + $a.device) }
            $base = Nav-Path $device @($a.segs)
            $progFile = $OutFile + '.prog'
            $streamFile = $OutFile + '.stream'
            if (Test-Path $streamFile) { Remove-Item $streamFile -Force -ErrorAction SilentlyContinue }
            $acc = New-Object System.Collections.ArrayList
            Walk-EntryFolders $base '' 0 $acc $progFile

            $tmpDir = Join-Path $env:TEMP ('mtp_meta_' + [guid]::NewGuid().ToString('N').Substring(0,8))
            New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
            $meta = @()
            try {
                foreach ($e in $acc) {
                    $f = $e.folder
                    $entryItem = $null; $vItem = $null; $aItem = $null
                    foreach ($it in $f.Items()) {
                        $nm = [string]$it.Name
                        if ($nm -eq 'entry.json') { $entryItem = $it }
                        if ($nm.StartsWith('video') -and $nm.EndsWith('.m4s')) { $vItem = $it }
                        if ($nm.StartsWith('audio') -and $nm.EndsWith('.m4s')) { $aItem = $it }
                    }
                    if ($null -eq $entryItem) { continue }
                    # 复制 entry.json 到临时目录读取（只有几KB）
                    $tmpNs = $shell.NameSpace($tmpDir)
                    $tmpNs.CopyHere($entryItem, 1556)
                    $tmpEntry = Join-Path $tmpDir 'entry.json'
                    $deadline = (Get-Date).AddSeconds(60)
                    while (-not (Test-Path $tmpEntry) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 400 }
                    if (-not (Test-Path $tmpEntry)) { continue }
                    $obj = $null
                    try { $obj = Get-Content $tmpEntry -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
                    Remove-Item $tmpEntry -Force -ErrorAction SilentlyContinue
                    if ($null -eq $obj) { continue }

                    $title = [string]$obj.title
                    if (-not $title -and $obj.video) { $title = [string]$obj.video.title }
                    if (-not $title) { $title = '未命名' }
                    $page = 1; $part = ''
                    if ($obj.page_data) {
                        if ($obj.page_data.page) { $page = [int]$obj.page_data.page }
                        $part = [string]$obj.page_data.part
                    } elseif ($obj.video -and $obj.video.page_data) {
                        if ($obj.video.page_data.page) { $page = [int]$obj.video.page_data.page }
                        $part = [string]$obj.video.page_data.part
                    }
                    $quality = ''
                    if ($obj.video_quality) { $quality = [string]$obj.video_quality }
                    elseif ($obj.video -and $obj.video.quality) { $quality = [string]$obj.video.quality }
                    $vsize = 0; $asize = 0
                    try { if ($vItem) { $vsize = [int64]$vItem.Size } } catch {}
                    try { if ($aItem) { $asize = [int64]$aItem.Size } } catch {}

                    $row = [pscustomobject]@{
                        rel = $e.rel; title = $title; page = $page; part = $part
                        quality = $quality; size = ($vsize + $asize)
                    }
                    $meta += $row
                    # 流式追加：Python 端读到一行就增量显示一个，不用等全部完成
                    try {
                        $jline = $row | ConvertTo-Json -Depth 8 -Compress
                        [IO.File]::AppendAllText($streamFile, $jline + "`n",
                            (New-Object System.Text.UTF8Encoding($false)))
                    } catch {}
                }
            } finally {
                Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            Write-Result ([pscustomobject]@{ ok = $true; items = $meta; count = $meta.Count })
        }

        'copy' {
            $device = Get-Device $a.device
            if ($null -eq $device) { Fail 'DEVICE_NOT_FOUND' ("Device not found: " + $a.device) }
            $segs = @($a.segs)
            if ($segs.Count -lt 1) { Fail 'BAD_PATH' 'Empty source path' }
            $parent = Nav-Path $device @($segs[0..($segs.Count-2)])
            $leaf = $segs[$segs.Count-1]
            $src = $null
            foreach ($it in $parent.Items()) {
                if ($it.Name -eq $leaf) { $src = $it; break }
            }
            if ($null -eq $src) { Fail 'PATH_NOT_FOUND' ("Not found: " + $leaf) }
            $timeout = 1800
            if ($a.timeout) { $timeout = [int]$a.timeout }
            $final = Copy-Here-Wait $src ([string]$a.dest) $leaf $timeout
            Write-Result ([pscustomobject]@{ ok = $true; path = $final })
        }

        'upload' {
            # 把本地文件上传到手机指定目录（segs 为手机上的目标目录路径，从存储根开始）
            $device = Get-Device $a.device
            if ($null -eq $device) { Fail 'DEVICE_NOT_FOUND' ("Device not found: " + $a.device) }
            $localFile = [string]$a.local_file
            if (-not (Test-Path -LiteralPath $localFile)) { Fail 'BAD_LOCAL' ("Local file not found: " + $localFile) }
            $segs = @($a.segs)
            $destFolder = Nav-Path $device $segs
            $leaf = [System.IO.Path]::GetFileName($localFile)
            $localDir = [System.IO.Path]::GetDirectoryName($localFile)
            $srcNs = $shell.NameSpace($localDir)
            $srcItem = $srcNs.ParseName($leaf)
            if ($null -eq $srcItem) { Fail 'BAD_LOCAL' ("Cannot open local file: " + $leaf) }
            $timeout = 1800
            if ($a.timeout) { $timeout = [int]$a.timeout }
            # 4=no progress UI, 16=yes to all, 512=no confirm, 1024=no error UI
            $destFolder.CopyHere($srcItem, 1556)
            $ok = Wait-Destination-Mtp $destFolder $leaf $timeout
            if (-not $ok) { Fail 'UPLOAD_TIMEOUT' ("Timed out uploading: " + $leaf) }
            Write-Result ([pscustomobject]@{ ok = $true; name = $leaf })
        }

        default { Fail 'UNKNOWN_MODE' ("Unknown mode: " + $mode) }
    }
}
catch {
    Fail 'EXCEPTION' ($_.Exception.Message)
}
