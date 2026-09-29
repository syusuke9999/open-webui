"""Offline behavioral checks for provider tracing; no SDK or credentials required.

Run from the repository root with:
    python -m unittest discover -s backend/tests -p 'test_weave_tracing.py' -v
"""

import asyncio
import copy
import importlib.util
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'open_webui/utils/weave_tracing.py'
SPEC = importlib.util.spec_from_file_location('weave_tracing_under_test', MODULE_PATH)
tracing = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tracing
SPEC.loader.exec_module(tracing)


class RecordingExporter:
    def __init__(self):
        self.records = []

    def submit(self, record):
        self.records.append(record)


async def chunks_from(chunks):
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


def sse_event(value):
    return ('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode('utf-8')


def streamed_choice(text, **extra):
    return {
        'id': 'chatcmpl-test',
        'model': 'test-model',
        'choices': [{'index': 0, 'delta': {'content': text, **extra}}],
    }


class ProviderTracingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exporter = RecordingExporter()
        self.addCleanup(patch.stopall)
        patch.object(tracing, '_exporter', self.exporter).start()
        patch.dict(os.environ, {'ENABLE_WEAVE': 'true', 'WEAVE_MAX_CAPTURE_BYTES': '65536'}).start()

    def start_trace(self, payload=None, **kwargs):
        return tracing.start_provider_trace(
            provider=kwargs.pop('provider', 'openai-compatible'),
            operation=kwargs.pop('operation', 'openai.chat.completions'),
            url=kwargs.pop('url', 'https://provider.example/v1/chat/completions'),
            payload=payload if payload is not None else {'model': 'test-model', 'messages': []},
            **kwargs,
        )

    async def consume(self, trace, chunks, protocol='sse'):
        return [chunk async for chunk in trace.wrap_stream(chunks_from(chunks), protocol=protocol)]

    def only_record(self):
        self.assertEqual(len(self.exporter.records), 1)
        return self.exporter.records[0]

    async def test_disabled_trace_passes_through_bytes_and_propagates_error(self):
        with patch.object(tracing, '_exporter', None):
            trace = self.start_trace()
            chunks = [b'data: ', b'unchanged\n\n', b'data: [DONE]\n\n']
            self.assertEqual(await self.consume(trace, chunks), chunks)
            trace.finish({'ignored': True})

            async def broken():
                yield b'partial'
                raise ConnectionError('upstream disconnected')

            with self.assertRaises(ConnectionError):
                _ = [chunk async for chunk in trace.wrap_stream(broken())]
        self.assertEqual(self.exporter.records, [])

    def test_records_actual_json_request_and_response_without_mutating_them(self):
        payload = {
            'model': 'provider-model',
            'messages': [{'role': 'system', 'content': 'Rules'}, {'role': 'user', 'content': 'こんにちは'}],
            'tools': [{'type': 'function', 'function': {'name': 'weather', 'parameters': {'type': 'object'}}}],
            'temperature': 0.7,
        }
        output = {'id': 'actual-result', 'choices': [{'message': {'role': 'assistant', 'content': '晴れ'}}]}
        originals = copy.deepcopy((payload, output))
        trace = self.start_trace(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        trace.set_status(200)
        trace.finish(output)
        record = self.only_record()
        self.assertEqual(record['inputs'], payload)
        self.assertEqual(record['output'], output)
        self.assertEqual(record['summary']['status_code'], 200)
        self.assertEqual((payload, output), originals)

    def test_records_original_responses_api_output(self):
        output = {
            'id': 'resp_test',
            'object': 'response',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '回答'}]}],
            'usage': {'input_tokens': 11, 'output_tokens': 4, 'total_tokens': 15},
        }
        trace = self.start_trace({'model': 'test', 'input': '質問'}, operation='openai.responses')
        trace.finish(output)
        self.assertEqual(self.only_record()['output'], output)

    async def test_fragmented_utf8_sse_and_done_preserve_every_original_chunk(self):
        first = streamed_choice('こんにちは', role='assistant')
        second = streamed_choice('世界')
        terminal = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 12}}
        wire = sse_event(first) + sse_event(second) + sse_event(terminal) + b'data: [DONE]\n\n'
        chunks = [wire[index : index + 1] for index in range(len(wire))]
        returned = await self.consume(self.start_trace(), chunks)
        self.assertEqual(returned, chunks)
        self.assertTrue(all(original is actual for original, actual in zip(chunks, returned)))
        output = self.only_record()['output']
        self.assertEqual(output['choices'][0]['message']['content'], 'こんにちは世界')
        self.assertEqual(output['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(output['usage'], {'total_tokens': 12})
        serialized = json.dumps(output, ensure_ascii=False)
        self.assertNotIn('\ufffd', serialized)
        self.assertIn('raw_events', output)

    async def test_interleaved_concurrent_requests_keep_their_inputs_and_outputs(self):
        left = self.start_trace({'model': 'left', 'messages': [{'role': 'user', 'content': 'L'}]})
        right = self.start_trace({'model': 'right', 'messages': [{'role': 'user', 'content': 'R'}]})
        await asyncio.gather(
            self.consume(left, [sse_event(streamed_choice('left-')), sse_event(streamed_choice('answer'))]),
            self.consume(right, [sse_event(streamed_choice('right-')), sse_event(streamed_choice('answer'))]),
        )
        self.assertEqual(len(self.exporter.records), 2)
        by_model = {record['inputs']['model']: record for record in self.exporter.records}
        self.assertEqual(by_model['left']['output']['choices'][0]['message']['content'], 'left-answer')
        self.assertEqual(by_model['right']['output']['choices'][0]['message']['content'], 'right-answer')

    async def test_openai_tool_calls_and_reasoning_are_assembled(self):
        first = {
            'choices': [
                {
                    'index': 0,
                    'delta': {
                        'role': 'assistant',
                        'reasoning_content': 'Think ',
                        'tool_calls': [
                            {
                                'index': 0,
                                'id': 'call_test',
                                'type': 'function',
                                'function': {'name': 'weather', 'arguments': '{"city":'},
                            }
                        ],
                    },
                }
            ],
        }
        second = {
            'choices': [
                {
                    'index': 0,
                    'delta': {
                        'reasoning_content': 'more',
                        'tool_calls': [{'index': 0, 'function': {'arguments': '"東京"}'}}],
                    },
                    'finish_reason': 'tool_calls',
                }
            ],
        }
        await self.consume(self.start_trace(), [sse_event(first), sse_event(second)])
        message = self.only_record()['output']['choices'][0]['message']
        self.assertEqual(message['reasoning_content'], 'Think more')
        self.assertEqual(message['tool_calls'][0]['id'], 'call_test')
        self.assertEqual(message['tool_calls'][0]['function'], {'name': 'weather', 'arguments': '{"city":"東京"}'})

    async def test_responses_sse_preserves_terminal_provider_response(self):
        terminal = {
            'id': 'resp_final',
            'object': 'response',
            'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '最終回答'}]}],
            'usage': {'input_tokens': 3, 'output_tokens': 5, 'total_tokens': 8},
        }
        chunks = [
            sse_event({'type': 'response.output_text.delta', 'delta': '最終回答'}),
            sse_event({'type': 'response.completed', 'response': terminal}),
        ]
        self.assertEqual(await self.consume(self.start_trace(operation='openai.responses'), chunks), chunks)
        output = self.only_record()['output']
        for key, value in terminal.items():
            self.assertEqual(output[key], value)
        self.assertIn('raw_events', output)

    async def test_responses_assembly_does_not_rewrite_original_raw_events(self):
        events = [
            {
                'type': 'response.output_item.added',
                'output_index': 0,
                'item': {'id': 'message-1', 'type': 'message', 'role': 'assistant', 'content': []},
            },
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'hello'},
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': ' world'},
        ]
        await self.consume(self.start_trace(operation='openai.responses'), [sse_event(event) for event in events])
        output = self.only_record()['output']
        self.assertEqual(output['output'][0]['content'][0]['text'], 'hello world')
        self.assertEqual(output['raw_events'], events)

    async def test_anthropic_blocks_tools_and_usage_preserve_original_events(self):
        events = [
            {
                'type': 'message_start',
                'message': {
                    'id': 'msg_test',
                    'type': 'message',
                    'role': 'assistant',
                    'model': 'claude-test',
                    'content': [],
                    'usage': {'input_tokens': 11, 'output_tokens': 1},
                },
            },
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': '考える'}},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': '回答'}},
            {
                'type': 'content_block_start',
                'index': 2,
                'content_block': {'type': 'tool_use', 'id': 'tool_1', 'name': 'lookup', 'input': {}},
            },
            {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"q":'}},
            {
                'type': 'content_block_delta',
                'index': 2,
                'delta': {'type': 'input_json_delta', 'partial_json': '"東京"}'},
            },
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 15}},
            {'type': 'message_stop'},
        ]
        chunks = [sse_event(event) for event in events]
        self.assertEqual(await self.consume(self.start_trace(operation='ollama.messages'), chunks), chunks)
        output = self.only_record()['output']
        self.assertEqual(output['content'][0]['thinking'], '考える')
        self.assertEqual(output['content'][1]['text'], '回答')
        self.assertEqual(output['content'][2]['input'], {'q': '東京'})
        self.assertEqual(output['stop_reason'], 'tool_use')
        self.assertEqual(output['usage'], {'input_tokens': 11, 'output_tokens': 15})
        self.assertEqual(output['raw_events'], events)

    async def test_ollama_ndjson_preserves_thinking_tools_usage_and_last_line(self):
        events = [
            {'model': 'test', 'message': {'role': 'assistant', 'content': 'こん', 'thinking': '考え'}, 'done': False},
            {
                'model': 'test',
                'message': {
                    'role': 'assistant',
                    'content': 'にちは',
                    'thinking': '中',
                    'tool_calls': [{'function': {'name': 'lookup', 'arguments': {'query': '東京'}}}],
                },
                'done': True,
                'prompt_eval_count': 3,
                'eval_count': 5,
            },
        ]
        wire = '\n'.join(json.dumps(event, ensure_ascii=False) for event in events).encode('utf-8')
        chunks = [wire[:80], wire[80:120], wire[120:]]
        self.assertEqual(await self.consume(self.start_trace(provider='ollama'), chunks, protocol='ndjson'), chunks)
        output = self.only_record()['output']
        self.assertEqual(output['message']['content'], 'こんにちは')
        self.assertEqual(output['message']['thinking'], '考え中')
        self.assertEqual(output['message']['tool_calls'], events[1]['message']['tool_calls'])
        self.assertEqual(output['prompt_eval_count'], 3)
        self.assertEqual(output['eval_count'], 5)

    def test_non_json_http_error_body_and_status_are_recorded(self):
        trace = self.start_trace()
        trace.set_status(502)
        trace.finish('<html>upstream unavailable</html>')
        record = self.only_record()
        self.assertIn('upstream unavailable', json.dumps(record['output']))
        self.assertEqual(record['summary']['status_code'], 502)
        self.assertIsNotNone(record['exception'])

    async def test_stream_exception_is_reraised_and_partial_output_recorded(self):
        failure = ConnectionError('failure includes secret URL https://user:password@example.test')

        async def broken():
            yield sse_event(streamed_choice('partial'))
            raise failure

        trace = self.start_trace()
        with self.assertRaises(ConnectionError) as caught:
            _ = [chunk async for chunk in trace.wrap_stream(broken(), protocol='sse')]
        self.assertIs(caught.exception, failure)
        record = self.only_record()
        self.assertEqual(record['output']['choices'][0]['message']['content'], 'partial')
        self.assertIsNotNone(record['exception'])
        self.assertNotIn('password', str(record['exception']))

    async def test_cancellation_closes_source_and_finishes_partial_trace_once(self):
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def source():
            try:
                yield sse_event(streamed_choice('partial'))
                entered.set()
                await asyncio.Future()
            finally:
                closed.set()

        trace = self.start_trace()

        async def consume():
            return [chunk async for chunk in trace.wrap_stream(source(), protocol='sse')]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        trace.finish()
        record = self.only_record()
        self.assertEqual(record['output']['choices'][0]['message']['content'], 'partial')
        self.assertIsNotNone(record['exception'])

    async def test_explicit_stream_close_releases_underlying_source(self):
        closed = asyncio.Event()

        async def source():
            try:
                yield sse_event(streamed_choice('partial'))
                yield sse_event(streamed_choice('unused'))
            finally:
                closed.set()

        source_iterator = source()
        iterator = self.start_trace().wrap_stream(source_iterator)
        await anext(iterator)
        await iterator.aclose()
        self.assertTrue(closed.is_set(), 'client disconnect must immediately release upstream response')
        self.assertEqual(self.only_record()['output']['choices'][0]['message']['content'], 'partial')

    def test_finish_is_idempotent(self):
        trace = self.start_trace()
        trace.finish({'answer': 'first'})
        trace.finish({'answer': 'second'}, exception=ValueError('late'))
        self.assertEqual(self.only_record()['output'], {'answer': 'first'})

    async def test_trace_export_failure_does_not_change_chat_response(self):
        class BrokenExporter:
            def submit(self, record):
                raise RuntimeError('exporter unavailable')

        with patch.object(tracing, '_exporter', BrokenExporter()):
            trace = self.start_trace()
            chunks = [sse_event(streamed_choice('still delivered')), b'data: [DONE]\n\n']
            self.assertEqual(await self.consume(trace, chunks), chunks)
            trace.finish({'answer': 'still delivered'})

    async def test_sse_multiline_data_crlf_and_comments(self):
        chunks = [
            b': keep-alive\r\n\r\nevent: completion\r\ndata: {"choices":\r\n',
            b'data: [{"index":0,"delta":{"content":"hello"}}]}\r\n\r\n',
            b'data: [DONE]\r\n\r\n',
        ]
        self.assertEqual(await self.consume(self.start_trace(), chunks), chunks)
        self.assertEqual(self.only_record()['output']['choices'][0]['message']['content'], 'hello')

    async def test_malformed_stream_event_remains_visible_without_breaking_forwarding(self):
        chunks = [b'data: upstream sent invalid JSON\n\n', sse_event(streamed_choice('valid tail'))]
        self.assertEqual(await self.consume(self.start_trace(), chunks), chunks)
        output = self.only_record()['output']
        self.assertIn('upstream sent invalid JSON', json.dumps(output))
        self.assertEqual(output['choices'][0]['message']['content'], 'valid tail')

    def test_redacts_secrets_and_inline_images_without_mutating_request(self):
        secret = 'sk-secret-sentinel'
        payload = {
            'model': 'test',
            'api_key': secret,
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': 'Inspect image'},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,PRIVATEIMAGE'}},
                    ],
                }
            ],
            'nested': {'Authorization': 'Bearer ' + secret, 'password': secret},
        }
        original = copy.deepcopy(payload)
        trace = self.start_trace(
            payload,
            url=f'https://user:{secret}@provider.example/v1/chat?api_key={secret}&api-version=2024-01-01',
            metadata={'chat_id': 'chat-test', 'session_id': secret, 'access_token': secret},
        )
        trace.finish({'token': secret, 'answer': 'visible'})
        record = self.only_record()
        serialized = json.dumps(record, default=str)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('PRIVATEIMAGE', serialized)
        self.assertIn('Inspect image', serialized)
        self.assertIn('visible', serialized)
        self.assertEqual(payload, original)

    async def test_capture_limit_does_not_truncate_forwarded_stream(self):
        with patch.dict(os.environ, {'WEAVE_MAX_CAPTURE_BYTES': '1024'}):
            trace = self.start_trace()
            chunks = [sse_event(streamed_choice('x' * 400)) for _ in range(40)] + [b'data: [DONE]\n\n']
            returned = await self.consume(trace, chunks)
        self.assertEqual(returned, chunks)
        record = self.only_record()
        self.assertTrue(record['summary']['capture_truncated'])
        self.assertLess(len(json.dumps(record['output'])), len(b''.join(chunks)))


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(tracing, '_exporter', None).start()
        test_environment = {
            key: value
            for key, value in os.environ.items()
            if key != 'ENABLE_WEAVE' and not key.startswith(('WEAVE_', 'WANDB_'))
        }
        patch.dict(os.environ, test_environment, clear=True).start()

    def configured(self):
        os.environ.update({'ENABLE_WEAVE': 'true', 'WEAVE_PROJECT': 'entity/test', 'WANDB_API_KEY': 'fake-test-key'})

    async def test_disabled_by_default_never_imports_sdk(self):
        with patch.object(tracing.importlib, 'import_module') as load_sdk:
            await tracing.initialize_weave()
        load_sdk.assert_not_called()
        self.assertIsNone(tracing._exporter)

    async def test_missing_project_or_key_does_not_trigger_interactive_login(self):
        for missing in ('WEAVE_PROJECT', 'WANDB_API_KEY'):
            with self.subTest(missing=missing):
                self.configured()
                os.environ.pop(missing)
                with patch.object(tracing.importlib, 'import_module') as load_sdk:
                    await tracing.initialize_weave()
                load_sdk.assert_not_called()
                self.assertIsNone(tracing._exporter)

    async def test_missing_sdk_disables_tracing_without_breaking_requests(self):
        self.configured()
        with patch.object(tracing.importlib, 'import_module', side_effect=ModuleNotFoundError('weave unavailable')):
            await tracing.initialize_weave()
        self.assertIsNone(tracing._exporter)
        trace = tracing.start_provider_trace(
            provider='test', operation='test.chat', url='https://example.test', payload={}
        )
        chunks = [b'data: original\n\n']
        self.assertEqual([chunk async for chunk in trace.wrap_stream(chunks_from(chunks))], chunks)

    async def test_sdk_startup_failure_never_leaks_error_details_or_breaks_requests(self):
        self.configured()
        sdk = SimpleNamespace(init=Mock(side_effect=RuntimeError('credential=DO-NOT-LOG')))
        with patch.object(tracing.importlib, 'import_module', return_value=sdk):
            with self.assertLogs(tracing.log, level='WARNING') as logs:
                await tracing.initialize_weave()
        self.assertNotIn('DO-NOT-LOG', '\n'.join(logs.output))
        self.assertIsNone(tracing._exporter)

    async def test_initialization_and_worker_export_once_then_flush_on_shutdown(self):
        self.configured()
        finished = threading.Event()
        exported = {}

        class Client:
            def __init__(self):
                self.flush_count = 0

            def create_call(self, operation, **kwargs):
                exported['operation'] = operation
                exported['create'] = kwargs
                exported['call'] = SimpleNamespace(summary={})
                return exported['call']

            def finish_call(self, call, **kwargs):
                exported['finish'] = kwargs
                finished.set()

            def flush(self):
                self.flush_count += 1

        client = Client()
        sdk = SimpleNamespace(init=Mock(return_value=client))
        try:
            with patch.object(tracing.importlib, 'import_module', return_value=sdk):
                await tracing.initialize_weave()
                await tracing.initialize_weave()
            sdk.init.assert_called_once()
            self.assertEqual(sdk.init.call_args.args, ('entity/test',))
            self.assertFalse(sdk.init.call_args.kwargs['settings']['implicitly_patch_integrations'])
            trace = tracing.start_provider_trace(
                provider='test',
                operation='test.chat',
                url='https://example.test',
                payload={'model': 'model-test'},
            )
            trace.set_status(200)
            trace.finish({'answer': 'test'})
            self.assertTrue(await asyncio.to_thread(finished.wait, 1), 'background exporter did not process trace')
            self.assertEqual(exported['operation'], 'test.chat')
            self.assertEqual(exported['create']['inputs'], {'model': 'model-test'})
            self.assertFalse(exported['create']['use_stack'])
            self.assertEqual(exported['finish']['output'], {'answer': 'test'})
            self.assertEqual(exported['call'].summary['status_code'], 200)
            self.assertLessEqual(exported['create']['started_at'], exported['finish']['ended_at'])
        finally:
            await tracing.shutdown_weave()
        self.assertIsNone(tracing._exporter)
        self.assertEqual(client.flush_count, 2)  # Per-record backpressure, then final shutdown flush.
        await tracing.shutdown_weave()
        self.assertEqual(client.flush_count, 2)

    @unittest.skipUnless(importlib.util.find_spec('weave'), 'optional Weave SDK is not installed')
    def test_installed_sdk_graphql_transport_accepts_its_auth(self):
        import gql.transport.httpx as gql_httpx
        from weave.compat.wandb.wandb_thin import internal_api

        real_transport = gql_httpx.HTTPXTransport
        transports = []
        requests = []

        def respond(request):
            requests.append(request)
            return gql_httpx.httpx.Response(200, json={'data': {'serverInfo': {'frontendHost': 'https://weave.test'}}})

        def offline_transport(**kwargs):
            # Keep the real SDK auth and client constructor; replace only network I/O.
            transport = real_transport(**kwargs, transport=gql_httpx.httpx.MockTransport(respond))
            transports.append(transport)
            return transport

        try:
            with (
                patch.object(internal_api, 'get_wandb_api_context', return_value='synthetic-test-only'),
                patch.object(internal_api.env, 'wandb_base_url', return_value='https://weave.test'),
                patch.object(gql_httpx, 'HTTPXTransport', side_effect=offline_transport),
            ):
                result = internal_api.Api().server_info()
        finally:
            for transport in transports:
                transport.close()

        self.assertEqual(result, {'serverInfo': {'frontendHost': 'https://weave.test'}})
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), 'https://weave.test/graphql')
        self.assertTrue(requests[0].headers['authorization'].startswith('Basic '))

    @unittest.skipUnless(importlib.util.find_spec('weave'), 'optional Weave SDK is not installed')
    async def test_installed_sdk_accepts_initializer_and_exporter_contract(self):
        import weave
        from weave.trace.weave_client import WeaveClient

        self.configured()
        client = create_autospec(WeaveClient, instance=True)
        client.create_call.return_value = SimpleNamespace(summary={})
        completed = threading.Event()
        client.finish_call.side_effect = lambda *args, **kwargs: completed.set()
        sdk = SimpleNamespace(init=create_autospec(weave.init, return_value=client))
        try:
            with patch.object(tracing.importlib, 'import_module', return_value=sdk):
                await tracing.initialize_weave()
            trace = tracing.start_provider_trace(
                provider='test',
                operation='test.chat',
                url='https://example.test',
                payload={'model': 'test'},
            )
            trace.finish({'answer': 'captured'})
            self.assertTrue(await asyncio.to_thread(completed.wait, 1), 'installed SDK rejected call parameters')
            client.create_call.assert_called_once()
            client.finish_call.assert_called_once()
        finally:
            await tracing.shutdown_weave()
        self.assertEqual(client.flush.call_count, 2)

    @unittest.skipUnless(importlib.util.find_spec('weave'), 'optional Weave SDK is not installed')
    async def test_installed_sdk_settings_survive_export_worker_thread(self):
        from weave.trace import settings as sdk_settings

        self.configured()
        completed = threading.Event()
        observed = {}
        initialization_thread = []

        def current_settings():
            return {
                'capture_code': sdk_settings.should_capture_code(),
                'implicitly_patch_integrations': sdk_settings.should_implicitly_patch_integrations(),
                'retry_max_attempts': sdk_settings.retry_max_attempts(),
                'http_timeout': sdk_settings.http_timeout(),
                'max_calls_queue_size': sdk_settings.max_calls_queue_size(),
                'enable_disk_fallback': sdk_settings.should_enable_disk_fallback(),
            }

        caller_settings = current_settings()

        class SettingsInspectingClient:
            def create_call(self, operation, **kwargs):
                observed['create'] = current_settings()
                observed['worker_thread'] = threading.get_ident()
                return SimpleNamespace(summary={})

            def finish_call(self, call, **kwargs):
                observed['finish'] = current_settings()
                completed.set()

            def flush(self):
                observed['flush'] = current_settings()

        def initialize_fake(project, *, settings):
            # Use the actual SDK's ContextVars, while replacing all network operations.
            initialization_thread.append(threading.get_ident())
            sdk_settings.UserSettings(**settings).apply()
            return SettingsInspectingClient()

        try:
            with patch.object(tracing.importlib, 'import_module', return_value=SimpleNamespace(init=initialize_fake)):
                await tracing.initialize_weave()
            trace = tracing.start_provider_trace(
                provider='test',
                operation='test.chat',
                url='https://example.test',
                payload={'model': 'test'},
            )
            trace.finish({'answer': 'captured'})
            self.assertTrue(await asyncio.to_thread(completed.wait, 1), 'export worker did not process request')
        finally:
            await tracing.shutdown_weave()

        expected = {
            'capture_code': False,
            'implicitly_patch_integrations': False,
            'retry_max_attempts': 1,
            'http_timeout': 5.0,
            'max_calls_queue_size': 64,
            'enable_disk_fallback': False,
        }
        for operation in ('create', 'finish', 'flush'):
            with self.subTest(operation=operation):
                self.assertEqual(observed[operation], expected)
        self.assertNotEqual(observed['worker_thread'], initialization_thread[0])
        self.assertNotEqual(observed['worker_thread'], threading.get_ident())
        self.assertEqual(current_settings(), caller_settings, 'SDK init must not alter caller ContextVars')

    async def test_full_export_queue_drops_logs_without_blocking_chat(self):
        entered = threading.Event()
        release = threading.Event()
        delivered = []

        class SlowClient:
            def create_call(self, operation, **kwargs):
                entered.set()
                release.wait(timeout=5)
                return SimpleNamespace(summary={})

            def finish_call(self, call, **kwargs):
                delivered.append(kwargs['output'])

            def flush(self):
                pass

        tracing._exporter = tracing._Exporter(SlowClient())

        def finish_requests(count):
            for index in range(count):
                trace = tracing.start_provider_trace(
                    provider='test',
                    operation='test.chat',
                    url='https://example.test',
                    payload={},
                )
                trace.finish({'index': index})

        try:
            finish_requests(1)
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            with self.assertLogs(tracing.log, level='WARNING'):
                await asyncio.wait_for(asyncio.to_thread(finish_requests, 100), timeout=1)
        finally:
            release.set()
            await tracing.shutdown_weave()
        self.assertGreater(len(delivered), 1)
        self.assertLess(len(delivered), 101, 'unavailable collector must not create an unbounded backlog')

    async def test_blocked_sdk_flush_cannot_build_an_unbounded_sdk_backlog(self):
        flushing = threading.Event()
        release = threading.Event()

        class BufferedClient:
            def __init__(self):
                self.created = 0
                self.finished = 0

            def create_call(self, operation, **kwargs):
                self.created += 1
                return SimpleNamespace(summary={})

            def finish_call(self, call, **kwargs):
                self.finished += 1

            def flush(self):
                flushing.set()
                release.wait(timeout=5)

        client = BufferedClient()
        tracing._exporter = tracing._Exporter(client)

        def finish_requests(count):
            for index in range(count):
                trace = tracing.start_provider_trace(
                    provider='test',
                    operation='test.chat',
                    url='https://example.test',
                    payload={},
                )
                trace.finish({'index': index})

        try:
            finish_requests(1)
            self.assertTrue(await asyncio.to_thread(flushing.wait, 1), 'worker did not flush its first trace')
            with self.assertLogs(tracing.log, level='WARNING'):
                await asyncio.wait_for(asyncio.to_thread(finish_requests, 100), timeout=1)
            self.assertEqual(client.created, 1, 'pending SDK futures must stop the worker from dequeuing more traces')
            self.assertEqual(client.finished, 1)
            self.assertEqual(tracing._exporter.pending.qsize(), 64)
        finally:
            release.set()
            await tracing.shutdown_weave()
        self.assertEqual(client.created, 65)
        self.assertEqual(client.finished, 65)

    async def test_worker_recovers_after_sdk_export_failure_without_logging_credentials(self):
        finished = threading.Event()

        class FlakyClient:
            def __init__(self):
                self.calls = 0

            def create_call(self, operation, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError('credential=DO-NOT-LOG')
                return SimpleNamespace(summary={})

            def finish_call(self, call, **kwargs):
                finished.set()

            def flush(self):
                pass

        tracing._exporter = tracing._Exporter(FlakyClient())
        try:
            with self.assertLogs(tracing.log, level='WARNING') as logs:
                for index in range(2):
                    trace = tracing.start_provider_trace(
                        provider='test',
                        operation='test.chat',
                        url='https://example.test',
                        payload={},
                    )
                    trace.finish({'index': index})
                self.assertTrue(await asyncio.to_thread(finished.wait, 1), 'export worker died after SDK error')
            self.assertNotIn('DO-NOT-LOG', '\n'.join(logs.output))
        finally:
            await tracing.shutdown_weave()


if __name__ == '__main__':
    unittest.main()
