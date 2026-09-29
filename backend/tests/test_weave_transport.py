"""Provider-boundary regressions without importing the application's database stack."""

import ast
import asyncio
import copy
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[1]


def load_function(relative_path, name, namespace):
    path = BACKEND / relative_path
    source = ast.parse(path.read_text(encoding='utf-8'))
    function = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    function.decorator_list = []
    # Postponed annotations and supplied globals avoid loading databases and optional providers.
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, function], type_ignores=[]))
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class Response:
    def __init__(self, content=None, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class StreamingResponse(Response):
    def __init__(self, content, **kwargs):
        super().__init__(**kwargs)
        self.body_iterator = content


class UpstreamResponse:
    def __init__(self, body=None, *, status=200, sse=False, chunks=(), text=None):
        self.body = body
        self.status = status
        self.ok = status < 400
        self.headers = {'Content-Type': 'text/event-stream' if sse else 'application/json'}
        self.chunks = chunks
        self.raw_text = text
        self.close_calls = 0

    async def json(self, **kwargs):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body

    async def text(self):
        return self.raw_text if self.raw_text is not None else json.dumps(self.body)

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f'HTTP {self.status}')


class RecordingTrace:
    def __init__(self, arguments, events, disabled=False):
        self.arguments = arguments
        self.events = events
        self.status = None
        self.output = None
        self.exception = None
        self.finished = False
        self.protocol = None
        self.disabled = disabled

    def set_status(self, status):
        self.status = status

    def finish(self, output=None, exception=None):
        if self.finished or self.disabled:
            return
        self.finished = True
        self.output = copy.deepcopy(output)
        self.exception = exception
        self.events.append('finish')

    async def wrap_stream(self, iterator, protocol):
        self.protocol = protocol
        chunks = []
        try:
            async for chunk in iterator:
                chunks.append(chunk)
                yield chunk
        except BaseException as exc:
            self.finish(output=chunks, exception=exc)
            raise
        finally:
            await iterator.aclose()
            self.finish(output=chunks)


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.traces = []
        self.disabled = False
        self.upstream = UpstreamResponse({'choices': [{'message': {'content': 'answer'}}]})
        self.session = SimpleNamespace(request=AsyncMock(side_effect=self.send))
        self.api_config = {}
        self.request = SimpleNamespace(
            state=SimpleNamespace(),
            app=SimpleNamespace(state=SimpleNamespace(OPENAI_MODELS={'test-model': {'urlIdx': 0}})),
        )
        self.user = SimpleNamespace(id='user-id', name='test', email='test@example.invalid', role='admin')
        self.namespace = {
            'asyncio': asyncio,
            'log': logging.getLogger(__name__),
            'HTTPException': HTTPException,
            'JSONResponse': Response,
            'PlainTextResponse': Response,
            'Response': Response,
            'StreamingResponse': StreamingResponse,
            'Depends': lambda dependency: dependency,
            'get_verified_user': lambda: None,
            'JSONCodec': SimpleNamespace(dumps=json.dumps, loads=json.loads, JSONDecodeError=json.JSONDecodeError),
            'Config': SimpleNamespace(get=AsyncMock(return_value=True)),
            'Models': SimpleNamespace(get_model_by_id=AsyncMock(return_value=None)),
            'check_model_access': AsyncMock(),
            'BYPASS_MODEL_ACCESS_CONTROL': False,
            'ENABLE_FORWARD_USER_INFO_HEADERS': False,
            'AIOHTTP_CLIENT_SESSION_SSL': True,
            'ERROR_MESSAGES': SimpleNamespace(SERVER_CONNECTION_ERROR='provider failed'),
            'get_session': AsyncMock(return_value=self.session),
            'get_client_timeout': lambda **kwargs: None,
            'get_openai_connection': AsyncMock(
                side_effect=lambda idx: ('https://provider.invalid/v1', 'SECRET', self.api_config)
            ),
            'get_headers_and_cookies': AsyncMock(return_value=({'Authorization': 'Bearer SECRET'}, {})),
            'get_custom_headers': AsyncMock(return_value={}),
            'strip_provider_model_prefix': lambda model, prefix: model.removeprefix(f'{prefix}.') if prefix else model,
            'is_openai_new_model': lambda model: False,
            '_clean_proxy_headers': dict,
            'publish_model_provider_request_failed': AsyncMock(),
            'cleanup_response': self.cleanup,
            'stream_wrapper': self.stream,
            'start_provider_trace': self.start,
            'convert_to_responses_payload': lambda payload: {
                **{key: value for key, value in payload.items() if key != 'messages'},
                'input': payload['messages'],
            },
            'convert_responses_result': self.convert_response,
            'urlparse': urlparse,
            '_TRACED_GENERATION_ENDPOINTS': self.load_ollama_endpoints(),
        }

    @staticmethod
    def load_ollama_endpoints():
        source = ast.parse((BACKEND / 'open_webui/routers/ollama.py').read_text(encoding='utf-8'))
        assignment = next(
            node
            for node in source.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == '_TRACED_GENERATION_ENDPOINTS' for target in node.targets
            )
        )
        return ast.literal_eval(assignment.value)

    async def send(self, *args, **kwargs):
        return self.upstream

    async def cleanup(self, response):
        if response is not None:
            response.close_calls += 1

    async def stream(self, response, **kwargs):
        try:
            for chunk in response.chunks:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield chunk
        finally:
            await self.cleanup(response)

    def start(self, **kwargs):
        trace = RecordingTrace(kwargs, self.events, disabled=self.disabled)
        self.traces.append(trace)
        return trace

    def convert_response(self, response):
        self.events.append('convert')
        return {'converted': response}

    def openai_chat(self):
        return load_function('open_webui/routers/openai.py', 'generate_chat_completion', self.namespace)

    def openai_responses(self):
        return load_function('open_webui/routers/openai.py', 'responses', self.namespace)

    def ollama(self):
        return load_function('open_webui/routers/ollama.py', 'send_request', self.namespace)

    def anthropic(self):
        self.namespace['openai'] = SimpleNamespace(
            get_anthropic_request_target=AsyncMock(
                return_value=(
                    'test-model',
                    {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}]},
                    'https://provider.invalid/v1',
                    'SECRET',
                    {'x-api-key': 'SECRET'},
                    {},
                )
            ),
            _clean_proxy_headers=dict,
            publish_model_provider_request_failed=AsyncMock(),
        )
        return load_function('open_webui/main.py', 'passthrough_anthropic_messages', self.namespace)

    @staticmethod
    def chat_body(**kwargs):
        return {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}], **kwargs}

    async def test_chat_captures_final_serialized_provider_payload(self):
        form = self.chat_body(
            max_completion_tokens=42,
            stream=False,
            stream_options={'include_usage': True},
            metadata={'chat_id': 'chat-1'},
        )
        result = await self.openai_chat()(self.request, form, self.user)
        trace = self.traces[0]
        request_body = self.session.request.call_args.kwargs['data']
        self.assertEqual(trace.arguments['payload'], request_body)
        actual = json.loads(request_body)
        self.assertEqual(actual['max_tokens'], 42)
        self.assertNotIn('max_completion_tokens', actual)
        self.assertNotIn('stream_options', actual)
        self.assertNotIn('metadata', actual)
        self.assertEqual(trace.arguments['metadata'], {'chat_id': 'chat-1'})
        self.assertEqual(trace.output, result)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_responses_capture_precedes_conversion(self):
        self.api_config['api_type'] = 'responses'
        original = {'object': 'response', 'output': [{'type': 'message', 'content': [{'text': 'answer'}]}]}
        self.upstream.body = original
        result = await self.openai_chat()(self.request, self.chat_body(), self.user)
        trace = self.traces[0]
        self.assertEqual(trace.output, original)
        self.assertEqual(result, {'converted': original})
        self.assertEqual(self.events, ['finish', 'convert'])
        self.assertTrue(trace.arguments['url'].endswith('/responses'))
        self.assertIn('input', json.loads(trace.arguments['payload']))

    async def test_chat_json_http_error_keeps_upstream_output(self):
        self.upstream = UpstreamResponse({'error': {'message': 'rate limit'}}, status=429)
        result = await self.openai_chat()(self.request, self.chat_body(), self.user)
        self.assertEqual(result.status_code, 429)
        self.assertEqual(self.traces[0].status, 429)
        self.assertEqual(self.traces[0].output, self.upstream.body)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_chat_sse_http_error_is_recorded_before_return(self):
        self.upstream = UpstreamResponse(status=503, sse=True, text='upstream unavailable')
        result = await self.openai_chat()(self.request, self.chat_body(stream=True), self.user)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.traces[0].output, 'upstream unavailable')
        self.assertEqual(self.traces[0].status, 503)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_chat_stream_preserves_bytes_and_cleanup(self):
        chunks = [b'data: {"choices":', b'[]}\n\n', b'data: [DONE]\n\n']
        self.upstream = UpstreamResponse(sse=True, chunks=chunks)
        result = await self.openai_chat()(self.request, self.chat_body(stream=True), self.user)
        self.assertFalse(self.traces[0].finished)
        self.assertEqual([chunk async for chunk in result.body_iterator], chunks)
        self.assertTrue(self.traces[0].finished)
        self.assertEqual(self.traces[0].protocol, 'sse')
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_disabled_trace_preserves_chat_response(self):
        self.disabled = True
        result = await self.openai_chat()(self.request, self.chat_body(), self.user)
        self.assertEqual(result, self.upstream.body)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_disabled_trace_preserves_stream_and_cleanup(self):
        self.disabled = True
        chunks = [b'data: {}\n\n', b'data: [DONE]\n\n']
        self.upstream = UpstreamResponse(sse=True, chunks=chunks)
        result = await self.openai_chat()(self.request, self.chat_body(stream=True), self.user)
        self.assertEqual([chunk async for chunk in result.body_iterator], chunks)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_request_cancellation_is_recorded_and_propagated(self):
        self.session.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.openai_chat()(self.request, self.chat_body(), self.user)
        self.assertIsInstance(self.traces[0].exception, asyncio.CancelledError)

    async def test_direct_responses_captures_exact_serialized_body(self):
        payload = {'model': 'test-model', 'input': 'hello'}
        form = SimpleNamespace(model='test-model', model_dump=lambda **kwargs: dict(payload))
        result = await self.openai_responses()(self.request, form, self.user)
        self.assertEqual(self.traces[0].arguments['payload'], self.session.request.call_args.kwargs['data'])
        self.assertEqual(self.traces[0].output, result)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_direct_responses_sse_error_is_traced_without_changing_stream(self):
        chunks = [b'event: error\ndata: {"error":"unavailable"}\n\n']
        self.upstream = UpstreamResponse(status=503, sse=True, chunks=chunks)
        payload = {'model': 'test-model', 'input': 'hello', 'stream': True}
        form = SimpleNamespace(model='test-model', model_dump=lambda **kwargs: dict(payload))
        result = await self.openai_responses()(self.request, form, self.user)
        self.assertEqual(result.status_code, 503)
        self.assertEqual([chunk async for chunk in result.body_iterator], chunks)
        self.assertEqual(self.traces[0].status, 503)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_nonstream_records_response(self):
        payload = '{"model":"llama","messages":[],"stream":false}'
        result = await self.ollama()('https://ollama.invalid/api/chat', payload=payload)
        self.assertEqual(self.traces[0].arguments['payload'], self.session.request.call_args.kwargs['data'])
        self.assertEqual(self.traces[0].output, result)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_generation_endpoint_protocols(self):
        for path, (_, protocol) in self.namespace['_TRACED_GENERATION_ENDPOINTS'].items():
            with self.subTest(path=path):
                chunks = [b'{"message":', b'{"content":"hello"}}\n']
                self.upstream = UpstreamResponse(chunks=chunks)
                result = await self.ollama()(
                    f'https://ollama.invalid/prefix{path}', payload=b'{"model":"llama"}', stream=True
                )
                self.assertEqual([chunk async for chunk in result.body_iterator], chunks)
                self.assertEqual(self.traces[-1].protocol, protocol)
                self.assertEqual(self.traces[-1].arguments['payload'], b'{"model":"llama"}')
                self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_non_generation_request_is_not_traced(self):
        result = await self.ollama()('https://ollama.invalid/api/pull', payload='{}')
        self.assertEqual(result, self.upstream.body)
        self.assertEqual(self.traces, [])
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_non_json_http_error_keeps_body(self):
        self.upstream = UpstreamResponse(ValueError('not JSON'), status=502, text='bad gateway')
        with self.assertRaises(HTTPException) as raised:
            await self.ollama()('https://ollama.invalid/api/chat', payload='{}')
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(self.traces[0].output, 'bad gateway')
        self.assertEqual(self.traces[0].status, 502)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_stream_failure_propagates_and_closes(self):
        self.upstream = UpstreamResponse(chunks=[b'{"message":', ConnectionError('lost connection')])
        result = await self.ollama()('https://ollama.invalid/api/chat', payload='{}', stream=True)
        with self.assertRaises(ConnectionError):
            _ = [chunk async for chunk in result.body_iterator]
        self.assertIsInstance(self.traces[0].exception, ConnectionError)
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_ollama_request_cancellation_propagates(self):
        self.session.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.ollama()('https://ollama.invalid/api/chat', payload='{}')
        self.assertIsInstance(self.traces[0].exception, asyncio.CancelledError)

    async def test_anthropic_captures_provider_response(self):
        result = await self.anthropic()(self.request, self.chat_body(), self.user)
        trace = self.traces[0]
        self.assertEqual(trace.output, result)
        payload = trace.arguments['payload']
        self.assertEqual(
            json.loads(payload) if isinstance(payload, str) else payload,
            json.loads(self.session.request.call_args.kwargs['data']),
        )
        self.assertEqual(trace.arguments['operation'], 'anthropic.messages')
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_anthropic_stream_preserves_bytes(self):
        chunks = [b'event: message_start\n', b'data: {}\n\n']
        self.upstream = UpstreamResponse(sse=True, chunks=chunks)
        result = await self.anthropic()(self.request, self.chat_body(), self.user)
        self.assertEqual([chunk async for chunk in result.body_iterator], chunks)
        self.assertEqual(self.traces[0].protocol, 'sse')
        self.assertEqual(self.upstream.close_calls, 1)

    async def test_anthropic_request_cancellation_propagates(self):
        self.session.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.anthropic()(self.request, self.chat_body(), self.user)
        self.assertIsInstance(self.traces[0].exception, asyncio.CancelledError)


if __name__ == '__main__':
    unittest.main()
