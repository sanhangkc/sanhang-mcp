#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sanhang-mcp —— 三行弦图（MathMagic）MCP 服务。

把一句自然语言数学描述，变成一段可以播放的 Manim 动画视频。

三种运行模式（默认不连接任何服务器，也不消耗作者资源）：
- 本地模式（SANHANG_LOCAL=1）：用你自己的 DeepSeek API Key 出码，用你本机的
  Manim 渲染，视频落在本地文件，全程不访问作者服务器。
- 后端模式（SANHANG_BASE_URL=你的后端）：连接你自建或三行弦图托管后端。
- 未配置（none）：启动即提示如何配置，绝不偷偷连回作者服务器。

设计取向：零第三方依赖（只用 Python 标准库）、stdio 传输、Python 3.8+。

协议：Model Context Protocol 2025-06-18（向下兼容 2025-03-26 / 2024-11-05）。
传输：stdio，一行一条 JSON-RPC 消息；stdout 只写 MCP 消息，日志一律走 stderr。

用法：
    python3 sanhang_mcp.py            # 作为 MCP server 被客户端拉起
    python3 sanhang_mcp.py --check    # 自检（按当前模式检查后端 / 本地依赖）
    python3 sanhang_mcp.py --whoami   # 查看当前模式与配额标识
    python3 sanhang_mcp.py --version
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SERVER_NAME = 'sanhang-mcp'
SERVER_VERSION = '1.1.0'

# 支持的协议版本，索引 0 为服务端最新
PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
LATEST_PROTOCOL = PROTOCOL_VERSIONS[0]

# 默认空：本包不内置任何后端地址，不会偷偷连回作者服务器，也不会消耗作者
# 的算力或 token。使用者要么开本地模式（SANHANG_LOCAL=1），要么自己指定后端
# （SANHANG_BASE_URL=...）。这是 v1.1.0 起的关键改动。
DEFAULT_BASE_URL = ''
DEEPSEEK_DEFAULT_URL = 'https://api.deepseek.com/chat/completions'
# 配额标识的兜底值：仅当本机标识无法生成也无法落盘时才会用到。
# 正常情况下由 _local_identifier() 在使用者机器上首次运行生成并落盘复用。
FALLBACK_USER_IDENTIFIER = 'mcp-client'
STATE_DIRNAME = '.sanhang-mcp'

# 未配置后端也未开本地模式时，所有工具统一报这个错。明确告诉使用者：本包
# 默认不连任何服务器，两种用法二选一。
_CONFIG_ERROR = (
    '未配置渲染后端，也未开启本地模式，无法生成弦图。\n'
    '\n'
    'sanhang-mcp 默认不会连接任何服务器，也不会消耗作者的算力或 token。\n'
    '两种用法二选一：\n'
    '\n'
    '1) 本地模式（用你自己的 DeepSeek Key + 本机 Manim，全程不访问外部服务器）：\n'
    '   export SANHANG_LOCAL=1\n'
    '   export SANHANG_DEEPSEEK_API_KEY=sk-你的key\n'
    '   uvx sanhang-mcp\n'
    '\n'
    '2) 连接你自己的后端 / 三行弦图托管服务：\n'
    '   export SANHANG_BASE_URL=https://你的后端/api\n'
    '\n'
    '详见 README 的「本地模式」与「自备后端」两节。'
)

# 渲染轮询：单次询问的 HTTP 超时 / 轮询间隔 / 默认最长等待
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_WAIT_SECONDS = 240
MAX_WAIT_SECONDS = 900
MIN_WAIT_SECONDS = 10

CODE_INLINE_LIMIT = 6000

_WRITE_LOCK = threading.Lock()
_CANCEL_EVENTS = {}          # request_id(str) -> threading.Event
_CANCEL_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 日志：只写 stderr，stdout 是协议通道，绝不能污染
# --------------------------------------------------------------------------- #
def _getenv(suffix, default=''):
    """读环境变量。SANHANG_ 前缀优先，MANIM_MCP_ 前缀兼容旧配置。"""
    for prefix in ('SANHANG_', 'MANIM_MCP_'):
        value = os.environ.get(prefix + suffix)
        if value:
            return value
    return default


def _state_dir():
    """存放本机标识的目录。可用 SANHANG_STATE_DIR 覆盖（容器/CI 场景）。"""
    configured = _getenv('STATE_DIR')
    if configured:
        return configured
    home = os.path.expanduser('~')
    if home and home != '~' and os.path.isdir(home):
        return os.path.join(home, STATE_DIRNAME)
    return os.path.join(tempfile.gettempdir(), STATE_DIRNAME)


def _local_identifier():
    """返回本机稳定的配额标识，首次运行时生成并落盘。

    为什么需要这个：早期默认值是固定的 'mcp-client'，意味着所有不设标识的
    使用者共用同一个配额桶 —— 前几个人就能把日额度吃空，后面的人全是 429，
    而且出问题时也分不清是谁在用。改成一人一标识后，每个人的用量各自独立，
    后端按标识统计与限流才有意义。
    """
    directory = _state_dir()
    path = os.path.join(directory, 'identifier')
    try:
        with open(path, 'r') as handle:
            cached = handle.read().strip()
        if cached and len(cached) <= 64:
            return cached
    except Exception:
        pass

    generated = 'mcp-' + uuid.uuid4().hex[:12]
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        temp_path = path + '.tmp'
        with open(temp_path, 'w') as handle:
            handle.write(generated)
        os.replace(temp_path, path)
    except Exception:
        # 目录不可写（只读挂载等）：本次进程内仍用生成值，不阻塞使用
        pass
    return generated


