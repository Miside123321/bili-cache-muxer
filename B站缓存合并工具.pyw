# -*- coding: utf-8 -*-
"""
B站缓存合并工具  v2.5
-------------------------------------------------
版权所有 (c) 作者 wan  B站UID: 500886854  https://space.bilibili.com/500886854
保留全部原创权利，未经许可不得用于商业用途或二次分发。

把 B站安卓客户端缓存的分离式 .m4s（纯视频 + 纯音频）自动合并为完整 MP4。
功能：
  1. 本地文件夹自动扫描（递归识别 entry.json，自动挑最高清晰度）
  2. 手机 MTP 直读：整批复制到电脑 → 本地快速识别 → 合并（关闭软件时自动清理临时副本）
  3. 手动选择 video.m4s / audio.m4s
  4. 手机读取/复制过程可随时「停止」，防止卡死
合并使用 FFmpeg 流复制（-c copy），不重新编码，速度快、音画天然同步。
支持 PyInstaller 打包为单文件 exe（ffmpeg 与手机助手内嵌）。
"""

import os
import sys
import json
import re
import shutil
import subprocess
import threading
import queue
import tempfile
import time
import glob
import uuid
import hashlib
import zipfile
import traceback
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------- 路径与常量

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# PyInstaller 单文件模式：资源从临时释放目录读取；工作目录/配置放到用户目录
if getattr(sys, 'frozen', False):
    RES_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    USER_DIR = os.path.join(os.path.expanduser('~'), '.bili_muxer')
else:
    RES_DIR = APP_DIR
    USER_DIR = APP_DIR
BIN_DIR = os.path.join(RES_DIR, 'bin')
WORK_DIR = os.path.join(USER_DIR, '_work')
CONFIG_PATH = os.path.join(USER_DIR, 'config.json')
HELPER_PS1 = os.path.join(RES_DIR, 'mtp_helper.ps1')
DEFAULT_OUT_NAME = 'B站合并输出'
PHONE_DEFAULT_SEGS = ['Android', 'data', 'tv.danmaku.bili', 'download']

QUALITY_NAMES = {
    '127': '8K', '126': '杜比视界', '125': 'HDR', '121': '4K',
    '120': '1080P60', '116': '1080P高码率', '112': '1080P+',
    '80': '1080P', '74': '720P60', '64': '720P',
    '32': '480P', '16': '360P',
}

FFMPEG_FALLBACKS = [
    r'D:\oopz\ffmpeg.exe',
    r'E:\JianyingPro\11.4.2.14459\ffmpeg.exe',
]
FFMPEG_DOWNLOAD_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def app_print(*args):
    try:
        print(*args)
    except Exception:
        pass


# ---------------------------------------------------------------- 通用工具

def sanitize_filename(name, default='video'):
    if not name:
        return default
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip(' ._')
    if not name:
        return default
    return name[:150]


def natkey(s):
    base = os.path.basename(str(s))
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', base)]


def human_size(n):
    if not n:
        return '—'
    try:
        n = float(n)
    except Exception:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.1f%s' % (n, unit)) if unit != 'B' else ('%dB' % n)
        n /= 1024.0


def time_to_seconds(ts):
    try:
        parts = ts.split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * float(parts[2])
    except Exception:
        return 0.0


def unique_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists('%s(%d)%s' % (root, i, ext)):
        i += 1
    return '%s(%d)%s' % (root, i, ext)


# ---------------------------------------------------------------- 配置

def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 外观 / 深色模式

THEME_LIGHT = {
    'bg': '#F5F5F7', 'panel': '#FFFFFF', 'field': '#FFFFFF',
    'fg': '#1D1D1F', 'muted': '#6E6E73', 'border': '#D2D2D7',
    'line': '#E3E3E8',
    'head_bg': '#ECECF0', 'sel_bg': '#CFE3F7', 'sel_fg': '#1D1D1F',
    'accent': '#0A8F4E', 'warn': '#A05A00',
    'done': '#1A7F1A', 'fail': '#C0392B', 'work': '#1A5FB4',
}
THEME_DARK = {
    'bg': '#26262A', 'panel': '#2E2E33', 'field': '#3A3A40',
    'fg': '#E8E8ED', 'muted': '#9A9AA1', 'border': '#45454B',
    'line': '#35353B',
    'head_bg': '#33333A', 'sel_bg': '#3E5D82', 'sel_fg': '#FFFFFF',
    'accent': '#3DDC84', 'warn': '#E8A85C',
    'done': '#6FD36F', 'fail': '#FF7B72', 'work': '#7FB4FF',
}

# 当前生效的外观（供 Toplevel 等模块级读取）
CURRENT_THEME = {'dark': False, 'p': THEME_LIGHT}


def system_prefers_dark():
    """读取 Windows 系统“应用模式”是否为深色。失败则视为浅色。"""
    if os.name != 'nt':
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        try:
            val, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        finally:
            winreg.CloseKey(key)
        return int(val) == 0
    except Exception:
        return False


def resolve_dark(pref):
    if pref == 'dark':
        return True
    if pref == 'light':
        return False
    return system_prefers_dark()


def apply_global_fonts(root, base=11):
    """统一放大全局字体（Tk 命名字体），标题字重保留。"""
    try:
        import tkinter.font as tkfont
        fam = 'Microsoft YaHei UI'
        plan = {
            'TkDefaultFont': (fam, base),
            'TkTextFont': (fam, base),
            'TkMenuFont': (fam, base),
            'TkHeadingFont': (fam, base, 'bold'),
            'TkCaptionFont': (fam, base, 'bold'),
            'TkSmallCaptionFont': (fam, base - 2),
            'TkIconFont': (fam, base),
            'TkTooltipFont': (fam, base - 2),
        }
        for name, spec in plan.items():
            try:
                tkfont.nametofont(name).configure(family=spec[0], size=spec[1],
                                                  weight=(spec[2] if len(spec) > 2 else 'normal'))
            except Exception:
                pass
        try:
            tkfont.nametofont('TkFixedFont').configure(family='Consolas', size=base - 1)
        except Exception:
            pass
    except Exception:
        pass


