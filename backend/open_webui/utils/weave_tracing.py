"""Opt-in provider-boundary traces, exported without blocking chat requests.

Only this module imports Weave, and only after explicit configuration. Provider
headers are deliberately never accepted. Captured bodies contain conversation
content; enable this integration only for a W&B project intended to receive it.
"""

import asyncio
import codecs
import contextvars
import copy
import datetime as dt
import importlib
import json
import logging
import os
import queue
import re
import threading
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)
_DEFAULT_CAPTURE_BYTES = 2 * 1024 * 1024
_exporter = None
_SENSITIVE_KEYS = {
    'authorization',
    'proxyauthorization',
    'apikey',
    'accesskey',
    'secretkey',
    'token',
    'accesstoken',
    'refreshtoken',
    'idtoken',
    'password',
    'secret',
    'clientsecret',
    'cookie',
    'setcookie',
    'headers',
    'credentials',
    'sessionid',
}
_MEDIA_KEYS = {'images', 'b64json', 'imagedata', 'audiodata', 'inputaudio', 'audio'}
_METADATA_KEYS = {'chat_id', 'message_id', 'task', 'task_id'}


def _safe_url(value):
    try:
        parts = urlsplit(value)
        # Keep the original escaped path, but never credentials, query, or fragment.
        return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
    except Exception:
        return '[invalid URL]'


def _safe_text(value):
    value = re.sub(r"(?<![\w])data:[^,\s]{0,128},[^\s\"'<>]*", '[inline media omitted]', value, flags=re.I)
    value = re.sub(r"https?://[^\s\"'<>]+", lambda match: _safe_url(match[0]), value)
    return value


class _Budget:
    def __init__(self, limit):
        self.remaining = limit
        self.truncated = False

    def capture(self, value, depth=0):
        if self.remaining <= 0 or depth > 32:
            self.truncated = True
            return '[capture truncated]'
        if isinstance(value, dict):
            return self._dict(value, depth)
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                if self.remaining <= 0:
                    self.truncated = True
                    result.append('[capture truncated]')
                    break
                self.remaining -= 1
                result.append(self.capture(item, depth + 1))
            return result
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        if isinstance(value, str):
            encoded = _safe_text(value).encode('utf-8')
            if len(encoded) > self.remaining:
                self.truncated = True
                value = encoded[: max(self.remaining, 0)].decode('utf-8', errors='ignore') + '[capture truncated]'
                self.remaining = 0
                return value
            self.remaining -= len(encoded) + 2
            return encoded.decode('utf-8')
        if value is None or isinstance(value, (bool, int, float)):
            self.remaining -= 16
            return value
        return self.capture(f'[{type(value).__name__} omitted]', depth + 1)

    def _dict(self, value, depth):
        result = {}
        for key, item in value.items():
            if self.remaining <= 0:
                self.truncated = True
                result['_capture_truncated'] = True
                break
            key = str(key)
            self.remaining -= len(key.encode('utf-8')) + 4
            normalized = re.sub(r'[^a-z0-9]', '', key.lower())
            if normalized in _SENSITIVE_KEYS:
                result[key] = '[redacted]'
            elif normalized in _MEDIA_KEYS or (normalized == 'data' and value.get('type') == 'base64'):
                result[key] = '[media omitted]'
            else:
                result[key] = self.capture(item, depth + 1)
        return result


def _capture_limit():
    try:
        return max(1024, min(int(os.environ.get('WEAVE_MAX_CAPTURE_BYTES', _DEFAULT_CAPTURE_BYTES)), 32 * 1024 * 1024))
    except (TypeError, ValueError):
        return _DEFAULT_CAPTURE_BYTES


def _decode_body(body, limit):
    if isinstance(body, (str, bytes)):
        if len(body) > limit:
            return {'body': '[body omitted: capture limit exceeded]', '_capture_truncated': True}
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            pass
    return body


def _append_fields(target, delta):
    """Merge only known textual delta fields, leaving raw events authoritative."""
    for key in ('content', 'reasoning_content', 'reasoning', 'thinking', 'refusal', 'arguments', 'name'):
        value = delta.get(key)
        if isinstance(value, str):
            target[key] = target.get(key, '') + value
    for key in ('role', 'id', 'type'):
        if key in delta:
            target[key] = delta[key]