def log(message):
    if _getenv('LOG').lower() in ('0', 'off', 'quiet', 'silent'):
        return
    try:
        sys.stderr.write('[sanhang-mcp] %s\n' % message)
        sys.stderr.flush()
    except Exception:
        pass


def _resolve_mode():
    """运行模式：local（自备 key + 本地 Manim）/ backend（自建或托管后端）/ none（未配置）。

    判定顺序：SANHANG_LOCAL 真值优先；其次看 SANHANG_BASE_URL 是否非空；
    都没有就是 none，所有工具返回 _CONFIG_ERROR 而不会偷偷连回作者服务器。
    """
    if _getenv('LOCAL').lower() in ('1', 'true', 'yes', 'on'):
        return 'local'
    if (_getenv('BASE_URL') or '').strip():
        return 'backend'
    return 'none'


class Config(object):
    """运行期配置，全部来自环境变量（便于 MCP 客户端用 env 段注入）。"""

    def __init__(self):
        self.mode = _resolve_mode()
        if self.mode == 'backend':
            self.base_url = _getenv('BASE_URL').strip().rstrip('/')
        else:
            self.base_url = ''
        self.user_identifier = _getenv('USER_IDENTIFIER') or _local_identifier()
        self.http_timeout = _env_float('HTTP_TIMEOUT', DEFAULT_HTTP_TIMEOUT, 3.0, 120.0)
        self.poll_interval = _env_float('POLL_INTERVAL', DEFAULT_POLL_INTERVAL, 1.0, 30.0)
        self.wait_seconds = _env_int('WAIT', DEFAULT_WAIT_SECONDS, MIN_WAIT_SECONDS, MAX_WAIT_SECONDS)


def _env_float(name, default, low, high):
    try:
        value = float(_getenv(name) or default)
    except (TypeError, ValueError):
        return float(default)
    return max(low, min(high, value))


def _env_int(name, default, low, high):
    try:
        value = int(float(_getenv(name) or default))
    except (TypeError, ValueError):
        return int(default)
    return max(low, min(high, value))


CONFIG = Config()


# --------------------------------------------------------------------------- #
# 后端访问
# --------------------------------------------------------------------------- #
class BackendError(Exception):
    """后端返回的业务错误（可展示给用户）。"""

    def __init__(self, message, code=None, status=None):
        Exception.__init__(self, message)
        self.message = message
        self.code = code
        self.status = status


class TaskCancelled(Exception):
    """客户端发了 notifications/cancelled，渲染任务随之中断。"""


def _join(base, path):
    return base.rstrip('/') + (path if path.startswith('/') else '/' + path)


def _http_json(method, path, payload=None, timeout=None):
    """对渲染后端发一次 JSON 请求，返回解析后的 dict。

    所有失败都收敛成 BackendError，消息面向终端用户而不是开发者。
    """
    url = _join(CONFIG.base_url, path)
    headers = {
        'Accept': 'application/json',
        'User-Agent': '%s/%s' % (SERVER_NAME, SERVER_VERSION),
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=timeout or CONFIG.http_timeout)
    except urllib.error.HTTPError as exc:
        raw = _safe_read(exc)
        parsed = _parse_json(raw)
        raise _translate_http_error(exc.code, parsed, raw)
    except urllib.error.URLError as exc:
        raise BackendError('无法连接渲染后端 %s：%s' % (url, getattr(exc, 'reason', exc)))
    except Exception as exc:                      # socket.timeout / ssl 错误等
        raise BackendError('请求渲染后端失败（%s）：%s' % (url, exc))

    try:
        raw = response.read().decode('utf-8', 'replace')
    finally:
        try:
            response.close()
        except Exception:
            pass

    parsed = _parse_json(raw)
    if parsed is None:
        raise BackendError('渲染后端返回了非 JSON 内容（前 200 字）：%s' % raw[:200])
    if isinstance(parsed, dict) and parsed.get('error') and 'result' not in parsed:
        raise BackendError(str(parsed.get('error')), code=parsed.get('code'))
    return parsed


def _safe_read(exc):
    try:
        return exc.read().decode('utf-8', 'replace')
    except Exception:
        return ''


def _parse_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _translate_http_error(status, parsed, raw):
    detail = ''
    code = None
    if isinstance(parsed, dict):
        code = parsed.get('code')
        detail = parsed.get('error') or parsed.get('message') or ''
    if not detail and raw:
        detail = raw[:200]

    if status == 400:
        return BackendError('描述不合法：%s' % (detail or '请检查 prompt'), code=code, status=status)
    if status == 403 and code == 'QUOTA_GEN_EXHAUSTED':
        return BackendError(
            '免费创作额度已用尽。三行弦图会员可无限创作并解锁高清下载，'
            '详情见 https://sanhangmath.top', code=code, status=status)
    if status == 403:
        return BackendError('无权访问：%s' % (detail or '权限不足'), code=code, status=status)
    if status == 404:
        return BackendError('任务不存在或已过期（渲染结果默认保留一段时间）', code=code, status=status)
    if status == 429:
        return BackendError('请求过于频繁，已触发每分钟限流，请稍后重试', code=code, status=status)
    if status >= 500:
        return BackendError('渲染服务内部错误，请稍后重试（%s）' % (detail or status), code=code, status=status)
    return BackendError('后端返回 %s：%s' % (status, detail or '未知错误'), code=code, status=status)


def _quota_line(quota):
    """把 /generate 返回的配额快照压成一行提示（会员返回无限则不展示）。"""
    if not isinstance(quota, dict):
        return ''
    gen = quota.get('gen') or {}
    daily = gen.get('daily') or {}
    if daily.get('remaining') is None:
        return ''
    try:
        return '今日剩余免费创作次数：%s 次' % int(daily.get('remaining'))
    except (TypeError, ValueError):
        return ''


