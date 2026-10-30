#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sanhang-mcp 协议自测客户端。

它是一个最小的 MCP 客户端实现：拉起 sanhang_mcp.py 子进程，按 MCP 2025-06-18 的
生命周期发消息、收响应，并对协议细节做断言。

用法：
    python3 test_client.py                # 只跑协议自测，不产生真实渲染（快，约 2 秒）
    python3 test_client.py --generate "画出 y=sin(x) 在 0 到 2π 的图像"   # 真跑一次渲染
    python3 test_client.py --gallery      # 调一次弦图墙，验证只读工具
    python3 test_client.py --server ./sanhang_mcp.py --timeout 300
"""

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER = os.path.join(HERE, 'sanhang_mcp.py')

PASS = 0
FAIL = 0


def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print('  PASS  %s' % name)
    else:
        FAIL += 1
        print('  FAIL  %s %s' % (name, detail))


class MCPClient(object):
    """极简 stdio MCP 客户端：一行一条 JSON-RPC，异步收消息按 id 匹配。"""

    def __init__(self, server_path, env=None):
        full_env = dict(os.environ)
        full_env.setdefault('PYTHONUNBUFFERED', '1')
        if env:
            full_env.update(env)
        self.proc = subprocess.Popen(
            [sys.executable, '-u', server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )
        self._next_id = 0
        self._responses = {}
        self._notifications = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop)
        self._reader.daemon = True
        self._reader.start()

    def _read_loop(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return
            line = line.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                print('  [警告] 服务端输出非 JSON：%s' % line[:200])
                continue
            with self._lock:
                if 'id' in message and ('result' in message or 'error' in message):
                    self._responses[str(message['id'])] = message
                else:
                    self._notifications.append(message)

    def send(self, method, params=None, notification=False, request_id=None):
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        if not notification:
            if request_id is None:
                self._next_id += 1
                request_id = self._next_id
            message['id'] = request_id
            target = str(request_id)
        else:
            target = None
        raw = json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n'
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()
        return target

    def wait(self, request_id, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if str(request_id) in self._responses:
                    return self._responses.pop(str(request_id))
            if self.proc.poll() is not None:
                raise RuntimeError('服务端进程已退出，code=%s，stderr=%s'
                                   % (self.proc.returncode, self.drain_stderr()))
            time.sleep(0.02)
        raise RuntimeError('等待响应 id=%s 超时（%s 秒）' % (request_id, timeout))

    def request(self, method, params=None, timeout=30.0):
        return self.wait(self.send(method, params), timeout)

    def call_tool(self, name, arguments, timeout=300.0):
        return self.request('tools/call', {'name': name, 'arguments': arguments}, timeout)

    def notifications(self):
        with self._lock:
            return list(self._notifications)

    def drain_stderr(self):
        try:
            return self.proc.stderr.read().decode('utf-8', 'replace')[:2000]
        except Exception:
            return ''

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def run_protocol_tests(client):
    print('\n[1] initialize 握手')
    resp = client.request('initialize', {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'sanhang-mcp-test-client', 'version': '0.1.0'},
    })
    result = resp.get('result') or {}
    check('返回 result 而非 error', 'result' in resp, resp.get('error'))
    check('protocolVersion 协商为 2025-06-18', result.get('protocolVersion') == '2025-06-18',
          result.get('protocolVersion'))
    check('serverInfo.name == manim-mcp',
          (result.get('serverInfo') or {}).get('name') == 'sanhang-mcp',
          result.get('serverInfo'))
    check('声明 tools 能力', 'tools' in (result.get('capabilities') or {}), result.get('capabilities'))
    check('带 instructions', bool(result.get('instructions')))

    print('\n[2] notifications/initialized')
    client.send('notifications/initialized', notification=True)
    time.sleep(0.1)
    check('通知不产生响应', client.proc.poll() is None)

    print('\n[3] 未知协议版本回落到服务端最新版')
    resp = client.request('initialize', {'protocolVersion': '1999-01-01',
                                         'capabilities': {},
                                         'clientInfo': {'name': 't', 'version': '0'}})
    check('回落为 2025-06-18', (resp.get('result') or {}).get('protocolVersion') == '2025-06-18',
          resp.get('result'))

    print('\n[4] ping')
    resp = client.request('ping')
    check('ping 返回空 result', resp.get('result') == {}, resp)

    print('\n[5] tools/list')
    resp = client.request('tools/list')
    tools = (resp.get('result') or {}).get('tools') or []
    names = [t.get('name') for t in tools]
    check('返回 3 个工具', len(tools) == 3, names)
    check('包含 sanhang_render', 'sanhang_render' in names, names)
    check('包含 sanhang_task', 'sanhang_task' in names, names)
    check('包含 sanhang_gallery', 'sanhang_gallery' in names, names)
    primary = [t for t in tools if t.get('name') == 'sanhang_render']
    schema = (primary[0] if primary else {}).get('inputSchema') or {}
    check('prompt 为必填', 'prompt' in (schema.get('required') or []), schema.get('required'))
    check('prompt 类型为 string',
          ((schema.get('properties') or {}).get('prompt') or {}).get('type') == 'string')

    print('\n[6] 未知方法 -> -32601')
    resp = client.request('tools/nonexistent')
    check('方法不存在报 -32601', (resp.get('error') or {}).get('code') == -32601, resp)

    print('\n[7] 未知工具 -> -32602')
    resp = client.call_tool('no-such-tool', {})
    check('未知工具报 -32602', (resp.get('error') or {}).get('code') == -32602, resp)

    print('\n[8] 参数非法 -> isError')
    resp = client.call_tool('sanhang_render', {})
    result = resp.get('result') or {}
    check('缺 prompt 返回 isError=true', result.get('isError') is True, resp)
    check('给出可读错误文案', bool((result.get('content') or [{}])[0].get('text')), result)

    print('\n[9] 非法 JSON -> -32700')
    client.proc.stdin.write(b'this is not json\n')
    client.proc.stdin.flush()
    deadline = time.time() + 5
    got = False
    while time.time() < deadline and not got:
        with client._lock:
            for key, message in list(client._responses.items()):
                if (message.get('error') or {}).get('code') == -32700:
                    got = True
                    client._responses.pop(key, None)
                    break
        time.sleep(0.02)
    check('解析错误报 -32700', got)

    print('\n[10] 输出纯净性（stdout 每行都是合法 JSON）')
    check('服务端仍存活', client.proc.poll() is None)


def run_generate(client, prompt, timeout):
    print('\n[真实渲染] tools/call manim-mcp')
    started = time.time()
    resp = client.call_tool('sanhang_render', {'prompt': prompt, 'wait': True,
                                          'max_wait_seconds': int(min(timeout, 600))},
                            timeout=timeout + 30)
    elapsed = time.time() - started
    result = resp.get('result') or {}
    if resp.get('error'):
        check('渲染调用未报协议错误', False, resp.get('error'))
        return 1
    text = (result.get('content') or [{}])[0].get('text') or ''
    structured = result.get('structuredContent') or {}
    print('  ---- 返回文本（前 600 字）----')
    print('\n'.join(['  ' + line for line in text[:600].splitlines()]))
    print('  ------------------------------')
    check('耗时 %.1fs' % elapsed, elapsed > 0)
    if result.get('isError'):
        check('渲染成功（非 isError）', False, text[:300])
        return 1
    check('拿到视频地址', bool(structured.get('video_url')), structured)
    check('拿到 Manim 代码', bool(structured.get('manim_code')), list(structured.keys()))
    check('拿到 task_id', bool(structured.get('task_id')), structured)
    return 0


def run_gallery(client):
    print('\n[只读] tools/call sanhang_gallery')
    resp = client.call_tool('sanhang_gallery', {'page_size': 3, 'sort_by': 'popular'}, timeout=60)
    result = resp.get('result') or {}
    text = (result.get('content') or [{}])[0].get('text') or ''
    print('  ---- 返回文本 ----')
    print('\n'.join(['  ' + line for line in text[:800].splitlines()]))
    check('画廊调用成功', result.get('isError') is False, text[:300])
    return 0 if result.get('isError') is False else 1


def main():
    argv = sys.argv[1:]
    server = DEFAULT_SERVER
    timeout = 300.0
    prompt = None
    gallery = False

    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '--server':
            index += 1
            server = argv[index]
        elif token == '--timeout':
            index += 1
            timeout = float(argv[index])
        elif token == '--generate':
            index += 1
            prompt = argv[index]
        elif token == '--gallery':
            gallery = True
        elif token in ('-h', '--help'):
            print(__doc__.strip())
            return 0
        index += 1

    if not os.path.exists(server):
        print('找不到服务端脚本：%s' % server)
        return 2

    print('启动服务端：%s' % server)
    client = MCPClient(server)
    exit_code = 0
    try:
        run_protocol_tests(client)
        if prompt:
            exit_code = run_generate(client, prompt, timeout)
        if gallery:
            exit_code = max(exit_code, run_gallery(client))
    finally:
        client.close()

    print('\n结果：%d 通过，%d 失败' % (PASS, FAIL))
    if FAIL:
        return 1
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