def set_dark_titlebar(window, dark):
    """让标题栏跟随深色（Win10 1809+ 用属性20，更早用19）；
    并把 Win11 的窗口外框改成淡色，避免深色界面外圈出现一条亮白边。"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
            if res == 0:
                break
        # DWMWA_BORDER_COLOR(34)：Win11 窗口外框，COLORREF 为 0x00BBGGRR
        p = THEME_DARK if dark else THEME_LIGHT
        hx = p['line'].lstrip('#')
        r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
        cref = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(cref), ctypes.sizeof(cref))
        window.update_idletasks()
    except Exception:
        pass


# ---------------------------------------------------------------- FFmpeg 定位

def find_ffmpeg(cfg):
    p = cfg.get('ffmpeg')
    if p and os.path.isfile(p):
        return p
    p = os.path.join(BIN_DIR, 'ffmpeg.exe')
    if os.path.isfile(p):
        return p
    p = shutil.which('ffmpeg')
    if p:
        return p
    for cand in FFMPEG_FALLBACKS:
        if os.path.isfile(cand):
            return cand
    for pat in (r'E:\JianyingPro\*\ffmpeg.exe',
                r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
                r'C:\ffmpeg\bin\ffmpeg.exe',
                os.path.expanduser(r'~\ffmpeg\bin\ffmpeg.exe')):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def download_ffmpeg(progress_cb):
    os.makedirs(BIN_DIR, exist_ok=True)
    dl_dir = os.path.join(APP_DIR, '_dl')
    os.makedirs(dl_dir, exist_ok=True)
    zip_path = os.path.join(dl_dir, 'ffmpeg.zip')

    def hook(blocks, bs, total):
        done = blocks * bs
        if total > 0:
            progress_cb(min(done / total, 0.99), '正在下载 FFmpeg（约 80MB）…  %s / %s'
                        % (human_size(done), human_size(total)))
        else:
            progress_cb(-1, '正在下载 FFmpeg…  %s' % human_size(done))

    urllib.request.urlretrieve(FFMPEG_DOWNLOAD_URL, zip_path, reporthook=hook)
    progress_cb(0.995, '正在解压…')
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        exe_member = next((n for n in names if n.replace('\\', '/').endswith('/bin/ffmpeg.exe')), None)
        if not exe_member:
            raise RuntimeError('压缩包中未找到 ffmpeg.exe')
        with z.open(exe_member) as src, open(os.path.join(BIN_DIR, 'ffmpeg.exe'), 'wb') as dst:
            shutil.copyfileobj(src, dst)
    try:
        shutil.rmtree(dl_dir, ignore_errors=True)
    except Exception:
        pass
    return os.path.join(BIN_DIR, 'ffmpeg.exe')


# ---------------------------------------------------------------- m4s 解析与合并

def m4s_skip_bytes(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(64)
    except Exception:
        return 0
    if len(head) >= 8 and head[4:8] == b'ftyp':
        return 0
    idx = head.find(b'ftyp')
    if 4 <= idx <= 40:
        return idx - 4
    return 0


def concat_m4s(files, out_path, log):
    with open(out_path, 'wb') as out:
        for fp in files:
            skip = m4s_skip_bytes(fp)
            if skip:
                log('  修正文件头（跳过 %d 字节）: %s' % (skip, os.path.basename(fp)))
            with open(fp, 'rb') as f:
                if skip:
                    f.seek(skip)
                shutil.copyfileobj(f, out, length=1024 * 1024)


def _probe_kind(exe, path, cache):
    if path in cache:
        return cache[path]
    kind = '?'
    try:
        p = subprocess.run([exe, '-hide_banner', '-i', path],
                           stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        text = p.stderr.decode('utf-8', 'replace')
        has_v = 'Video:' in text
        has_a = 'Audio:' in text
        if has_v and not has_a:
            kind = 'v'
        elif has_a and not has_v:
            kind = 'a'
        elif has_v:
            kind = 'v'
    except Exception:
        pass
    cache[path] = kind
    return kind


def resolve_media(container, exe, log):
    """在一个清晰度目录（或 cid 目录）里找到有序的视频/音频分片。"""
    v_direct = os.path.join(container, 'video.m4s')
    a_direct = os.path.join(container, 'audio.m4s')
    if os.path.isfile(v_direct) and os.path.isfile(a_direct):
        return [v_direct], [a_direct]

    v_dir = os.path.join(container, 'video')
    a_dir = os.path.join(container, 'audio')
    if os.path.isdir(v_dir) and os.path.isdir(a_dir):
        vs = sorted(glob.glob(os.path.join(v_dir, '*.m4s')), key=natkey)
        aus = sorted(glob.glob(os.path.join(a_dir, '*.m4s')), key=natkey)
        if vs and aus:
            return vs, aus

    idx = os.path.join(container, 'index.json')
    if os.path.isfile(idx):
        try:
            with open(idx, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)

            def walk_strings(o):
                if isinstance(o, str):
                    yield o
                elif isinstance(o, dict):
                    for k, val in o.items():
                        yield k
                        for s in walk_strings(val):
                            yield s
                elif isinstance(o, list):
                    for it in o:
                        for s in walk_strings(it):
                            yield s

            names = [s for s in walk_strings(data) if s.lower().endswith('.m4s')]
            all_files = glob.glob(os.path.join(container, '**', '*.m4s'), recursive=True)
            norm = {fp: fp.replace('\\', '/') for fp in all_files}
            vs, aus = [], []
            used = set()

            def classify(fp, hint=''):
                low = (norm[fp] + ' ' + hint).lower()
                if '/audio/' in low or 'audio' in os.path.basename(low):
                    aus.append(fp)
                elif '/video/' in low or 'video' in os.path.basename(low):
                    vs.append(fp)

            for ref in names:
                clean = ref.split('?')[0].replace('\\', '/')
                base = os.path.basename(clean)
                hint = 'audio' if 'audio' in clean.lower() else ('video' if 'video' in clean.lower() else '')
                cands = []
                if '/' in clean:
                    cands = [fp for fp in all_files if norm[fp].endswith(clean)]
                if not cands:
                    cands = [fp for fp in all_files if os.path.basename(fp) == base]
                for fp in cands:
                    if fp not in used:
                        used.add(fp)
                        classify(fp, hint)
            vs = sorted(set(vs), key=natkey)
            aus = sorted(set(aus), key=natkey)
            if vs and aus:
                return vs, aus
        except Exception:
            pass

    all_files = sorted(glob.glob(os.path.join(container, '**', '*.m4s'), recursive=True),
                       key=natkey)
    if all_files:
        vs, aus = [], []
        probe_cache = {}
        for fp in all_files:
            low = fp.replace('\\', '/').lower()
            if '/video/' in low or os.path.basename(low).startswith('video'):
                vs.append(fp)
            elif '/audio/' in low or os.path.basename(low).startswith('audio'):
                aus.append(fp)
            else:
                kind = _probe_kind(exe, fp, probe_cache) if exe else '?'
                if kind == 'v':
                    vs.append(fp)
                elif kind == 'a':
                    aus.append(fp)
        if vs:
            return vs, aus

    if os.path.isfile(v_direct):
        return [v_direct], []
    raise RuntimeError('未找到视频/音频 m4s 文件：%s' % container)


def best_quality_dir(cid_dir):
    candidates = []
    try:
        for name in os.listdir(cid_dir):
            full = os.path.join(cid_dir, name)
            if os.path.isdir(full) and re.fullmatch(r'\d{2,4}', name):
                has_media = (
                    os.path.isfile(os.path.join(full, 'video.m4s'))
                    or os.path.isdir(os.path.join(full, 'video'))
                    or glob.glob(os.path.join(full, '**', '*.m4s'), recursive=True)
                )
                if has_media:
                    candidates.append((int(name), full))
    except Exception:
        pass
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], str(candidates[0][0])
    return cid_dir, ''


def parse_entry_json(cid_dir, entry_path):
    with open(entry_path, 'r', encoding='utf-8-sig', errors='replace') as f:
        meta = json.load(f)
    mi = meta.get('mediaInfo') or {}
    pd = mi.get('page_data') or {}
    title = mi.get('title') or meta.get('title') or os.path.basename(cid_dir)
    part = pd.get('part') or ''
    try:
        page = int(pd.get('page') or 1)
    except Exception:
        page = 1
    tag = str(meta.get('type_tag') or meta.get('quality') or '')
    return meta, title, str(part), page, tag


def quality_label(tag, qdir):
    if not tag:
        tag = os.path.basename(qdir)
    return '%s（%s）' % (QUALITY_NAMES.get(tag, '未知清晰度'), tag) if tag else '未知清晰度'


def build_local_task(cid_dir, entry_path, exe, log, kind='local'):
    meta, title, part, page, tag = parse_entry_json(cid_dir, entry_path)
    qdir, qtag = best_quality_dir(cid_dir)
    use_tag = tag or qtag
    v_files, a_files = resolve_media(qdir, exe, log)
    size = sum(os.path.getsize(x) for x in v_files + a_files if os.path.isfile(x))
    return {
        'id': uuid.uuid4().hex,
        'kind': kind,
        'title': title,
        'part': part,
        'page': page,
        'quality': quality_label(use_tag, qdir),
        'cid_dir': os.path.abspath(cid_dir),
        'segs': None,
        'device': None,
        'v_files': v_files,
        'a_files': a_files,
        'size': size,
        'status': '待合并',
    }


def scan_local_folder(root, exe, log):
    tasks = []
    seen = set()
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        if depth > 3:
            dirnames[:] = []
            continue
        if 'entry.json' in filenames:
            try:
                t = build_local_task(dirpath, os.path.join(dirpath, 'entry.json'), exe, log)
                key = t['cid_dir']
                if key not in seen:
                    seen.add(key)
                    tasks.append(t)
            except Exception as e:
                log('跳过一个无效缓存: %s（%s）' % (dirpath, e))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not re.fullmatch(r'\d{2,4}', d) or depth < 2]

    if not tasks:
        probe = root
        if os.path.isfile(os.path.join(probe, 'entry.json')):
            tasks.append(build_local_task(probe, os.path.join(probe, 'entry.json'), exe, log))
        else:
            try:
                v, a = resolve_media(root, exe, log)
                if v:
                    tasks.append({
                        'id': uuid.uuid4().hex, 'kind': 'local',
                        'title': os.path.basename(root.rstrip('\\/')),
                        'part': '', 'page': 1,
                        'quality': quality_label('', root),
                        'cid_dir': os.path.abspath(root), 'segs': None, 'device': None,
                        'v_files': v, 'a_files': a,
                        'size': sum(os.path.getsize(x) for x in v + a if os.path.isfile(x)),
                        'status': '待合并',
                    })
            except Exception:
                pass
    return tasks


def suggest_output_name(task):
    title = sanitize_filename(task.get('title') or 'bilibili_video')
    part = sanitize_filename(task.get('part') or '')
    page = task.get('page') or 1
    if task.get('kind') == 'manual':
        return title
    if page and page > 1:
        if part and part != title:
            return ('%s - P%d %s' % (title, page, part))[:150]
        return ('%s - P%d' % (title, page))[:150]
    return title


# ---------------------------------------------------------------- FFmpeg 合并

def run_ffmpeg_merge(exe, v_path, a_path, out_path, log, progress_cb):
    has_audio = bool(a_path)
    if has_audio:
        base_cmd = [exe, '-y', '-hide_banner', '-nostats',
                    '-i', v_path, '-i', a_path]
        map_cmd = ['-map', '0:v:0', '-map', '1:a:0']
    else:
        base_cmd = [exe, '-y', '-hide_banner', '-nostats', '-i', v_path]
        map_cmd = []
    cmd = base_cmd + map_cmd + ['-c', 'copy', '-movflags', '+faststart', out_path]

    def execute(c):
        p = subprocess.Popen(c, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW,
                             bufsize=1, universal_newlines=True, encoding='utf-8',
                             errors='replace')
        duration = None
        for line in p.stderr:
            if duration is None and 'Duration:' in line:
                m = re.search(r'Duration:\s*(\d+:\d+:\d+\.\d+)', line)
                if m:
                    duration = time_to_seconds(m.group(1))
            m = re.search(r'time=(\d+:\d+:\d+\.\d+)', line)
            if m and duration:
                cur = time_to_seconds(m.group(1))
                progress_cb(min(cur / duration, 1.0))
        p.wait()
        return p.returncode

    log('  FFmpeg 封装中（流复制，不重编码）…')
    rc = execute(cmd)
    if rc != 0 and has_audio:
        log('  默认音视频映射失败，尝试自动选择流重试…')
        cmd = base_cmd + ['-c', 'copy', '-movflags', '+faststart', out_path]
        rc = execute(cmd)
    if rc != 0:
        raise RuntimeError('FFmpeg 合并失败（返回码 %s）' % rc)
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError('输出文件为空或未生成')

    try:
        p = subprocess.run([exe, '-hide_banner', '-i', out_path],
                           stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
        info = p.stderr.decode('utf-8', 'replace')
        if 'Video:' not in info:
            raise RuntimeError('输出文件中没有视频流')
        if has_audio and 'Audio:' not in info:
            raise RuntimeError('输出文件中没有音频流')
    except RuntimeError:
        raise
    except Exception:
        pass


def process_task(task, out_dir, exe, mtp_client, log, progress_cb):
    """完整处理一个任务（手机任务已在扫描阶段整批落到本地镜像），返回最终文件路径。"""
    os.makedirs(WORK_DIR, exist_ok=True)
    token = task['id']
    work = os.path.join(WORK_DIR, token)
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    v_files = task.get('v_files') or []
    a_files = task.get('a_files') or []

    # 手机任务：按需只复制当前这一个视频目录（扫描阶段只读了元数据，未复制视频）
    if not v_files and task.get('kind') == 'mtp' and task.get('segs') and mtp_client:
        progress_cb(-1, '正在从手机复制该视频（仅复制选中项，MTP 稍慢）…')
        log('  按需复制: %s' % '/'.join(task['segs']))
        local = mtp_client.copy_dir(task['device'], task['segs'], work, timeout=1800)
        cid_dir = local if os.path.isdir(local) else work
        qdir, qtag = best_quality_dir(cid_dir)
        v_files, a_files = resolve_media(qdir, exe, log)
        task['cid_dir'] = cid_dir
        if not task.get('quality') or task['quality'].startswith('未知'):
            task['quality'] = quality_label(qtag, qdir)

    if not v_files:
        cid_dir = task.get('cid_dir')
        if cid_dir:
            qdir, qtag = best_quality_dir(cid_dir)
            v_files, a_files = resolve_media(qdir, exe, log)
    if not v_files:
        raise RuntimeError('缺少视频文件')

    log('  视频分片 %d 个，音频分片 %d 个' % (len(v_files), len(a_files)))
    progress_cb(0.02, '正在拼接分片…')
    v_out = os.path.join(work, 'video.mp4')
    a_out = os.path.join(work, 'audio.mp4')
    concat_m4s(v_files, v_out, log)
    if a_files:
        concat_m4s(a_files, a_out, log)

    name = suggest_output_name(task)
    out_path = unique_path(os.path.join(out_dir, name + '.mp4'))
    run_ffmpeg_merge(exe, v_out, a_out if a_files else None, out_path, log, progress_cb)

    shutil.rmtree(work, ignore_errors=True)
    return out_path


# ---------------------------------------------------------------- MTP 客户端

class MtpError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__('[%s] %s' % (code, message))


class MtpClient:
    """手机 MTP 访问。所有耗时操作都走可取消的后台 PowerShell 进程。"""

    def __init__(self, log):
        self.log = log
        self._procs = []
        self._lock = threading.Lock()
        self.cancel_requested = False

    def request_cancel(self):
        self.cancel_requested = True
        with self._lock:
            for p in self._procs:
                try:
                    p.terminate()
                except Exception:
                    pass
                try:
                    p.kill()
                except Exception:
                    pass

    def reset_cancel(self):
        self.cancel_requested = False

    def _register(self, p):
        with self._lock:
            self._procs.append(p)

    def _unregister(self, p):
        with self._lock:
            try:
                self._procs.remove(p)
            except ValueError:
                pass

    def _spawn(self, payload, timeout=1800, stream_cb=None):
        if self.cancel_requested:
            raise MtpError('CANCELLED', '操作已取消')
        args_fd, args_path = tempfile.mkstemp(prefix='mtp_args_', suffix='.json')
        out_fd, out_path = tempfile.mkstemp(prefix='mtp_out_', suffix='.json')
        os.close(args_fd)
        os.close(out_fd)
        prog_path = out_path + '.prog'
        try:
            with open(args_path, 'w', encoding='utf-8-sig') as f:
                json.dump(payload, f, ensure_ascii=False)
            cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-File', HELPER_PS1, '-ArgsFile', args_path, '-OutFile', out_path]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=CREATE_NO_WINDOW)
            self._register(p)
            try:
                deadline = time.time() + timeout
                stream_path = out_path + '.stream'
                stream_pos = 0
                while time.time() < deadline:
                    if self.cancel_requested:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        try:
                            p.kill()
                        except Exception:
                            pass
                        raise MtpError('CANCELLED', '操作已取消')
                    if os.path.isfile(out_path):
                        try:
                            with open(out_path, 'r', encoding='utf-8-sig') as f:
                                res = json.load(f)
                            if not res.get('ok'):
                                raise MtpError(res.get('code', '?'), res.get('message', '未知错误'))
                            return True, res
                        except (ValueError, json.JSONDecodeError):
                            pass
                    # 流式结果：每读到一个完整行就回调，实现扫描出一个显示一个
                    if stream_cb is not None and os.path.isfile(stream_path):
                        try:
                            with open(stream_path, 'rb') as sf:
                                sf.seek(stream_pos)
                                chunk = sf.read()
                            last_nl = chunk.rfind(b'\n')
                            if last_nl >= 0:
                                stream_pos += last_nl + 1
                                for raw_line in chunk[:last_nl + 1].split(b'\n'):
                                    raw_line = raw_line.strip()
                                    if not raw_line:
                                        continue
                                    try:
                                        stream_cb(json.loads(raw_line.decode('utf-8')))
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    time.sleep(0.3)
                try:
                    p.kill()
                except Exception:
                    pass
                raise MtpError('TIMEOUT', '手机响应超时（请确认手机已解锁、处于文件传输模式）')
            finally:
                self._unregister(p)
                try:
                    p.wait(timeout=3)
                except Exception:
                    pass
        finally:
            for pth in (args_path, out_path, prog_path, out_path + '.stream'):
                try:
                    if os.path.exists(pth):
                        os.remove(pth)
                except Exception:
                    pass

    def devices(self):
        ok, res = self._spawn({'mode': 'devices'}, timeout=60)
        return res.get('devices', [])

    def list_dir(self, device, segs):
        ok, res = self._spawn({'mode': 'list', 'device': device, 'segs': list(segs)}, timeout=120)
        return res.get('items', [])

    def treewalk(self, device, segs):
        ok, res = self._spawn({'mode': 'treewalk', 'device': device, 'segs': list(segs)}, timeout=1800)
        entries = res.get('entries', [])
        return [e.replace('\\', '/') for e in entries], len(entries)

    def metadata(self, device, segs, timeout=1800, stream_cb=None):
        """只读 entry.json 元数据（标题/分P/大小），不复制视频文件本体。
        stream_cb：每解析出一个视频就回调一次，用于增量显示。"""
        ok, res = self._spawn({'mode': 'metadata', 'device': device,
                               'segs': list(segs), 'timeout': timeout},
                              timeout=timeout + 120, stream_cb=stream_cb)
        return res.get('items', [])

    def copy_file(self, device, segs, dest_dir, timeout=1800):
        """复制手机上单个文件到本地目录。"""
        ok, res = self._spawn({'mode': 'copy', 'device': device, 'segs': list(segs),
                               'dest': dest_dir, 'timeout': timeout}, timeout=timeout + 120)
        return res.get('path')

    def copy_dir(self, device, segs, dest_dir, timeout=3600):
        ok, res = self._spawn({'mode': 'copy', 'device': device, 'segs': list(segs),
                               'dest': dest_dir, 'timeout': timeout}, timeout=timeout + 120)
        return res.get('path')

    def upload_file(self, device, segs, local_file, timeout=1800):
        """把本地文件上传到手机指定目录（segs 为手机上的目标目录路径）。"""
        ok, res = self._spawn({'mode': 'upload', 'device': device, 'segs': list(segs),
                               'local_file': local_file, 'timeout': timeout}, timeout=timeout + 120)
        return res.get('name')


# ---------------------------------------------------------------- 手机文件夹浏览窗

class PhoneBrowser(tk.Toplevel):
    def __init__(self, master, mtp, device, start_segs=None):
        super().__init__(master)
        self.title('浏览手机文件夹')
        self.geometry('560x520')
        self.transient(master)
        self.grab_set()
        self.configure(bg=CURRENT_THEME['p']['bg'])
        self.mtp = mtp
        self.device = device
        self.segs = list(start_segs or [])
        self.selected = None
        self._loading = False

        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        ttk.Button(top, text='← 返回上级', command=self.go_up).pack(side='left')
        ttk.Button(top, text='刷新', command=self.reload).pack(side='left', padx=4)
        self.path_var = tk.StringVar()
        ttk.Label(top, textvariable=self.path_var, style='Muted.TLabel').pack(side='left', padx=8)

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(mid, columns=('name',), show='headings', selectmode='browse')
        self.tree.heading('name', text='文件夹（双击进入）')
        self.tree.column('name', width=520, anchor='w')
        vs = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', self.on_double)
        self.tree.bind('<Return>', self.on_double)

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, style='Muted.TLabel').pack(fill='x', padx=10)

        bot = ttk.Frame(self, padding=8)
        bot.pack(fill='x')
        ttk.Button(bot, text='选择当前文件夹', command=self.choose).pack(side='right')
        ttk.Button(bot, text='取消', command=self.destroy).pack(side='right', padx=6)
        self.update_idletasks()
        set_dark_titlebar(self, CURRENT_THEME['dark'])

        self.reload()

    def go_up(self):
        if self._loading:
            return
        if self.segs:
            self.segs.pop()
            self.reload()

    def on_double(self, _evt=None):
        if self._loading:
            return
        iid = self.tree.focus()
        if not iid:
            return
        name = self.tree.item(iid, 'values')[0]
        self.segs.append(name)
        self.reload()

    def choose(self):
        self.selected = list(self.segs)
        self.destroy()

    def reload(self):
        if self._loading:
            return
        self._loading = True
        self.tree.delete(*self.tree.get_children())
        self.path_var.set('内部存储设备/' + '/'.join(self.segs))
        self.status_var.set('加载中…')

        def work():
            try:
                items = self.mtp.list_dir(self.device, self.segs)
                folders = [i['name'] for i in items if i.get('isFolder')]
                self.after(0, lambda: self.show(folders, None))
            except MtpError as e:
                self.after(0, lambda: self.show([], str(e)))

        threading.Thread(target=work, daemon=True).start()

    def show(self, folders, err):
        self._loading = False
        for name in sorted(folders, key=lambda s: s.lower()):
            self.tree.insert('', 'end', values=(name,))
        if err:
            self.status_var.set('读取失败：%s' % err)
        elif not folders:
            self.status_var.set('（空文件夹。若这是 Android/data，说明手机系统禁止电脑直接访问，请见使用说明里的替代方法）')
        else:
            self.status_var.set('共 %d 个文件夹' % len(folders))


# ---------------------------------------------------------------- 主界面

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('B站缓存合并工具  v2.5')
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1120, sw - 60), min(920, sh - 130)
        self.geometry('%dx%d' % (w, max(620, h)))
        apply_global_fonts(self, base=11)
        self.minsize(960, 600)
        self.cfg = load_config()
        self.q = queue.Queue()
        self.tasks = {}
        self.running = False
        self.scan_cancel = False
        self.mirror_dir = None
        self._last_phone_key = None
        self.stop_btn = None
        self.ffmpeg = find_ffmpeg(self.cfg)
        self.mtp = MtpClient(self.thread_log)
        self.theme_pref = self.cfg.get('theme', 'auto')
        self._dark = resolve_dark(self.theme_pref)
        self._last_sys_dark = system_prefers_dark()
        CURRENT_THEME['dark'] = self._dark
        CURRENT_THEME['p'] = THEME_DARK if self._dark else THEME_LIGHT

        self._build_ui()
        self.apply_theme(self._dark)
        self._poll_queue()
        self.after(1000, self._watch_system_theme)
        if not self.ffmpeg:
            self.after(300, self.offer_ffmpeg_download)
            self.after(600, self.auto_detect_device)
        else:
            self.log('已找到 FFmpeg：%s' % self.ffmpeg)

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use('vista')
        except Exception:
            pass

        # ===== 自绘菜单栏 =====
        # Windows 原生菜单栏由系统绘制，DWM 深色只作用于标题栏，无法跟随深色，
        # 因此改用 Tk 自绘的 Menubutton 条，颜色可完全跟随主题。
        self.topbar = tk.Frame(self, bd=0, highlightthickness=0)
        self.topbar.pack(fill='x', side='top')
        self.mb_sep = tk.Frame(self.topbar, height=1, bd=0, highlightthickness=0)
        self.mb_sep.pack(side='bottom', fill='x')

        self.mb_set = tk.Menubutton(
            self.topbar, text='设置 ▾', bd=0, relief='flat', indicatoron=False,
            cursor='hand2', padx=12, pady=6,
            font=('Microsoft YaHei UI', 11), highlightthickness=0)
        m_set = tk.Menu(self.mb_set, tearoff=0)
        self.theme_var = tk.StringVar(value=self.theme_pref)
        m_app = tk.Menu(m_set, tearoff=0)
        for val, lab in (('auto', '跟随系统'), ('light', '浅色模式'), ('dark', '深色模式')):
            m_app.add_radiobutton(label=lab, value=val, variable=self.theme_var,
                                  command=lambda v=val: self.set_theme_pref(v))
        m_set.add_cascade(label='外观', menu=m_app)
        m_set.add_separator()
        m_set.add_command(label='手动指定 ffmpeg.exe…', command=self.specify_ffmpeg)
        m_set.add_command(label='自动下载 FFmpeg…',
                          command=lambda: self.offer_ffmpeg_download(force=True))
        self.mb_set['menu'] = m_set
        self.mb_set.pack(side='left', padx=(10, 2), pady=(6, 3))

        self.mb_help = tk.Menubutton(
            self.topbar, text='帮助 ▾', bd=0, relief='flat', indicatoron=False,
            cursor='hand2', padx=12, pady=6,
            font=('Microsoft YaHei UI', 11), highlightthickness=0)
        m_help = tk.Menu(self.mb_help, tearoff=0)
        m_help.add_command(label='使用说明', command=self.show_help)
        m_help.add_command(label='关于', command=self.show_about)
        self.mb_help['menu'] = m_help
        self.mb_help.pack(side='left', padx=2, pady=(6, 3))

        self._menus = [m_set, m_help, m_app]
        self._mb_list = [self.mb_set, self.mb_help]

        # 底部区域（进度 / 按钮 / 日志）先占位：窗口高度不够时优先压缩列表，而不是把底部挤没
        self.bottom_area = ttk.Frame(self)
        self.bottom_area.pack(side='bottom', fill='x')

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='x', padx=10, pady=(10, 6))

        # ===== Tab1 本地扫描 =====
        t1 = ttk.Frame(self.nb, padding=10)
        self.nb.add(t1, text='① 本地文件夹自动扫描')
        ttk.Label(t1, text='把手机 download 文件夹先复制到电脑，或直接选择电脑上的缓存目录（会自动递归识别所有视频）：')\
            .grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 6))
        self.local_var = tk.StringVar(value=self.cfg.get('last_local_dir', ''))
        ttk.Entry(t1, textvariable=self.local_var).grid(row=1, column=0, sticky='ew')
        ttk.Button(t1, text='浏览本地…', command=self.pick_local)\
            .grid(row=1, column=1, padx=4)
        ttk.Button(t1, text='浏览手机…', command=self.pick_phone_for_local)\
            .grid(row=1, column=2, padx=4)
        self.scan_local_btn = ttk.Button(t1, text='开始扫描', command=self.do_scan_local)
        self.scan_local_btn.grid(row=1, column=3, padx=4)
        t1.columnconfigure(0, weight=1)

        # ===== Tab2 手机直读 =====
        t2 = ttk.Frame(self.nb, padding=10)
        self.nb.add(t2, text='② 手机直读')
        row1 = ttk.Frame(t2)
        row1.pack(fill='x', pady=(0, 6))
        ttk.Label(row1, text='手机设备名：').pack(side='left')
        self.device_var = tk.StringVar(value=self.cfg.get('device', ''))
        self.device_combo = ttk.Combobox(row1, textvariable=self.device_var, width=24)
        self.device_combo.pack(side='left')
        ttk.Button(row1, text='刷新设备', command=self.refresh_devices).pack(side='left', padx=6)

        self.phone_path_var = tk.StringVar(value='内部存储设备/' + '/'.join(PHONE_DEFAULT_SEGS))
        ttk.Label(t2, textvariable=self.phone_path_var, style='Muted.TLabel').pack(anchor='w')
        btns = ttk.Frame(t2)
        btns.pack(fill='x', pady=6)
        self.scan_phone_btn = ttk.Button(btns, text='一键读取手机 B 站缓存', command=self.do_scan_phone_default)
        self.scan_phone_btn.pack(side='left')
        ttk.Button(btns, text='选择手机上的其他文件夹…', command=self.pick_phone_other).pack(side='left', padx=8)

        hint = ('提示：需手机解锁、USB 连接方式选「文件传输 / MTP」。'
                '安卓 11 以上系统通常禁止电脑直接访问 Android/data，若读取为空：'
                '在手机上用系统「文件管理」把 Android/data/tv.danmaku.bili/download '
                '整个复制到「内部存储/Download/bili」，再点上面的「选择手机上的其他文件夹」选中它即可。')
        ttk.Label(t2, text=hint, style='Warn.TLabel', wraplength=1000, justify='left').pack(anchor='w')

        # ===== Tab3 手动 =====
        t3 = ttk.Frame(self.nb, padding=10)
        self.nb.add(t3, text='③ 手动选择文件')
        ttk.Label(t3, text='视频文件 video.m4s：').grid(row=0, column=0, sticky='e', pady=3)
        self.man_v = tk.StringVar()
        ttk.Entry(t3, textvariable=self.man_v).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(t3, text='浏览…', command=lambda: self._pick_m4s(self.man_v))\
            .grid(row=0, column=2, padx=4)
        ttk.Label(t3, text='音频文件 audio.m4s：').grid(row=1, column=0, sticky='e', pady=3)
        self.man_a = tk.StringVar()
        ttk.Entry(t3, textvariable=self.man_a).grid(row=1, column=1, sticky='ew', padx=4)
        ttk.Button(t3, text='浏览…', command=lambda: self._pick_m4s(self.man_a, audio=True))\
            .grid(row=1, column=2, padx=4)
        ttk.Label(t3, text='保存标题：').grid(row=2, column=0, sticky='e', pady=3)
        self.man_title = tk.StringVar(value='手动合并视频')
        ttk.Entry(t3, textvariable=self.man_title).grid(row=2, column=1, sticky='ew', padx=4)
        ttk.Button(t3, text='添加到合并列表', command=self.add_manual).grid(row=3, column=1, sticky='w', pady=6)
        t3.columnconfigure(1, weight=1)

        # ===== 输出设置 =====
        out_frame = ttk.LabelFrame(self, text='输出设置', padding=8)
        out_frame.pack(fill='x', padx=10, pady=4)
        ttk.Label(out_frame, text='输出文件夹：').pack(side='left')
        default_out = self.cfg.get('out_dir') or os.path.join(
            os.path.expanduser('~'), 'Desktop', DEFAULT_OUT_NAME)
        self.out_var = tk.StringVar(value=default_out)
        ttk.Entry(out_frame, textvariable=self.out_var).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(out_frame, text='浏览…', command=self.pick_out).pack(side='left')
        self.open_after_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(out_frame, text='合并完成后打开输出文件夹',
                        variable=self.open_after_var).pack(side='left', padx=10)
        self.writeback_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(out_frame, text='合并完成后写回手机存储根目录',
                        variable=self.writeback_var).pack(side='left', padx=10)

        # ===== 任务列表 =====
        list_frame = ttk.LabelFrame(self, text='合并列表（点「选」列勾选/取消）', padding=8)
        list_frame.pack(fill='both', expand=True, padx=10, pady=4)
        cols = ('check', 'title', 'part', 'quality', 'source', 'size', 'status', 'wb')
        self.tree = ttk.Treeview(list_frame, columns=cols, show='headings',
                                 selectmode='extended', height=6)
        heads = [('check', '选', 46), ('title', '标题', 250), ('part', '分P', 205),
                 ('quality', '清晰度', 135), ('source', '来源', 92),
                 ('size', '大小', 92), ('status', '状态', 102), ('wb', '回写', 98)]
        for c, t, w in heads:
            self.tree.heading(c, text=t)
            anchor = 'center' if c in ('check', 'size', 'status', 'wb') else 'w'
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == 'title'))
        sb = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tree.bind('<Button-1>', self.on_tree_click)
        self.tree.tag_configure('done', foreground='#1a7f1a')
        self.tree.tag_configure('fail', foreground='#c0392b')
        self.tree.tag_configure('work', foreground='#1a5fb4')

        ops = ttk.Frame(self.bottom_area)
        ops.pack(fill='x', padx=10)
        ttk.Button(ops, text='全选', command=lambda: self.set_all(True)).pack(side='left')
        ttk.Button(ops, text='全不选', command=lambda: self.set_all(False)).pack(side='left', padx=4)
        ttk.Button(ops, text='移除选中行', command=self.remove_selected).pack(side='left')
        ttk.Button(ops, text='清空列表', command=self.clear_tasks).pack(side='left', padx=4)

        # ===== 进度与日志 =====
        prog = ttk.Frame(self.bottom_area, padding=(10, 6))
        prog.pack(fill='x')
        self.pvar = tk.StringVar(value='就绪')
        ttk.Label(prog, textvariable=self.pvar).pack(anchor='w')
        self.pb = ttk.Progressbar(prog, mode='determinate', maximum=1000)
        self.pb.pack(fill='x', pady=2)

        # ===== 底部：醒目的开始合并按钮 + 停止 =====
        # 使用原生 tk.Button，颜色渲染最稳定（ttk 自定义样式在原生主题下会显示空白）
        bottom = ttk.Frame(self.bottom_area, padding=(10, 0, 10, 8))
        bottom.pack(fill='x')
        bar = ttk.Frame(bottom)
        bar.pack(fill='x', pady=(2, 6))

        self.start_btn = tk.Button(
            bar, text='▶ 开始合并选中项', command=self.start_merge,
            bg='#0a8f4e', fg='white', activebackground='#0fae61',
            activeforeground='white', disabledforeground='#cfd8d3',
            font=('Microsoft YaHei UI', 15, 'bold'),
            relief='raised', bd=1, padx=26, pady=10, cursor='hand2')
        self.start_btn.pack(side='right')

        self.stop_btn = tk.Button(
            bar, text='■ 停止（取消手机读取）', command=self.stop_phone_op,
            bg='#d9534f', fg='white', activebackground='#c9302c',
            activeforeground='white', disabledforeground='#e6b8b6',
            font=('Microsoft YaHei UI', 11),
            relief='raised', bd=1, padx=14, pady=7, cursor='hand2', state='disabled')
        self.stop_btn.pack(side='right', padx=(0, 10))

        ttk.Label(bar, text='先在上方列表勾选要合并的视频 → 点右侧绿色大按钮',
                  style='Muted.TLabel').pack(side='left')

        self.log_box = tk.Text(bottom, height=5, state='disabled', background='#1e1e1e',
                               foreground='#d4d4d4', font=('Consolas', 10))
        self.log_box.pack(fill='both', expand=True)

        # 菜单
        # 菜单已在顶部自绘菜单栏中创建（见 _build_ui 开头）

        self.protocol('WM_DELETE_WINDOW', self.on_close)

    # ------------------------------------------------------------ 外观主题
    def set_theme_pref(self, pref):
        self.theme_pref = pref
        self.cfg['theme'] = pref
        save_config(self.cfg)
        self._last_sys_dark = system_prefers_dark()
        self.apply_theme(resolve_dark(pref))

    def _watch_system_theme(self):
        """跟随系统时，系统切换深浅色后自动同步（含标题栏）。"""
        try:
            cur = system_prefers_dark()
            if cur != self._last_sys_dark:
                self._last_sys_dark = cur
                if self.theme_pref == 'auto':
                    self.apply_theme(cur)
        except Exception:
            pass
        try:
            self.after(2000, self._watch_system_theme)
        except Exception:
            pass

    def apply_theme(self, dark):
        p = THEME_DARK if dark else THEME_LIGHT
        CURRENT_THEME['dark'] = dark
        CURRENT_THEME['p'] = p
        self._dark = dark

        self.configure(bg=p['bg'])
        style = ttk.Style(self)
        try:
            style.theme_use('clam' if dark else 'vista')
        except Exception:
            pass

        style.configure('.', background=p['bg'], foreground=p['fg'])
        style.configure('TFrame', background=p['bg'])
        style.configure('TLabelframe', background=p['bg'], bordercolor=p['line'],
                        relief='flat', borderwidth=1, lightcolor=p['bg'],
                        darkcolor=p['bg'])
        style.configure('TLabelframe.Label', background=p['bg'], foreground=p['fg'])
        style.configure('TLabel', background=p['bg'], foreground=p['fg'])
        style.configure('Muted.TLabel', background=p['bg'], foreground=p['muted'])
        style.configure('Warn.TLabel', background=p['bg'], foreground=p['warn'])

        style.configure('TButton', background=p['panel'], foreground=p['fg'],
                        bordercolor=p['line'], relief='flat', padding=(12, 6),
                        focusthickness=0, focuscolor=p['bg'],
                        lightcolor=p['panel'], darkcolor=p['panel'])
        style.map('TButton',
                  background=[('active', p['head_bg']), ('pressed', p['head_bg']),
                              ('disabled', p['panel'])],
                  foreground=[('disabled', p['muted'])],
                  lightcolor=[('active', p['head_bg']), ('pressed', p['head_bg'])],
                  darkcolor=[('active', p['head_bg']), ('pressed', p['head_bg'])])
        style.configure('TEntry', fieldbackground=p['field'], foreground=p['fg'],
                        bordercolor=p['line'], insertcolor=p['fg'], padding=(6, 5),
                        lightcolor=p['field'], darkcolor=p['field'],
                        borderwidth=1, relief='flat')
        style.configure('TCombobox', fieldbackground=p['field'], background=p['panel'],
                        foreground=p['fg'], arrowcolor=p['fg'],
                        bordercolor=p['line'], padding=(6, 5),
                        lightcolor=p['field'], darkcolor=p['field'])
        style.map('TCombobox',
                  fieldbackground=[('readonly', p['field'])],
                  foreground=[('readonly', p['fg'])],
                  background=[('active', p['head_bg'])])
        style.configure('TCheckbutton', background=p['bg'], foreground=p['fg'],
                        focuscolor=p['bg'], indicatorcolor=p['field'],
                        indicatormargin=(0, 0, 8, 0))
        style.map('TCheckbutton',
                  background=[('active', p['bg'])],
                  foreground=[('disabled', p['muted'])],
                  indicatorcolor=[('selected', p['accent']), ('active', p['head_bg'])])
        style.configure('Treeview', background=p['panel'], fieldbackground=p['panel'],
                        foreground=p['fg'], bordercolor=p['line'],
                        rowheight=30, borderwidth=1, relief='flat',
                        lightcolor=p['line'], darkcolor=p['line'])
        style.map('Treeview', background=[('selected', p['sel_bg'])],
                  foreground=[('selected', p['sel_fg'])])
        style.configure('Treeview.Heading', background=p['head_bg'],
                        foreground=p['fg'], relief='flat', bordercolor=p['line'],
                        padding=(6, 7), lightcolor=p['head_bg'],
                        darkcolor=p['head_bg'])
        style.map('Treeview.Heading', background=[('active', p['head_bg'])])
        style.configure('TNotebook', background=p['bg'], bordercolor=p['line'],
                        borderwidth=0, lightcolor=p['bg'], darkcolor=p['bg'])
        style.configure('TNotebook.Tab', background=p['head_bg'], foreground=p['muted'],
                        padding=(18, 8), bordercolor=p['line'], borderwidth=0,
                        lightcolor=p['head_bg'], darkcolor=p['head_bg'])
        style.map('TNotebook.Tab',
                  background=[('selected', p['panel'])],
                  foreground=[('selected', p['fg'])],
                  lightcolor=[('selected', p['panel'])],
                  darkcolor=[('selected', p['panel'])])
        style.configure('TProgressbar', background=p['accent'],
                        troughcolor=p['panel'], bordercolor=p['line'], thickness=12,
                        lightcolor=p['accent'], darkcolor=p['accent'])
        for sb in ('TScrollbar', 'Vertical.TScrollbar', 'Horizontal.TScrollbar'):
            style.configure(sb, background=p['panel'], troughcolor=p['bg'],
                            bordercolor=p['line'], arrowcolor=p['fg'],
                            borderwidth=0, lightcolor=p['panel'],
                            darkcolor=p['panel'])

        try:
            self.tree.tag_configure('done', foreground=p['done'])
            self.tree.tag_configure('fail', foreground=p['fail'])
            self.tree.tag_configure('work', foreground=p['work'])
        except Exception:
            pass

        try:
            self.topbar.configure(bg=p['bg'])
            for mb in getattr(self, '_mb_list', []):
                mb.configure(bg=p['bg'], fg=p['fg'], activebackground=p['head_bg'],
                             activeforeground=p['fg'], disabledforeground=p['muted'],
                             highlightthickness=0, bd=0, relief='flat')
            self.mb_sep.configure(bg=p['line'])
        except Exception:
            pass

        for m in getattr(self, '_menus', []):
            try:
                m.configure(bg=p['panel'], fg=p['fg'], activebackground=p['sel_bg'],
                            activeforeground=p['sel_fg'], selectcolor=p['accent'],
                            disabledforeground=p['muted'], bd=0, relief='flat')
            except Exception:
                pass

        self.update_idletasks()
        set_dark_titlebar(self, dark)

    # ------------------------------------------------------------ 日志/队列
    def log(self, msg):
        def append():
            self.log_box.configure(state='normal')
            self.log_box.insert('end', time.strftime('[%H:%M:%S] ') + msg + '\n')
            self.log_box.see('end')
            self.log_box.configure(state='disabled')
        self.after(0, append)

    def thread_log(self, msg):
        self.q.put(('log', msg))

    def _poll_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == 'log':
                    self.log(ev[1])
                elif kind == 'add_tasks':
                    self.add_tasks_to_tree(ev[1])
                elif kind == 'add_task_one':
                    self.add_tasks_to_tree([ev[1]])
                elif kind == 'status':
                    iid, status, tag = ev[1], ev[2], ev[3]
                    if iid in self.tasks:
                        self.tasks[iid]['status'] = status
                        vals = list(self.tree.item(iid, 'values'))
                        vals[6] = status
                        self.tree.item(iid, values=vals, tags=(tag,) if tag else ())
                elif kind == 'wb':
                    iid, wb = ev[1], ev[2]
                    if iid in self.tasks:
                        self.tasks[iid]['wb'] = wb
                        vals = list(self.tree.item(iid, 'values'))
                        vals[7] = wb
                        self.tree.item(iid, values=vals)
                elif kind == 'progress':
                    frac, text = ev[1], ev[2]
                    if frac is not None and frac >= 0:
                        self.pb['value'] = frac * 1000
                    if text:
                        self.pvar.set(text)
                elif kind == 'buttons':
                    op = ev[2] if len(ev) > 2 else 'merge'
                    self.set_busy(not ev[1], op=op)
                elif kind == 'scan_info':
                    self.pvar.set(ev[1])
                elif kind == 'merge_done':
                    self.on_merge_done(ev[1], ev[2])
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def set_busy(self, busy, op='merge'):
        """busy: 是否有耗时操作；op: 'merge' 合并 / 'phone' 手机读取"""
        self.running = busy
        base = 'disabled' if busy else 'normal'
        if op == 'merge':
            for b in (self.start_btn, self.scan_local_btn, self.scan_phone_btn):
                b.configure(state=base)
            if self.stop_btn is not None:
                self.stop_btn.configure(state='disabled')
        else:
            for b in (self.start_btn, self.scan_local_btn, self.scan_phone_btn):
                b.configure(state=base)
            if self.stop_btn is not None:
                self.stop_btn.configure(state='normal' if busy else 'disabled')

    # ------------------------------------------------------------ 选择交互
    def pick_local(self):
        d = filedialog.askdirectory(title='选择 B站缓存目录（download 或其上层都可以）')
        if d:
            self.local_var.set(d)
            self.cfg['last_local_dir'] = d
            save_config(self.cfg)

    def pick_out(self):
        d = filedialog.askdirectory(title='选择输出文件夹')
        if d:
            self.out_var.set(d)

    def _pick_m4s(self, var, audio=False):
        p = filedialog.askopenfilename(
            title='选择音频 m4s' if audio else '选择视频 m4s',
            filetypes=[('m4s/mp4 文件', '*.m4s *.mp4'), ('所有文件', '*.*')])
        if p:
            var.set(p)
            if not audio and self.man_title.get() in ('手动合并视频', ''):
                guess = os.path.basename(os.path.dirname(os.path.dirname(p)))
                if guess and not guess.isdigit():
                    self.man_title.set(guess)

    def pick_phone_for_local(self):
        dlg = PhoneBrowser(self, self.mtp, self.device_var.get().strip())
        self.wait_window(dlg)
        if dlg.selected is not None:
            segs = dlg.selected
            self.local_var.set('[手机] 内部存储设备/' + '/'.join(segs))
            self._phone_segs = segs
            self.do_scan_phone(segs)

    def pick_phone_other(self):
        dlg = PhoneBrowser(self, self.mtp, self.device_var.get().strip())
        self.wait_window(dlg)
        if dlg.selected is not None:
            self.phone_path_var.set('内部存储设备/' + '/'.join(dlg.selected))
            self.do_scan_phone(dlg.selected)

    def refresh_devices(self):
        def work():
            try:
                devs = self.mtp.devices()
                self.after(0, lambda: self.device_combo.configure(values=devs))
                if devs:
                    cur = self.device_var.get().strip()
                    if cur not in devs:
                        self.device_var.set(devs[0])
                        self.thread_log('已自动识别设备：%s（可下拉切换）' % devs[0])
                    else:
                        self.thread_log('检测到设备：%s' % '、'.join(devs))
                else:
                    self.thread_log('未检测到手机设备：请插好数据线、手机解锁并选「文件传输 / MTP」')
            except MtpError as e:
                self.thread_log('设备枚举失败：%s' % e)
        threading.Thread(target=work, daemon=True).start()

    def auto_detect_device(self):
        """启动时自动检测并回填设备名，避免默认写死设备型号。"""
        def work():
            try:
                devs = self.mtp.devices()
                if devs:
                    self.after(0, lambda: self.device_combo.configure(values=devs))
                    cur = self.device_var.get().strip()
                    if cur not in devs:
                        self.device_var.set(devs[0])
                        self.after(0, lambda: self.thread_log('已自动识别设备：%s' % devs[0]))
                    else:
                        self.after(0, lambda: self.device_combo.set(cur))
            except MtpError:
                pass  # 静默：未接手机属正常，用户可随时点「刷新设备」
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 扫描
    def do_scan_local(self):
        root = self.local_var.get().strip()
        if root.startswith('[手机]'):
            segs = getattr(self, '_phone_segs', None)
            if segs:
                self.do_scan_phone(segs)
                return
            root = ''
        if not root or not os.path.isdir(root):
            messagebox.showwarning('提示', '请先选择有效的本地缓存文件夹')
            return
        self.cfg['last_local_dir'] = root
        save_config(self.cfg)
        self.pvar.set('正在扫描本地文件夹…')

        def work():
            try:
                tasks = scan_local_folder(root, self.ffmpeg, self.thread_log)
                self.q.put(('add_tasks', tasks))
                self.q.put(('scan_info', '本地扫描完成，发现 %d 个视频' % len(tasks)))
                if not tasks:
                    self.thread_log('没有在该目录发现 B站缓存（应包含 entry.json 和 video.m4s/audio.m4s）')
            except Exception as e:
                self.thread_log('扫描失败：%s' % e)

        threading.Thread(target=work, daemon=True).start()

    def do_scan_phone_default(self):
        self.do_scan_phone(PHONE_DEFAULT_SEGS)

    def do_scan_phone(self, segs):
        device = self.device_var.get().strip()
        if not device:
            messagebox.showwarning('提示', '未选择手机设备：请点「刷新设备」自动识别，或手动填写设备名')
            return
        self.cfg['device'] = device
        save_config(self.cfg)
        self.mtp.reset_cancel()
        self.scan_cancel = False
        self.scan_phone_btn.configure(state='disabled')

        self.pvar.set('正在读取手机视频清单（只读标题等元数据，不复制视频）…')

        def work():
            def build_task(it):
                rel = str(it.get('rel', '')).replace('\\', '/')
                rel_parts = [x for x in rel.split('/') if x]
                title = it.get('title') or '未命名'
                qtag = str(it.get('quality', '') or '')
                qlabel = quality_label(qtag, None) if qtag else '未知清晰度'
                try:
                    size = int(it.get('size', 0) or 0)
                except Exception:
                    size = 0
                try:
                    page = int(it.get('page', 1) or 1)
                except Exception:
                    page = 1
                return {
                    'id': 'mtp_' + hashlib.md5(rel.encode('utf-8')).hexdigest()[:12],
                    'kind': 'mtp',
                    'device': device,
                    'title': title,
                    'part': it.get('part', '') or '',
                    'page': page,
                    'quality': qlabel,
                    'size': size,
                    'segs': list(segs) + rel_parts,
                    'cid_dir': None,
                    'v_files': [], 'a_files': [],
                    'status': '待合并',
                }

            counter = [0]
            def on_item(it):
                task = build_task(it)
                counter[0] += 1
                # 扫描出一个立即增量加入列表，不用等全部完成
                self.q.put(('add_task_one', task))
                self.q.put(('progress', None, '已读取 %d 个视频信息（读一个显示一个）…' % counter[0]))

            try:
                self.q.put(('progress', 0.0, '正在读取手机视频清单（读一个显示一个）…'))
                self.q.put(('buttons', False, 'phone'))
                self.thread_log('正在读取手机视频清单（只读 entry.json，读一个显示一个）…')
                items = self.mtp.metadata(device, segs, stream_cb=on_item)
                if self.mtp.cancel_requested:
                    self.q.put(('scan_info', '已取消'))
                    return
                self.thread_log('视频清单读取完成，发现 %d 个视频' % len(items))
                if not items:
                    self.thread_log('没有发现 B站缓存。请确认 B站APP 里已缓存视频，手机已解锁、处于文件传输模式。')
                    self.q.put(('scan_info', '手机扫描完成，未发现视频'))
                    return
                self.q.put(('scan_info',
                            '手机清单读取完成：发现 %d 个视频。勾选后点开始合并，仅复制并合并你选中的视频。'
                            % len(items)))
            except MtpError as e:
                self.thread_log('读取手机失败：%s' % e)
                if e.code == 'CANCELLED':
                    self.q.put(('scan_info', '已取消'))
                elif e.code in ('DEVICE_LOCKED', 'DEVICE_NOT_FOUND', 'PATH_NOT_FOUND'):
                    self.thread_log('请确认：手机已解锁、USB 模式为「文件传输」、设备名正确。')
                    self.q.put(('scan_info', '手机读取失败'))
                else:
                    self.q.put(('scan_info', '手机读取失败'))
            except Exception as e:
                self.thread_log('读取手机失败：%s' % e)
                self.q.put(('scan_info', '手机读取失败'))
            finally:
                self.q.put(('buttons', True, 'phone'))

        threading.Thread(target=work, daemon=True).start()

    def stop_phone_op(self):
        """停止正在进行的手机读取/复制操作，防止卡死。"""
        self.scan_cancel = True
        self.mtp.request_cancel()
        self.pvar.set('正在停止…')
        self.log('已请求停止手机读取/复制…')

        def cleanup_later():
            time.sleep(1.0)
            if self.mirror_dir and os.path.isdir(self.mirror_dir):
                try:
                    shutil.rmtree(self.mirror_dir, ignore_errors=True)
                    self.log('已清理未完成的临时副本')
                except Exception:
                    pass
            self.q.put(('buttons', True, 'phone'))

        threading.Thread(target=cleanup_later, daemon=True).start()

    def add_manual(self):
        v = self.man_v.get().strip()
        a = self.man_a.get().strip()
        title = self.man_title.get().strip() or '手动合并视频'
        if not v or not os.path.isfile(v):
            messagebox.showwarning('提示', '请先选择有效的视频 m4s 文件')
            return
        if a and not os.path.isfile(a):
            messagebox.showwarning('提示', '音频文件无效（可留空，仅合成画面）')
            return
        task = {
            'id': uuid.uuid4().hex, 'kind': 'manual',
            'title': title, 'part': '', 'page': 1, 'quality': '手动指定',
            'cid_dir': None, 'segs': None, 'device': None,
            'v_files': [v], 'a_files': [a] if a else [],
            'size': sum(os.path.getsize(x) for x in [v, a] if x and os.path.isfile(x)),
            'status': '待合并',
        }
        self.add_tasks_to_tree([task])
        self.pvar.set('已添加手动任务')

    # ------------------------------------------------------------ 列表
    def add_tasks_to_tree(self, tasks):
        existing = {self._dedup_key(self.tasks[i]) for i in self.tasks}
        added = 0
        for t in tasks:
            key = self._dedup_key(t)
            if key in existing:
                continue
            existing.add(key)
            self.tasks[t['id']] = t
            source = {'local': '本地', 'mtp': '手机', 'manual': '手动'}.get(t['kind'], '')
            part = ('P%d %s' % (t['page'], t['part'])) if t.get('page') and t['page'] > 1 else (t.get('part') or '')
            self.tree.insert('', 'end', iid=t['id'],
                             values=('☑', t['title'], part, t['quality'], source,
                                     human_size(t.get('size')), '待合并', '—'))
            added += 1
        if added:
            self.log('已加入 %d 个任务（列表共 %d 个）' % (added, len(self.tasks)))

    @staticmethod
    def _dedup_key(t):
        if t.get('kind') == 'mtp':
            # 本地镜像扫描的 mtp 任务 segs 为 None，用 cid_dir 去重；
            # 直选手机文件夹的任务 segs 非空，用 segs 去重。
            if t.get('cid_dir'):
                return 'mtp:' + t['cid_dir']
            return 'mtp:' + '/'.join(t.get('segs') or [])
        if t.get('kind') == 'manual':
            return 'manual:' + '|'.join(t.get('v_files') or [])
        return 'local:' + (t.get('cid_dir') or '')

    def on_tree_click(self, event):
        # 只有点击最左侧“选”列（方块）才切换勾选，点行其他位置不改变勾选
        if self.tree.identify_region(event.x, event.y) not in ('cell', 'tree'):
            return
        if self.tree.identify_column(event.x) != '#1':
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        vals = list(self.tree.item(iid, 'values'))
        vals[0] = '☐' if vals[0] == '☑' else '☑'
        self.tree.item(iid, values=vals)

    def _checked_ids(self):
        out = []
        for iid in self.tree.get_children():
            if self.tree.item(iid, 'values')[0] == '☑':
                out.append(iid)
        return out

    def set_all(self, checked):
        mark = '☑' if checked else '☐'
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, 'values'))
            vals[0] = mark
            self.tree.item(iid, values=vals)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tasks.pop(iid, None)
            self.tree.delete(iid)

    def clear_tasks(self):
        self.tasks.clear()
        self.tree.delete(*self.tree.get_children())

    # ------------------------------------------------------------ 合并
    def start_merge(self):
        ids = self._checked_ids()
        if not ids:
            messagebox.showinfo('提示', '列表里没有勾选的任务')
            return
        out_dir = self.out_var.get().strip()
        if not out_dir:
            messagebox.showwarning('提示', '请先选择输出文件夹')
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror('错误', '输出文件夹无法创建：%s' % e)
            return
        if not self.ffmpeg:
            self.offer_ffmpeg_download()
            return
        self.cfg['out_dir'] = out_dir
        save_config(self.cfg)
        self.set_busy(True, op='merge')
        self.pb['value'] = 0

        def work():
            ok, fail = [], []
            total = len(ids)
            for idx, iid in enumerate(ids, 1):
                t = self.tasks[iid]
                title = t['title']
                self.q.put(('status', iid, '合并中…', 'work'))
                self.q.put(('progress', 0.0, '(%d/%d) 正在合并：%s' % (idx, total, title)))
                self.q.put(('log', '── (%d/%d) %s' % (idx, total, title)))
                try:
                    def cb(frac, text=None):
                        if frac is None:
                            return
                        overall = ((idx - 1) + max(frac, 0)) / total
                        msg = text or ('(%d/%d) 正在合并：%s  %.0f%%'
                                       % (idx, total, title, frac * 100))
                        self.q.put(('progress', overall, msg))
                    out_path = process_task(t, out_dir, self.ffmpeg, self.mtp,
                                            self.thread_log, cb)
                    self.q.put(('status', iid, '完成', 'done'))
                    self.q.put(('log', '  完成 → %s' % out_path))
                    ok.append((out_path, iid))
                except Exception as e:
                    self.q.put(('status', iid, '失败', 'fail'))
                    self.q.put(('log', '  失败：%s' % e))
                    fail.append((title, str(e)))
            self.q.put(('progress', 1.0, '全部结束：成功 %d，失败 %d' % (len(ok), len(fail))))
            self.q.put(('buttons', True, 'merge'))
            self.q.put(('merge_done', ok, fail))

        threading.Thread(target=work, daemon=True).start()

    def on_merge_done(self, ok, fail):
        # 不再合并完就删 mirror，保留到软件关闭时再清理（方便重复合并/调整）
        if ok and self.open_after_var.get():
            try:
                os.startfile(self.out_var.get().strip())
            except Exception:
                pass
        # 可选：写回手机存储根目录
        if ok and self.writeback_var.get():
            self._writeback_to_phone(ok)
        if fail:
            messagebox.showwarning('合并完成（有失败）',
                                   '成功 %d 个，失败 %d 个。\n失败原因见下方日志。' % (len(ok), len(fail)))
        else:
            messagebox.showinfo('完成', '全部 %d 个视频合并成功！' % len(ok))

    def _writeback_to_phone(self, ok_list):
        """把合并好的文件写回手机 Download 目录，结果反映到列表「回写」列。"""
        device = self.device_var.get().strip()
        if not device:
            self.log('未选择手机设备，跳过写回。')
            return
        target_segs = ['Download']
        self.log('开始把 %d 个合并文件写回手机 Download 目录…' % len(ok_list))

        def work():
            ok_cnt = 0
            for item in ok_list:
                out_path, iid = item[0], item[1]
                if not out_path or not os.path.isfile(out_path):
                    self.q.put(('wb', iid, '跳过'))
                    continue
                try:
                    self.mtp.upload_file(device, target_segs, out_path, timeout=1800)
                    self.log('  已写回手机：%s' % os.path.basename(out_path))
                    self.q.put(('wb', iid, '成功'))
                    ok_cnt += 1
                except MtpError as e:
                    self.log('  写回失败：%s（%s）' % (os.path.basename(out_path), e))
                    self.q.put(('wb', iid, '失败'))
            self.log('写回完成：成功 %d / %d 个。' % (ok_cnt, len(ok_list)))

        threading.Thread(target=work, daemon=True).start()

    def cleanup_mirror(self):
        """清理手机直读/按需复制产生的所有本地临时文件（仅电脑临时副本，绝不涉及手机源文件）。"""
        try:
            names = os.listdir(WORK_DIR)
        except Exception:
            return
        cleaned = 0
        for name in names:
            p = os.path.join(WORK_DIR, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            self.log('已自动清理电脑上的临时文件（%d 项）' % cleaned)

    # ------------------------------------------------------------ ffmpeg
    def specify_ffmpeg(self):
        p = filedialog.askopenfilename(title='选择 ffmpeg.exe',
                                       filetypes=[('ffmpeg.exe', 'ffmpeg.exe'), ('所有文件', '*.*')])
        if p:
            self.ffmpeg = p
            self.cfg['ffmpeg'] = p
            save_config(self.cfg)
            self.log('已指定 FFmpeg：%s' % p)

    def offer_ffmpeg_download(self, force=False):
        if self.ffmpeg and not force:
            return
        ans = messagebox.askyesno(
            '需要 FFmpeg',
            '未检测到 FFmpeg，它是合并视频必需的免费开源组件。\n\n'
            '点「是」自动下载官方版本（约 80MB，只下载一次）；\n'
            '点「否」可稍后用菜单「设置 → 手动指定 ffmpeg.exe」。')
        if not ans:
            return
        win = tk.Toplevel(self)
        win.title('下载 FFmpeg')
        win.geometry('460x150')
        win.transient(self)
        win.configure(bg=CURRENT_THEME['p']['bg'])
        ttk.Label(win, text='准备下载…').pack(pady=10)
        pb = ttk.Progressbar(win, maximum=1000, length=400)
        pb.pack(pady=4)
        info = tk.StringVar()
        ttk.Label(win, textvariable=info).pack(pady=6)
        win.update_idletasks()
        set_dark_titlebar(win, CURRENT_THEME['dark'])

        def cb(frac, text):
            self.after(0, lambda: (pb.configure(value=max(frac, 0) * 1000), info.set(text)))

        def work():
            try:
                p = download_ffmpeg(cb)
                self.ffmpeg = p
                self.cfg['ffmpeg'] = p
                save_config(self.cfg)
                self.after(0, lambda: (win.destroy(), self.log('FFmpeg 就绪：%s' % p)))
            except Exception as e:
                self.after(0, lambda: (win.destroy(),
                                       messagebox.showerror('下载失败', '自动下载失败：%s\n'
                                                            '可自行下载 ffmpeg 后在设置里指定。' % e)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 其他
    def show_help(self):
        messagebox.showinfo(
            '使用说明',
            '1. 本地扫描：把手机里的 download 文件夹复制到电脑任意位置，选择后点「开始扫描」，\n'
            '   工具会自动识别所有缓存视频并挑选最高清晰度。\n\n'
            '2. 手机直读：手机解锁 → USB 模式选「文件传输/MTP」→ 点「一键读取」。\n'
            '   工具会把缓存整批复制到电脑本地快速识别，重复扫描会复用已有副本，\n'
            '   软件关闭时自动清理临时文件。勾选「写回手机」可把合并好的 MP4 推送回手机 Download 目录。\n'
            '   读取/复制过程中可随时点红色「停止」按钮。\n'
            '   安卓 11+ 通常禁止电脑访问 Android/data：在手机系统「文件管理」里把\n'
            '   Android/data/tv.danmaku.bili/download 复制到 Download/bili，\n'
            '   再用「选择手机上的其他文件夹」选中它。\n\n'
            '3. 手动模式：分别选择 video.m4s 和 audio.m4s。\n\n'
            '合并原理：FFmpeg 流复制（不重编码），速度接近文件复制，音画保持原始同步。\n'
            'index.json 是 B站缓存的分片索引，工具会自动解析。\n\n'
            '外观：默认跟随 Windows 深/浅色自动切换（含标题栏）；可在\n'
            '「设置 → 外观」里固定为浅色模式或深色模式，选择会被记住。')

    def show_about(self):
        # 版权声明：作者 wan，B站 UID 500886854，保留原创权
        messagebox.showinfo(
            '关于',
            'B站缓存合并工具 v2.5\n\n'
            '作者：wan\n'
            'B站主页：https://space.bilibili.com/500886854\n\n'
            '本工具为个人原创作品，保留全部原创权利。\n'
            '仅供个人学习与备份自己缓存的内容使用，请勿用于商业用途或二次分发。\n'
            '使用本工具造成的任何版权问题，由使用者自行承担。')

    def on_close(self):
        try:
            shutil.rmtree(os.path.join(WORK_DIR, 'mtp_meta'), ignore_errors=True)
        except Exception:
            pass
        try:
            self.cleanup_mirror()
        except Exception:
            pass
        self.destroy()


def selftest():
    """隐藏自检：不显示窗口，检查内嵌资源与主题是否可用（打包后验证用）。"""
    import tempfile
    lines = []
    lines.append('ffmpeg=' + str(find_ffmpeg({})))
    lines.append('ps1=' + str(os.path.isfile(HELPER_PS1)) + ' -> ' + HELPER_PS1)
    lines.append('dark_system=' + str(system_prefers_dark()))
    app = App()
    app.withdraw()
    app.update_idletasks()
    s = ttk.Style(app)
    lines.append('theme=' + s.theme_use())
    lines.append('tree_bg=' + str(s.lookup('Treeview', 'background')))
    lines.append('root_bg=' + str(app.cget('bg')))
    lines.append('native_menu=' + repr(app.cget('menu')))
    lines.append('topbar_bg=' + str(app.topbar.cget('bg')))
    lines.append('mb_bg=' + str(app.mb_set.cget('bg')))
    lines.append('mb_fg=' + str(app.mb_set.cget('fg')))
    try:
        import tkinter.font as tkfont
        f0 = tkfont.nametofont('TkDefaultFont')
        lines.append('font=%s %s' % (f0.actual('family'), f0.actual('size')))
        lines.append('rowheight=' + str(s.lookup('Treeview', 'rowheight')))
        lines.append('line_color=' + str(s.lookup('Treeview', 'bordercolor')))
    except Exception:
        pass
    lines.append('version_title=' + app.title())
    app.destroy()
    path = os.path.join(tempfile.gettempdir(), 'bili_selftest.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n'.join(lines))
    return 0


def main():
    if '--selftest' in sys.argv:
        try:
            return selftest()
        except Exception:
            import traceback
            app_print(traceback.format_exc())
            return 1
    os.makedirs(WORK_DIR, exist_ok=True)
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