# --------------------------------------------------------------------------- #
# 轮询
# --------------------------------------------------------------------------- #
_STATUS_LABEL = {
    'pending': '排队中',
    'processing': '正在生成代码并渲染',
    'rendering': '正在渲染视频',
    'completed': '已完成',
}


def _poll_task(task_id, timeout_seconds, ctx):
    """轮询 /api/task/<id> 直到完成；超时返回 None（任务仍在后端继续跑）。"""
    started = time.time()
    deadline = started + max(MIN_WAIT_SECONDS, timeout_seconds)
    last_progress = -1

    while True:
        if ctx.get('cancel') is not None and ctx['cancel'].is_set():
            _cancel_remote(task_id)
            raise TaskCancelled('已按客户端请求取消渲染任务')

        data = _http_json('GET', '/task/%s' % urllib.parse.quote(task_id))
        status = str(data.get('status') or '').lower()
        try:
            progress = int(float(data.get('progress') or 0))
        except (TypeError, ValueError):
            progress = 0

        if status == 'completed':
            _notify_progress(ctx, 100, 100, '渲染完成')
            return data
        if status == 'failed':
            raise BackendError(str(data.get('error') or '渲染失败（后端未给出原因）'))
        if status in ('cancelled', 'canceled'):
            raise TaskCancelled('后端任务已取消')

        if progress != last_progress:
            last_progress = progress
            _notify_progress(ctx, progress, 100, _STATUS_LABEL.get(status, status or '处理中'))

        if time.time() >= deadline:
            return None
        time.sleep(CONFIG.poll_interval)


def _cancel_remote(task_id):
    try:
        _http_json('POST', '/task/%s/cancel' % urllib.parse.quote(task_id))
        log('已请求后端取消任务 %s' % task_id)
    except Exception as exc:
        log('取消任务失败（可忽略）：%s' % exc)


# --------------------------------------------------------------------------- #
# 结果格式化
# --------------------------------------------------------------------------- #
def _absolutize(url):
    """后端 media 字段是站内相对路径（/api/media/...），外部客户端需要带域名的直链。"""
    if not url:
        return ''
    url = str(url).strip()
    if url.startswith(('http://', 'https://')):
        return url
    origin = CONFIG.base_url
    if origin.endswith('/api'):
        origin = origin[:-4]
    return origin + (url if url.startswith('/') else '/' + url)


def _fence(code):
    fence = '```'
    if '```' in code:
        fence = '~~~'
    return '%spython\n%s\n%s' % (fence, code.strip(), fence)


def _format_result(result, task_id=None, cached=False, quota=None):
    """把 /api/task 的 result 字段整理成一段给模型/用户看的 Markdown。"""
    video_url = _absolutize(result.get('animation_url') or result.get('video_url') or '')
    thumbnail_url = _absolutize(result.get('thumbnail_url') or '')
    tags = result.get('tags') or []
    code = result.get('manim_code') or ''
    prompt = result.get('prompt') or ''
    record_id = result.get('record_id')

    lines = []
    lines.append('弦图已生成%s。' % ('（复用历史渲染结果）' if cached else ''))
    if prompt:
        lines.append('')
        lines.append('描述：%s' % prompt)
    lines.append('')
    if video_url:
        lines.append('视频：%s' % video_url)
    if thumbnail_url:
        lines.append('封面：%s' % thumbnail_url)
    if isinstance(tags, list) and tags:
        lines.append('标签：%s' % '、'.join([str(t) for t in tags if t]))
    if record_id:
        lines.append('作品 ID：%s' % record_id)
    if task_id:
        lines.append('任务 ID：%s' % task_id)
    quota_text = _quota_line(quota)
    if quota_text:
        lines.append(quota_text)
    lines.append('')
    lines.append('说明：视频直链可直接播放或下载；高清档位与站内保存需要站点会员。')

    if code:
        lines.append('')
        lines.append('Manim 代码：')
        lines.append('')
        if len(code) > CODE_INLINE_LIMIT:
            lines.append(_fence(code[:CODE_INLINE_LIMIT]))
            lines.append('')
            lines.append('（代码较长，已截断到 %d 字符）' % CODE_INLINE_LIMIT)
        else:
            lines.append(_fence(code))

    structured = {
        'task_id': task_id or result.get('task_id') or '',
        'record_id': record_id,
        'prompt': prompt,
        'video_url': video_url,
        'thumbnail_url': thumbnail_url,
        'tags': tags if isinstance(tags, list) else [],
        'cached': bool(cached),
        'manim_code': code,
    }
    if quota:
        structured['quota'] = quota
    return '\n'.join(lines).strip(), structured