class _StreamCapture:
    """Incremental SSE/NDJSON decoder; never changes the forwarded stream."""

    def __init__(self, protocol, limit):
        self.protocol = protocol
        self.limit = limit
        self.seen = 0
        self.truncated = False
        self.error = False
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.buffer = ''
        self.data_lines = []
        self.events = []
        self.result = {}
        self.choices = {}
        self.response_items = {}
        self.response_complete = False
        self.anthropic_blocks = {}

    def feed(self, chunk):
        if not isinstance(chunk, (str, bytes, bytearray, memoryview)):
            return
        chunk = chunk.encode('utf-8') if isinstance(chunk, str) else bytes(chunk)
        available = max(0, self.limit - self.seen)
        self.seen += len(chunk)
        if len(chunk) > available:
            self.truncated = True
        if available:
            self.buffer += self.decoder.decode(chunk[:available])
            self._lines()

    def _lines(self, final=False):
        while True:
            match = re.search(r'\r\n|\r|\n', self.buffer)
            if match is None:
                break
            if not final and match[0] == '\r' and match.end() == len(self.buffer):
                break
            line, self.buffer = self.buffer[: match.start()], self.buffer[match.end() :]
            self._line(line)
        if final:
            if self.buffer:
                self._line(self.buffer)
                self.buffer = ''
            if self.data_lines:
                self._event('\n'.join(self.data_lines))
                self.data_lines = []

    def _line(self, line):
        if self.protocol == 'ndjson':
            if line.strip():
                self._event(line)
        elif not line:
            if self.data_lines:
                self._event('\n'.join(self.data_lines))
                self.data_lines = []
        elif line.startswith('data:'):
            self.data_lines.append(line[5:].removeprefix(' '))

    def _event(self, data):
        if data.strip() == '[DONE]':
            self.events.append('[DONE]')
            return
        try:
            event = json.loads(data)
        except (ValueError, TypeError):
            self.events.append({'unparsed': data})
            return
        self.events.append(event)
        if isinstance(event, dict):
            self._merge_event(event)

    def _merge_event(self, event):
        if event.get('error') or event.get('type') in ('error', 'response.failed'):
            self.error = True
        for key in ('id', 'model', 'object', 'created'):
            if key in event:
                self.result[key] = event[key]
        if 'usage' in event and event.get('type') != 'message_delta':
            self.result['usage'] = copy.deepcopy(event['usage'])
        for choice in event.get('choices', []):
            self._choice(choice)
        if self.protocol == 'ndjson':
            self._ollama(event)
        elif str(event.get('type', '')).startswith('response.'):
            self._responses(event)
        elif event.get('type') in (
            'message_start',
            'content_block_start',
            'content_block_delta',
            'message_delta',
            'message_stop',
        ):
            self._anthropic(event)

    def _choice(self, choice):
        index = choice.get('index', 0)
        merged = self.choices.setdefault(index, {'index': index, 'message': {'role': 'assistant', 'content': ''}})
        delta = choice.get('delta', choice.get('message', {}))
        _append_fields(merged['message'], delta)
        if isinstance(choice.get('text'), str):
            merged['text'] = merged.get('text', '') + choice['text']
            merged['message']['content'] += choice['text']
        for tool in delta.get('tool_calls', []):
            calls = merged['message'].setdefault('tool_calls', [])
            tool_index = tool.get('index', len(calls))
            existing = next((call for call in calls if call.get('index') == tool_index), None)
            if existing is None:
                existing = {'index': tool_index, 'function': {}}
                calls.append(existing)
            _append_fields(existing, tool)
            _append_fields(existing['function'], tool.get('function', {}))
        if 'function_call' in delta:
            _append_fields(merged['message'].setdefault('function_call', {}), delta['function_call'])
        if choice.get('finish_reason') is not None:
            merged['finish_reason'] = choice['finish_reason']

    def _ollama(self, event):
        for key, value in event.items():
            if key not in ('message', 'response'):
                self.result[key] = value
        message = self.result.setdefault('message', {'role': 'assistant', 'content': ''})
        _append_fields(message, event.get('message', {}))
        if event.get('message', {}).get('tool_calls'):
            message.setdefault('tool_calls', []).extend(event['message']['tool_calls'])
        if isinstance(event.get('response'), str):
            self.result['response'] = self.result.get('response', '') + event['response']
            message['content'] += event['response']
        self.choices[0] = {'index': 0, 'message': message, 'finish_reason': event.get('done_reason')}
        if 'prompt_eval_count' in event or 'eval_count' in event:
            prompt, completion = event.get('prompt_eval_count', 0), event.get('eval_count', 0)
            self.result['usage'] = {
                'prompt_tokens': prompt,
                'completion_tokens': completion,
                'total_tokens': prompt + completion,
            }

    def _responses(self, event):
        kind = event['type']
        if isinstance(event.get('response'), dict):
            self.result.update(event['response'])
            if kind in ('response.completed', 'response.failed', 'response.incomplete'):
                self.response_complete = True
        index = event.get('output_index', 0)
        if isinstance(event.get('item'), dict):
            self.response_items[index] = copy.deepcopy(event['item'])
        if kind == 'response.function_call_arguments.delta':
            item = self.response_items.setdefault(index, {'type': 'function_call', 'arguments': ''})
            item['arguments'] = item.get('arguments', '') + event.get('delta', '')
        if kind in ('response.output_text.delta', 'response.refusal.delta'):
            item = self.response_items.setdefault(index, {'type': 'message', 'role': 'assistant', 'content': []})
            content_index = event.get('content_index', 0)
            content = item.setdefault('content', [])
            # Keep unexpected sparse indices in raw_events; never allocate gaps.
            if not isinstance(content_index, int) or not 0 <= content_index <= len(content):
                return
            if content_index == len(content):
                content.append({'type': 'output_text', 'text': ''})
            part = content[content_index]
            key = 'refusal' if kind == 'response.refusal.delta' else 'text'
            part[key] = part.get(key, '') + event.get('delta', '')

    def _anthropic(self, event):
        kind = event['type']
        index = event.get('index', 0)
        if kind == 'message_start' and isinstance(event.get('message'), dict):
            self.result.update(copy.deepcopy(event['message']))
        elif kind == 'content_block_start':
            self.anthropic_blocks[index] = copy.deepcopy(event.get('content_block', {}))
        elif kind == 'content_block_delta':
            block = self.anthropic_blocks.setdefault(index, {})
            delta = event.get('delta', {})
            for key in ('text', 'thinking', 'signature'):
                if isinstance(delta.get(key), str):
                    block[key] = block.get(key, '') + delta[key]
            if isinstance(delta.get('partial_json'), str):
                block['_partial_json'] = block.get('_partial_json', '') + delta['partial_json']
        elif kind == 'message_delta':
            self.result.update(event.get('delta', {}))
            self.result.setdefault('usage', {}).update(event.get('usage', {}))

    def finish(self):
        self.buffer += self.decoder.decode(b'', final=True)
        self._lines(final=True)
        if self.response_items and not self.response_complete:
            self.result['output'] = list(self.response_items.values())
        if self.anthropic_blocks:
            for block in self.anthropic_blocks.values():
                if '_partial_json' in block:
                    partial = block.pop('_partial_json')
                    try:
                        block['input'] = json.loads(partial)
                    except ValueError:
                        block['partial_json'] = partial
            self.result['content'] = list(self.anthropic_blocks.values())
        if self.choices:
            self.result['choices'] = list(self.choices.values())
        self.result['raw_events'] = self.events
        budget = _Budget(self.limit)
        output = budget.capture(self.result)
        self.truncated |= budget.truncated
        return output