def _format_pending(task_id, status='pending', quota=None, waited=0):
    text = [
        '渲染任务已提交，后台继续生成中。',
        '',
        '任务 ID：%s' % task_id,
        '当前状态：%s' % (_STATUS_LABEL.get(status, status)),
        '已等待：%d 秒' % int(waited),
        '',
        '稍后用 sanhang_task 查询（action=status 或 action=wait）即可拿到视频地址。',
    ]
    quota_text = _quota_line(quota)
    if quota_text:
        text.append(quota_text)
    structured = {'task_id': task_id, 'status': status, 'pending': True}
    if quota:
        structured['quota'] = quota
    return '\n'.join(text), structured


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def _tool_generate(args, ctx):
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return _error('参数 prompt 必填，且必须是非空字符串。')
    prompt = prompt.strip()
    if CONFIG.mode == 'local':
        return _tool_generate_local(args, ctx)
    if CONFIG.mode == 'none':
        return _error(_CONFIG_ERROR)
    if len(prompt) > 1000:
        return _error('参数 prompt 过长（后端上限 1000 字符），请精简到一句可执行的动画描述。')

    wait = args.get('wait', True)
    if isinstance(wait, str):
        wait = wait.strip().lower() not in ('false', '0', 'no', 'off')
    wait = bool(wait)

    max_wait = args.get('max_wait_seconds')
    if max_wait is None:
        max_wait = CONFIG.wait_seconds
    try:
        max_wait = int(float(max_wait))
    except (TypeError, ValueError):
        max_wait = CONFIG.wait_seconds
    max_wait = max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, max_wait))

    payload = {'prompt': prompt,
               'user_identifier': args.get('user_identifier') or CONFIG.user_identifier}
    motto = (args.get('motto') or '').strip()
    logo_url = (args.get('logo_url') or '').strip()
    if motto:
        payload['motto'] = motto
    if logo_url:
        payload['logo_url'] = logo_url

    response = _http_json('POST', '/generate', payload)
    task_id = response.get('task_id') or ''
    quota = response.get('quota')

    ready = response.get('result') if response.get('status') == 'completed' else None
    if ready:
        text, structured = _format_result(ready, task_id=task_id,
                                          cached=bool(response.get('cached')), quota=quota)
        return text, structured, False

    if not task_id:
        return _error('后端未返回 task_id，无法追踪渲染进度。原始响应：%s' % json.dumps(
            response, ensure_ascii=False)[:300])

    if not wait:
        text, structured = _format_pending(task_id, response.get('status') or 'pending', quota)
        return text, structured, False

    _notify_progress(ctx, 1, 100, '任务已提交，开始轮询渲染进度')
    started = time.time()
    data = _poll_task(task_id, max_wait, ctx)
    if data is None:
        waited = time.time() - started
        text, structured = _format_pending(task_id, 'processing', quota, waited)
        return text, structured, False

    text, structured = _format_result(data.get('result') or {}, task_id=task_id, quota=quota)
    return text, structured, False


def _tool_task(args, ctx):
    if CONFIG.mode == 'none':
        return _error(_CONFIG_ERROR)
    if CONFIG.mode == 'local':
        return _error(
            '本地模式下渲染是同步完成的，没有后台任务，sanhang_task 不可用。'
            '直接调用 sanhang_render 即可得到本地视频文件。')
    task_id = (args.get('task_id') or '').strip()
    if not task_id:
        return _error('参数 task_id 必填。')
    action = (args.get('action') or 'status').strip().lower()

    if action == 'cancel':
        data = _http_json('POST', '/task/%s/cancel' % urllib.parse.quote(task_id))
        status = data.get('status') or 'cancelled'
        text = '已请求取消任务 %s，当前状态：%s。' % (task_id, status)
        return text, {'task_id': task_id, 'status': status, 'cancelled': True}, False

    if action == 'wait':
        max_wait = args.get('max_wait_seconds')
        try:
            max_wait = int(float(max_wait if max_wait is not None else CONFIG.wait_seconds))
        except (TypeError, ValueError):
            max_wait = CONFIG.wait_seconds
        max_wait = max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, max_wait))
        started = time.time()
        data = _poll_task(task_id, max_wait, ctx)
        if data is None:
            text, structured = _format_pending(task_id, 'processing', None, time.time() - started)
            return text, structured, False
        text, structured = _format_result(data.get('result') or {}, task_id=task_id)
        return text, structured, False

    data = _http_json('GET', '/task/%s' % urllib.parse.quote(task_id))
    status = str(data.get('status') or 'unknown')
    if status == 'completed':
        text, structured = _format_result(data.get('result') or {}, task_id=task_id)
        return text, structured, False
    if status == 'failed':
        return _error('任务 %s 渲染失败：%s' % (task_id, data.get('error') or '后端未给出原因'),
                      extra={'task_id': task_id, 'status': status})

    lines = [
        '任务 %s 当前状态：%s' % (task_id, _STATUS_LABEL.get(status, status)),
        '进度：%s%%' % (data.get('progress') or 0),
        '',
        '用 sanhang_task（action=wait）等待它完成。',
    ]
    return '\n'.join(lines), {'task_id': task_id, 'status': status,
                              'progress': data.get('progress') or 0, 'pending': True}, False


def _tool_gallery(args, ctx):
    if CONFIG.mode == 'none':
        return _error(_CONFIG_ERROR)
    if CONFIG.mode == 'local':
        return _error(
            '本地模式不连接社区画廊，不会访问任何服务器。'
            '如需浏览公开作品，请配置 SANHANG_BASE_URL 指向一个后端。')
    params = {
        'page': _clamp_int(args.get('page'), 1, 1, 100),
        'page_size': _clamp_int(args.get('page_size'), 10, 1, 50),
    }
    q = (args.get('q') or '').strip()
    tags = args.get('tags') or ''
    category = (args.get('category') or '').strip()
    sort_by = (args.get('sort_by') or 'created_at').strip().lower()
    if q:
        params['q'] = q
    if tags:
        params['tags'] = tags if isinstance(tags, str) else ','.join([str(t) for t in tags])
    if category:
        params['category'] = category
    if sort_by in ('created_at', 'popular', 'likes'):
        params['sort_by'] = sort_by

    query = urllib.parse.urlencode(params)
    data = _http_json('GET', '/history/public?' + query)
    items = data.get('data') or []

    if not items:
        return '弦图墙没有匹配的公开作品。可以换关键词，或直接调用 sanhang_render 生成一个新作品。', \
               {'total': data.get('total', 0), 'items': []}, False

    lines = ['弦图墙公开作品（第 %s 页，共 %s 条）：' % (data.get('page'), data.get('total'))]
    cards = []
    for item in items:
        title = item.get('title') or item.get('prompt') or '（无标题）'
        video_url = _absolutize(item.get('video_url'))
        lines.append('')
        lines.append('- 《%s》' % title)
        tags = item.get('tags') or []
        if tags:
            lines.append('  标签：%s' % '、'.join([str(t) for t in tags[:8] if t]))
        if video_url:
            lines.append('  视频：%s' % video_url)
        cards.append({
            'task_id': item.get('task_id'),
            'title': title,
            'prompt': item.get('prompt'),
            'tags': tags,
            'video_url': video_url,
            'thumbnail_url': _absolutize(item.get('thumbnail_url')),
        })

    structured = {'page': data.get('page'), 'total': data.get('total'), 'items': cards}
    return '\n'.join(lines), structured, False


def _clamp_int(value, default, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _error(message, extra=None):
    structured = {'error': message}
    if extra:
        structured.update(extra)
    return message, structured, True


# --------------------------------------------------------------------------- #
# 本地模式：用户自备 DeepSeek Key + 本机 Manim 渲染，全程不访问作者服务器
# --------------------------------------------------------------------------- #
_SYSTEM_GEN = (
    '你是一个 Manim 社区版代码生成器。'
    '只输出一个 Python 文件内容：定义一个名为 MainScene 的 Scene 子类，'
    '可被 `manim -qm scene.py MainScene` 直接渲染通过。'
    '不要任何解释，不要使用 markdown 代码块包裹，直接输出纯 Python 代码。'
    '使用标准 Manim 社区版 API（如 axes.plot、self.play、MathTex 等），'
    '不要引用外部资源或网络下载。'
)

_SYSTEM_FIX = (
    '下面这段 Manim 代码在本地渲染时失败，附上了渲染错误。'
    '请修正代码并只输出修正后的完整 Python 文件内容（定义 MainScene，'
    '不要解释、不要 markdown 包裹）。'
)


def _strip_code_fence(text):
    """去掉 DeepSeek 偶尔包在外的 ```python ... ``` 围栏，返回纯代码。"""
    text = (text or '').strip()
    if text.startswith('```'):
        lines = text.split('\n')
        if lines and lines[0].strip().startswith('```'):
            lines = lines[1:]
        while lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)
    return text.strip()


def _deepseek_chat(messages, api_key, model, timeout=90):
    """用标准库 urllib 调 DeepSeek chat completions（用户自己的 key）。"""
    url = _getenv('DEEPSEEK_BASE_URL') or DEEPSEEK_DEFAULT_URL
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % api_key,
        'User-Agent': '%s/%s' % (SERVER_NAME, SERVER_VERSION),
    }
    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.7,
        'stream': False,
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = _safe_read(exc)
        raise BackendError('DeepSeek 返回错误 %s：%s' % (exc.code, raw[:200]))
    except Exception as exc:
        raise BackendError('调用 DeepSeek 失败：%s' % exc)
    raw = response.read().decode('utf-8', 'replace')
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        raise BackendError('DeepSeek 返回了非 JSON 内容（前 200 字）：%s' % raw[:200])
    choices = parsed.get('choices') or []
    if not choices:
        reason = ''
        err = parsed.get('error')
        if isinstance(err, dict):
            reason = err.get('message', '')
        suffix = ('：' + reason) if reason else '（可能余额不足或触发内容过滤）'
        raise BackendError('DeepSeek 未返回内容%s' % suffix)
    return choices[0].get('message', {}).get('content', '') or ''


def _deepseek_generate_code(prompt, api_key, model):
    messages = [
        {'role': 'system', 'content': _SYSTEM_GEN},
        {'role': 'user', 'content': prompt},
    ]
    return _strip_code_fence(_deepseek_chat(messages, api_key, model))


def _deepseek_fix_code(code, error_text, api_key, model):
    messages = [
        {'role': 'system', 'content': _SYSTEM_FIX},
        {'role': 'user', 'content': '原始代码：\n```python\n%s\n```\n\n渲染错误：\n%s'
         % (code, error_text[:3000])},
    ]
    return _strip_code_fence(_deepseek_chat(messages, api_key, model))


def _find_mp4(root):
    if not root or not os.path.isdir(root):
        return None
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith('.mp4'):
                return os.path.join(dirpath, name)
    return None


def _run_local_manim(code, ctx):
    """写临时文件并用本机 manim 渲染。成功返回 (视频路径, None)，失败 (None, 错误)。"""
    manim = shutil.which('manim') or shutil.which('manimce')
    if not manim:
        return None, '本地未检测到 manim。请先安装：pip install manim，并确认 manim 在 PATH 中。'
    if not shutil.which('ffmpeg'):
        return None, '本地未检测到 ffmpeg，Manim 渲染需要它（通常随 manim 依赖安装，或单独装）。'

    work = tempfile.mkdtemp(prefix='sanhang_')
    py_path = os.path.join(work, 'scene.py')
    try:
        with open(py_path, 'w') as handle:
            handle.write(code)
    except Exception as exc:
        return None, '写入临时文件失败：%s' % exc

    quality = (_getenv('LOCAL_QUALITY') or 'm').strip().lower()
    if quality not in ('l', 'm', 'h', 'k'):
        quality = 'm'
    timeout = _env_float('LOCAL_TIMEOUT', 600, 30, 1800)
    cmd = [manim, '-q' + quality, '--disable_caching', py_path, 'MainScene']
    try:
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, '本地 Manim 渲染超时（默认 600 秒，可用 SANHANG_LOCAL_TIMEOUT 调大）。'
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or '')[-1800:]
    video = _find_mp4(os.path.join(work, 'media', 'videos'))
    if not video:
        return None, 'Manim 渲染结束但未找到 mp4 输出（检查 Manim 是否成功生成 MainScene）。'
    return video, None