class _Exporter:
    def __init__(self, client, context=None):
        self.client = client
        self.pending = queue.Queue(maxsize=64)
        self.closing = threading.Event()
        # Weave settings are ContextVars, set in the initialization thread.
        context = context if context is not None else contextvars.copy_context()
        self.thread = threading.Thread(target=context.run, args=(self._run,), name='weave-export', daemon=True)
        self.thread.start()

    def submit(self, record):
        if self.closing.is_set():
            return
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            log.warning('Weave export queue is full; dropping a trace')

    def _run(self):
        while not self.closing.is_set() or not self.pending.empty():
            try:
                record = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                call = self.client.create_call(
                    record['op'],
                    inputs=record['inputs'],
                    attributes=record['attributes'],
                    use_stack=False,
                    started_at=record['started_at'],
                )
                call.summary.update(record['summary'])
                self.client.finish_call(
                    call, output=record['output'], exception=record['exception'], ended_at=record['ended_at']
                )
                # Do not drain our bounded queue into the SDK's unbounded future
                # executor during a slow upload or outage. This worker may wait;
                # provider requests never wait for this flush.
                self.client.flush()
            except Exception as exc:
                # SDK/network exceptions may include authorization or request data.
                log.warning('Weave trace export failed (%s)', type(exc).__name__)
            finally:
                self.pending.task_done()
        try:
            self.client.flush()
        except Exception as exc:
            log.warning('Weave flush failed (%s)', type(exc).__name__)

    def close(self):
        self.closing.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            log.warning('Weave shutdown timed out; some traces may not have been exported')


async def initialize_weave():
    """Initialize only when opted in; never initiate an interactive W&B login."""
    global _exporter
    if _exporter is not None or os.environ.get('ENABLE_WEAVE', 'false').lower() != 'true':
        return
    project = os.environ.get('WEAVE_PROJECT', '').strip()
    if not project or not os.environ.get('WANDB_API_KEY', '').strip():
        log.warning('Weave tracing disabled: WEAVE_PROJECT and WANDB_API_KEY are required')
        return

    def initialize():
        weave = importlib.import_module('weave')
        client = weave.init(
            project,
            settings={
                'implicitly_patch_integrations': False,
                'capture_code': False,
                'print_call_link': False,
                'retry_max_attempts': 1,
                'http_timeout': 5.0,
                'max_calls_queue_size': 64,
                'enable_disk_fallback': False,
            },
        )
        return client, contextvars.copy_context()

    try:
        client, context = await asyncio.wait_for(asyncio.to_thread(initialize), timeout=15)
        _exporter = _Exporter(client, context)
        log.info('Weave provider tracing enabled')
    except Exception as exc:
        log.warning('Weave tracing disabled: initialization failed (%s)', type(exc).__name__)


async def shutdown_weave():
    global _exporter
    exporter, _exporter = _exporter, None
    if exporter is not None:
        await asyncio.to_thread(exporter.close)


class ProviderTrace:
    def __init__(self, exporter=None, *, provider='', operation='', url='', payload=None, metadata=None):
        self.exporter = exporter
        self.finished = False
        self.status = None
        self.capture = None
        self.limit = _capture_limit()
        self.started_at = dt.datetime.now(dt.UTC)
        self.operation = operation
        self.input_budget = _Budget(self.limit)
        if exporter is not None:
            body = _decode_body(payload, self.limit)
            self.inputs = self.input_budget.capture(body if isinstance(body, dict) else {'body': body})
            metadata = {key: value for key, value in (metadata or {}).items() if key in _METADATA_KEYS}
            self.attributes = _Budget(16 * 1024).capture({**metadata, 'provider': provider, 'url': _safe_url(url)})

    def set_status(self, status):
        self.status = status

    def _summary(self, output, budget):
        truncated = self.input_budget.truncated or budget.truncated or bool(self.inputs.get('_capture_truncated'))
        if self.capture is not None:
            truncated |= self.capture.truncated
        summary = {'status_code': self.status, 'capture_truncated': truncated}
        if isinstance(output, dict):
            summary['capture_truncated'] |= bool(output.get('_capture_truncated'))
            usage = output.get('usage')
            if isinstance(usage, dict):
                model = output.get('model') or self.inputs.get('model') or 'unknown'
                usage = usage.copy()
                if 'input_tokens' in usage:
                    usage.setdefault('prompt_tokens', usage['input_tokens'])
                if 'output_tokens' in usage:
                    usage.setdefault('completion_tokens', usage['output_tokens'])
                summary['usage'] = {str(model): {'requests': 1, **usage}}
        return summary

    def finish(self, output=None, exception=None):
        if self.finished or self.exporter is None:
            return
        self.finished = True
        try:
            budget = _Budget(self.limit)
            output = budget.capture(_decode_body(output, self.limit))
            summary = self._summary(output, budget)
            if exception is not None:
                exception = RuntimeError(f'Provider request failed ({type(exception).__name__})')
            elif self.status is not None and self.status >= 400:
                exception = RuntimeError(f'Provider returned HTTP {self.status}')
            elif self.capture is not None and self.capture.error:
                exception = RuntimeError('Provider returned an error stream event')
            elif isinstance(output, dict) and output.get('error'):
                exception = RuntimeError('Provider returned an error response')
            self.exporter.submit(
                {
                    'op': self.operation,
                    'inputs': self.inputs,
                    'attributes': self.attributes,
                    'output': output,
                    'summary': summary,
                    'exception': exception,
                    'started_at': self.started_at,
                    'ended_at': dt.datetime.now(dt.UTC),
                }
            )
        except Exception as exc:
            log.warning('Weave trace capture failed (%s)', type(exc).__name__)

    async def wrap_stream(self, iterator, protocol='sse'):
        if self.exporter is None:
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                if hasattr(iterator, 'aclose'):
                    await iterator.aclose()
            return
        self.capture = _StreamCapture(protocol, self.limit)
        error = None
        try:
            async for chunk in iterator:
                try:
                    self.capture.feed(chunk)
                except Exception:
                    self.capture.truncated = True
                yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                output = self.capture.finish()
            except Exception:
                output = {'_capture_truncated': True}
            self.finish(output, error)
            if hasattr(iterator, 'aclose'):
                await iterator.aclose()


def start_provider_trace(*, provider, operation, url, payload, metadata=None):
    try:
        return ProviderTrace(
            _exporter, provider=provider, operation=operation, url=url, payload=payload, metadata=metadata
        )
    except Exception as exc:
        log.warning('Weave trace capture unavailable (%s)', type(exc).__name__)
        return ProviderTrace()