def _tool_generate_local(args, ctx):
    prompt = args.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        return _error('参数 prompt 必填，且必须是非空字符串。')
    prompt = prompt.strip()
    if len(prompt) > 1000:
        return _error('参数 prompt 过长（本地模式同样建议控制在 1000 字符内），请精简描述。')

    api_key = _getenv('DEEPSEEK_API_KEY') or _getenv('API_KEY')
    if not api_key:
        return _error(
            '本地模式需要你自己的 DeepSeek API Key（全程不消耗作者资源）。\n'
            '请设置环境变量：SANHANG_DEEPSEEK_API_KEY=sk-你的key\n'
            '再启动本服务。')
    model = _getenv('LOCAL_MODEL') or 'deepseek-chat'

    _notify_progress(ctx, 5, 100, '正在调用 DeepSeek 生成 Manim 代码')
    try:
        code = _deepseek_generate_code(prompt, api_key, model)
    except BackendError as exc:
        return _error(str(exc))
    if not code:
        return _error('DeepSeek 未返回可执行代码（可能触发内容过滤或余额不足）。')

    last_err = None
    for attempt in (1, 2):
        _notify_progress(ctx, 30 + attempt * 20, 100, '本地 Manim 渲染中（第 %d 次）' % attempt)
        video, err = _run_local_manim(code, ctx)
        if video:
            path = os.path.abspath(video)
            url = 'file://' + path
            text = (
                '本地渲染完成（使用你自己的 DeepSeek Key + 本机 Manim，未访问任何外部服务器）。\n\n'
                '描述：%s\n\n'
                '视频文件：%s\n\n'
                'Manim 代码：\n\n%s'
            ) % (prompt, url, _fence(code))
            structured = {
                'mode': 'local',
                'video_path': path,
                'video_url': url,
                'manim_code': code,
                'prompt': prompt,
            }
            return text, structured, False
        last_err = err
        if attempt == 1:
            _notify_progress(ctx, 70, 100, '首次渲染失败，正在请求 DeepSeek 修正代码')
            try:
                fixed = _deepseek_fix_code(code, last_err or '未知错误', api_key, model)
            except BackendError as exc:
                return _error('渲染失败且无法请求修正：%s\n\n原始错误：\n%s'
                              % (exc, (last_err or '')[:800]))
            if not fixed:
                break
            code = fixed

    return _error(
        '本地 Manim 渲染失败（已尝试 AI 纠错一次）。\n\n'
        '错误信息：\n%s\n\n代码：\n%s'
        % ((last_err or '未知')[:1800], _fence(code)))


# --------------------------------------------------------------------------- #
# 工具清单
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        'name': 'sanhang_render',
        'title': '生成弦图',
        'description': (
            '把一句自然语言数学描述渲染成 Manim 动画视频，返回可直接播放的 mp4 地址与可复用的 '
            'Manim 代码。适用于函数图像、几何变换、微积分（极限/导数/积分/级数）、线性代数'
            '（矩阵/向量/特征值）、概率分布、复数与复平面、物理运动等可视化需求。'
            '默认阻塞等待渲染完成（约 30~90 秒）；把 wait 设为 false 可立即拿到 task_id 稍后再查。'
            '同一描述命中缓存时会秒回历史结果。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'prompt': {
                    'type': 'string',
                    'description': '中文或英文的动画描述，越具体越好。例如“画出 y=sin(x) 在 0 到 2π 上的图像，'
                                   '并用一个红点演示它随 x 增大的运动”。上限 1000 字符。',
                    'minLength': 1,
                    'maxLength': 1000,
                },
                'wait': {
                    'type': 'boolean',
                    'description': '是否阻塞等待渲染完成。默认 true。',
                    'default': True,
                },
                'max_wait_seconds': {
                    'type': 'integer',
                    'description': '阻塞等待的上限秒数，默认 240，范围 10~900。超时不报错，会返回 task_id 供后续查询。',
                    'default': DEFAULT_WAIT_SECONDS,
                    'minimum': MIN_WAIT_SECONDS,
                    'maximum': MAX_WAIT_SECONDS,
                },
                'user_identifier': {
                    'type': 'string',
                    'description': '可选。用于配额统计与作品归属的标识。默认用本机自动生成的标识（首次运行写入 ~/.sanhang-mcp/identifier），也可用环境变量 SANHANG_USER_IDENTIFIER 覆盖。',
                },
                'motto': {
                    'type': 'string',
                    'description': '可选。烧录在视频角落的署名短句（需会员权限，普通调用会被忽略）。',
                },
                'logo_url': {
                    'type': 'string',
                    'description': '可选。烧录在视频角落的 logo 图片地址（需会员权限）。',
                },
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
        'annotations': {'readOnlyHint': False, 'openWorldHint': True},
    },
    {
        'name': 'sanhang_task',
        'title': '查询或取消渲染任务',
        'description': (
            '查询一个已提交渲染任务的状态；也可以阻塞等待它完成，或请求取消。'
            '配合 sanhang_render 的 wait=false 使用：先提交拿到 task_id，再在这里等结果，避免客户端超时。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'task_id': {'type': 'string', 'description': 'sanhang_render 返回的任务 ID（UUID）。'},
                'action': {
                    'type': 'string',
                    'enum': ['status', 'wait', 'cancel'],
                    'description': 'status 查询一次；wait 轮询到完成；cancel 请求取消。默认 status。',
                    'default': 'status',
                },
                'max_wait_seconds': {
                    'type': 'integer',
                    'description': 'action=wait 时的等待上限秒数，默认 240，范围 10~900。',
                    'default': DEFAULT_WAIT_SECONDS,
                    'minimum': MIN_WAIT_SECONDS,
                    'maximum': MAX_WAIT_SECONDS,
                },
            },
            'required': ['task_id'],
            'additionalProperties': False,
        },
        'annotations': {'readOnlyHint': False, 'openWorldHint': True},
    },
    {
        'name': 'sanhang_gallery',
        'title': '浏览弦图墙',
        'description': (
            '浏览已公开的弦图作品（社区画廊）。可用来了解这套服务能画出什么、找参考风格，'
            '或给用户展示同类作品。返回标题、标签与视频直链。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'q': {'type': 'string', 'description': '关键词，匹配标题与描述。'},
                'tags': {'type': 'string', 'description': '标签过滤，逗号分隔，如“三角函数,正弦”。'},
                'category': {'type': 'string',
                             'description': '二级学科，如 数学/物理学/计算机科学 等，留空表示不过滤。'},
                'sort_by': {'type': 'string', 'enum': ['created_at', 'popular', 'likes'],
                            'description': '排序：最新 / 最热 / 最多赞。默认最新。', 'default': 'created_at'},
                'page': {'type': 'integer', 'description': '页码，默认 1。', 'default': 1, 'minimum': 1},
                'page_size': {'type': 'integer', 'description': '每页条数，默认 10，最多 50。',
                              'default': 10, 'minimum': 1, 'maximum': 50},
            },
            'additionalProperties': False,
        },
        'annotations': {'readOnlyHint': True, 'openWorldHint': True},
    },
]

TOOL_HANDLERS = {
    'sanhang_render': _tool_generate,
    'sanhang_task': _tool_task,
    'sanhang_gallery': _tool_gallery,
}


# --------------------------------------------------------------------------- #
# JSON-RPC 传输层
# --------------------------------------------------------------------------- #
def _write_message(obj):
    """写一条 MCP 消息。JSON-RPC over stdio 要求一行一条、不得含内嵌换行。"""
    payload = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    payload = payload.replace('\r', ' ').replace('\n', ' ')
    with _WRITE_LOCK:
        try:
            sys.stdout.write(payload + '\n')
            sys.stdout.flush()
        except Exception as exc:
            log('写 stdout 失败：%s' % exc)


def _respond(msg_id, result):
    _write_message({'jsonrpc': '2.0', 'id': msg_id, 'result': result})


def _respond_error(msg_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    _write_message({'jsonrpc': '2.0', 'id': msg_id, 'error': error})


def _notify_progress(ctx, progress, total, message):
    token = (ctx or {}).get('progress_token')
    if token is None:
        return
    try:
        _notify('notifications/progress', {
            'progressToken': token,
            'progress': progress,
            'total': total,
            'message': message,
        })
    except Exception:
        pass


def _notify(method, params):
    _write_message({'jsonrpc': '2.0', 'method': method, 'params': params})


def _negotiate_protocol(requested):
    if requested in PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL


INSTRUCTIONS = (
    'sanhang_render 把数学描述渲染成 Manim 动画视频。\n'
    '运行模式由环境变量决定：SANHANG_LOCAL=1 用你自己的 DeepSeek Key + 本机 Manim 本地出片'
    '（全程不访问作者服务器）；SANHANG_BASE_URL=你的后端 连接后端渲染；两者都不设则提示如何配置。\n'
    '后端模式下，渲染通常 30~90 秒，若客户端有工具超时限制，把 wait 设为 false，再用 sanhang_task 轮询。\n'
    '本地模式下渲染同步完成，直接返回本地视频文件路径。\n'
    '描述越具体（函数式、区间、要演示的动作、配色）成片越贴合预期。'
)


def _handle_request(msg_id, method, params):
    try:
        if method == 'initialize':
            requested = (params or {}).get('protocolVersion')
            _respond(msg_id, {
                'protocolVersion': _negotiate_protocol(requested),
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {
                    'name': SERVER_NAME,
                    'title': '三行弦图 sanhang-mcp',
                    'version': SERVER_VERSION,
                },
                'instructions': INSTRUCTIONS,
            })
            return

        if method == 'ping':
            _respond(msg_id, {})
            return

        if method == 'tools/list':
            _respond(msg_id, {'tools': TOOLS})
            return

        if method == 'tools/call':
            _handle_tool_call(msg_id, params or {})
            return

        if method in ('logging/setLevel', 'resources/list', 'prompts/list'):
            # 未实现的能力，给空结果而不是报错，避免客户端启动阶段硬失败
            key = 'resources' if method == 'resources/list' else 'prompts'
            if method == 'logging/setLevel':
                _respond(msg_id, {})
            else:
                _respond(msg_id, {key: []})
            return

        _respond_error(msg_id, -32601, 'Method not found: %s' % method)
    except Exception as exc:
        log('处理 %s 失败：%s' % (method, exc))
        _respond_error(msg_id, -32603, 'Internal error', {'detail': str(exc)})


def _handle_tool_call(msg_id, params):
    name = params.get('name')
    arguments = params.get('arguments') or {}
    meta = params.get('_meta') or {}
    progress_token = meta.get('progressToken')

    if not isinstance(arguments, dict):
        _respond_error(msg_id, -32602, 'Invalid params: arguments must be an object')
        return
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        _respond_error(msg_id, -32602, 'Unknown tool: %s' % name,
                       {'available': sorted(TOOL_HANDLERS.keys())})
        return

    cancel_event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_EVENTS[str(msg_id)] = cancel_event

    ctx = {'request_id': msg_id, 'cancel': cancel_event, 'progress_token': progress_token}
    try:
        text, structured, is_error = handler(arguments, ctx)
        result = {
            'content': [{'type': 'text', 'text': text}],
            'structuredContent': structured,
            'isError': bool(is_error),
        }
        _respond(msg_id, result)
    except TaskCancelled as exc:
        _respond(msg_id, {
            'content': [{'type': 'text', 'text': '已取消：%s' % exc}],
            'structuredContent': {'cancelled': True, 'message': str(exc)},
            'isError': True,
        })
    except BackendError as exc:
        structured = {'error': exc.message}
        if exc.code:
            structured['code'] = exc.code
        if exc.status:
            structured['status'] = exc.status
        _respond(msg_id, {
            'content': [{'type': 'text', 'text': exc.message}],
            'structuredContent': structured,
            'isError': True,
        })
    except Exception as exc:
        log('工具 %s 执行异常：%s' % (name, exc))
        _respond(msg_id, {
            'content': [{'type': 'text', 'text': '执行失败：%s' % exc}],
            'structuredContent': {'error': str(exc)},
            'isError': True,
        })
    finally:
        with _CANCEL_LOCK:
            _CANCEL_EVENTS.pop(str(msg_id), None)


def _handle_notification(method, params):
    if method == 'notifications/cancelled':
        request_id = (params or {}).get('requestId')
        with _CANCEL_LOCK:
            event = _CANCEL_EVENTS.get(str(request_id))
        if event is not None:
            event.set()
            log('收到取消请求 requestId=%s' % request_id)
        return
    if method in ('notifications/initialized', 'notifications/roots/list_changed'):
        return
    log('忽略未知通知：%s' % method)


def _dispatch(message):
    """分发一条已解析的消息；请求走线程，通知就地处理。"""
    if isinstance(message, list):
        if not message:
            _respond_error(None, -32600, 'Invalid Request: empty batch')
            return
        for item in message:
            _dispatch(item)
        return

    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        _respond_error(None, -32600, 'Invalid Request: jsonrpc must be "2.0"')
        return

    method = message.get('method')
    has_id = 'id' in message
    if not method:
        if not has_id:
            return
        _respond_error(message.get('id'), -32600, 'Invalid Request: missing method')
        return

    if not has_id:
        _handle_notification(method, message.get('params') or {})
        return

    worker = threading.Thread(
        target=_handle_request,
        args=(message.get('id'), method, message.get('params') or {}),
        name='mcp-req-%s' % message.get('id'),
    )
    worker.daemon = True
    worker.start()


def _prepare_streams():
    """保证 stdin/stdout 用 UTF-8，否则中文 prompt 在 Windows 上会炸。"""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass


def serve():
    _prepare_streams()
    if CONFIG.mode == 'backend':
        loc = '后端 ' + CONFIG.base_url
    elif CONFIG.mode == 'local':
        loc = '本地模式（自备 DeepSeek Key + 本地 Manim）'
    else:
        loc = '未配置（调用工具会提示如何配置 SanHANG_LOCAL 或 SANHANG_BASE_URL）'
    log('%s %s 启动，模式 %s（%s）' % (SERVER_NAME, SERVER_VERSION, CONFIG.mode, loc))
    while True:
        line = sys.stdin.readline()
        if not line:
            log('stdin 关闭，退出')
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _respond_error(None, -32700, 'Parse error', {'detail': str(exc)})
            continue
        _dispatch(message)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def _print(text):
    sys.stdout.write(text + '\n')
    sys.stdout.flush()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if '--version' in argv or '-V' in argv:
        _print('%s %s' % (SERVER_NAME, SERVER_VERSION))
        return 0
    if '--help' in argv or '-h' in argv:
        _print(__doc__.strip())
        return 0
    if '--whoami' in argv:
        from_env = bool(_getenv('USER_IDENTIFIER'))
        _print('mode: %s' % CONFIG.mode)
        _print('identifier: %s' % CONFIG.user_identifier)
        _print('source: %s' % ('环境变量' if from_env else '本机自动生成'))
        if not from_env:
            _print('file: %s' % os.path.join(_state_dir(), 'identifier'))
        return 0
    if '--check' in argv:
        if CONFIG.mode == 'local':
            _print('mode: local（自备 DeepSeek Key + 本地 Manim）')
            api_key = _getenv('DEEPSEEK_API_KEY') or _getenv('API_KEY')
            _print('DeepSeek API Key: %s' % ('已配置' if api_key else '未配置（需 SANHANG_DEEPSEEK_API_KEY）'))
            manim = shutil.which('manim') or shutil.which('manimce')
            ffmpeg = shutil.which('ffmpeg')
            _print('manim: %s' % (manim or '未检测到'))
            _print('ffmpeg: %s' % (ffmpeg or '未检测到'))
            if api_key and manim and ffmpeg:
                _print('OK 本地渲染链路就绪')
                return 0
            _print('FAIL 本地渲染依赖不全，请补齐上述缺少的项')
            return 1
        if CONFIG.mode == 'none':
            _print('mode: none（未配置后端或本地模式）')
            _print(_CONFIG_ERROR)
            return 1
        try:
            health = _http_json('GET', '/health')
            _print('OK %s -> %s' % (CONFIG.base_url, json.dumps(health, ensure_ascii=False)[:300]))
            _print('identifier: %s' % CONFIG.user_identifier)
            return 0
        except BackendError as exc:
            _print('FAIL %s -> %s' % (CONFIG.base_url, exc.message))
            return 1
    return serve()


if __name__ == '__main__':
    sys.exit(main())
