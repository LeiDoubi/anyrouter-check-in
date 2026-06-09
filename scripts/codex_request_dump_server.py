from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 15722
DEFAULT_MODEL = 'gpt-5.4'


def utc_stamp() -> str:
	return datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')


def json_bytes(value: Any) -> bytes:
	return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def pretty_json_bytes(value: Any) -> bytes:
	return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def maybe_json(body: bytes) -> Any | None:
	if not body:
		return None
	try:
		return json.loads(body.decode('utf-8'))
	except (UnicodeDecodeError, json.JSONDecodeError):
		return None


def text_preview(body: bytes, limit: int = 12000) -> str:
	if not body:
		return ''
	text = body[:limit].decode('utf-8', errors='replace')
	if len(body) > limit:
		text += f'\n... truncated {len(body) - limit} bytes ...'
	return text


def response_object(response_id: str, model: str, text: str, *, status: str = 'completed') -> dict[str, Any]:
	message_id = f'msg_{uuid.uuid4().hex}'
	now = int(time.time())
	return {
		'id': response_id,
		'object': 'response',
		'created_at': now,
		'status': status,
		'error': None,
		'incomplete_details': None,
		'instructions': None,
		'max_output_tokens': None,
		'model': model,
		'output': [
			{
				'id': message_id,
				'type': 'message',
				'status': status,
				'role': 'assistant',
				'content': [
					{
						'type': 'output_text',
						'text': text,
						'annotations': [],
					}
				],
			}
		],
		'parallel_tool_calls': True,
		'previous_response_id': None,
		'reasoning': {'effort': None, 'summary': None},
		'store': False,
		'temperature': 1.0,
		'text': {'format': {'type': 'text'}},
		'tool_choice': 'auto',
		'tools': [],
		'top_p': 1.0,
		'truncation': 'disabled',
		'usage': {
			'input_tokens': 1,
			'input_tokens_details': {'cached_tokens': 0},
			'output_tokens': 3,
			'output_tokens_details': {'reasoning_tokens': 0},
			'total_tokens': 4,
		},
		'user': None,
		'metadata': {},
	}


class DumpHandler(BaseHTTPRequestHandler):
	server_version = 'CodexRequestDump/0.1'

	def log_message(self, fmt: str, *args: Any) -> None:
		sys.stderr.write('%s - - [%s] %s\n' % (self.client_address[0], self.log_date_time_string(), fmt % args))

	@property
	def dump_server(self) -> 'DumpServer':
		return self.server  # type: ignore[return-value]

	def do_GET(self) -> None:
		body = self.read_body()
		self.dump_request(body)

		if self.path.rstrip('/') in ('/status', '/v1/status'):
			self.send_json({'ok': True, 'service': 'codex-request-dump', 'port': self.dump_server.server_port})
			return

		if self.path.rstrip('/') in ('/models', '/v1/models'):
			now = int(time.time())
			self.send_json(
				{
					'object': 'list',
					'data': [
						{
							'id': self.dump_server.default_model,
							'object': 'model',
							'created': now,
							'owned_by': 'codex-request-dump',
						}
					],
				}
			)
			return

		self.send_json({'error': {'message': 'not found', 'type': 'not_found'}}, status=HTTPStatus.NOT_FOUND)

	def do_POST(self) -> None:
		body = self.read_body()
		parsed = maybe_json(body)
		self.dump_request(body, parsed_body=parsed)

		if self.path.rstrip('/') in ('/responses', '/v1/responses', '/v1/v1/responses'):
			request = parsed if isinstance(parsed, dict) else {}
			model = str(request.get('model') or self.dump_server.default_model)
			if request.get('stream') is True:
				self.send_stream_response(model)
			else:
				self.send_json_response(model)
			return

		if self.path.rstrip('/') in ('/chat/completions', '/v1/chat/completions'):
			self.send_chat_completion(parsed if isinstance(parsed, dict) else {})
			return

		self.send_json({'error': {'message': 'not found', 'type': 'not_found'}}, status=HTTPStatus.NOT_FOUND)

	def do_OPTIONS(self) -> None:
		body = self.read_body()
		self.dump_request(body)
		self.send_response(HTTPStatus.NO_CONTENT)
		self.send_header('Access-Control-Allow-Origin', '*')
		self.send_header('Access-Control-Allow-Headers', '*')
		self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
		self.send_header('Content-Length', '0')
		self.end_headers()

	def read_body(self) -> bytes:
		if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
			return self.read_chunked_body()

		content_length = self.headers.get('Content-Length')
		if not content_length:
			return b''

		try:
			length = int(content_length)
		except ValueError:
			return b''
		return self.rfile.read(length)

	def read_chunked_body(self) -> bytes:
		chunks: list[bytes] = []
		while True:
			line = self.rfile.readline()
			if not line:
				break
			size_text = line.split(b';', 1)[0].strip()
			try:
				size = int(size_text, 16)
			except ValueError:
				break
			if size == 0:
				while True:
					trailer = self.rfile.readline()
					if trailer in (b'\r\n', b'\n', b''):
						break
				break
			chunks.append(self.rfile.read(size))
			self.rfile.read(2)
		return b''.join(chunks)

	def dump_request(self, body: bytes, *, parsed_body: Any | None = None) -> None:
		request_id = f'{utc_stamp()}_{uuid.uuid4().hex[:8]}'
		base = self.dump_server.out_dir / request_id
		body_path = base.with_suffix('.body')
		json_path = base.with_suffix('.json')

		body_path.write_bytes(body)
		headers = list(self.headers.raw_items())
		record = {
			'id': request_id,
			'timestamp': datetime.now(UTC).isoformat(),
			'client_address': self.client_address[0],
			'client_port': self.client_address[1],
			'requestline': self.requestline,
			'method': self.command,
			'path': self.path,
			'request_version': self.request_version,
			'headers': [{'name': name, 'value': value} for name, value in headers],
			'body': {
				'bytes': len(body),
				'sha256': hashlib.sha256(body).hexdigest(),
				'base64': base64.b64encode(body).decode('ascii'),
				'text_preview': text_preview(body),
				'json': parsed_body if parsed_body is not None else maybe_json(body),
				'file': str(body_path),
			},
		}
		json_path.write_bytes(pretty_json_bytes(record))
		print(f'[{request_id}] {self.command} {self.path} {len(body)} bytes -> {json_path}', flush=True)

	def send_json(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
		payload = json_bytes(value)
		self.send_response(status)
		self.send_header('Content-Type', 'application/json')
		self.send_header('Content-Length', str(len(payload)))
		self.send_header('Cache-Control', 'no-cache')
		self.end_headers()
		self.wfile.write(payload)

	def send_json_response(self, model: str) -> None:
		resp = response_object(f'resp_{uuid.uuid4().hex}', model, 'dump server ok')
		self.send_json(resp)

	def send_chat_completion(self, request: dict[str, Any]) -> None:
		model = str(request.get('model') or self.dump_server.default_model)
		self.send_json(
			{
				'id': f'chatcmpl_{uuid.uuid4().hex}',
				'object': 'chat.completion',
				'created': int(time.time()),
				'model': model,
				'choices': [
					{
						'index': 0,
						'message': {'role': 'assistant', 'content': 'dump server ok'},
						'finish_reason': 'stop',
					}
				],
				'usage': {'prompt_tokens': 1, 'completion_tokens': 3, 'total_tokens': 4},
			}
		)

	def send_sse(self, event_type: str, data: dict[str, Any]) -> None:
		payload = f'event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, separators=(",", ":"))}\n\n'
		self.wfile.write(payload.encode('utf-8'))
		self.wfile.flush()

	def send_stream_response(self, model: str) -> None:
		response_id = f'resp_{uuid.uuid4().hex}'
		message_id = f'msg_{uuid.uuid4().hex}'
		content_index = 0
		output_index = 0
		text = 'dump server ok'
		now = int(time.time())

		in_progress_response = response_object(response_id, model, '', status='in_progress')
		in_progress_response['created_at'] = now
		in_progress_response['output'] = []
		in_progress_response['usage'] = None

		self.send_response(HTTPStatus.OK)
		self.send_header('Content-Type', 'text/event-stream')
		self.send_header('Cache-Control', 'no-cache, no-transform')
		self.send_header('Connection', 'close')
		self.send_header('X-Accel-Buffering', 'no')
		self.end_headers()

		self.send_sse('response.created', {'type': 'response.created', 'response': in_progress_response})
		self.send_sse('response.in_progress', {'type': 'response.in_progress', 'response': in_progress_response})
		self.send_sse(
			'response.output_item.added',
			{
				'type': 'response.output_item.added',
				'output_index': output_index,
				'item': {
					'id': message_id,
					'type': 'message',
					'status': 'in_progress',
					'role': 'assistant',
					'content': [],
				},
			},
		)
		self.send_sse(
			'response.content_part.added',
			{
				'type': 'response.content_part.added',
				'item_id': message_id,
				'output_index': output_index,
				'content_index': content_index,
				'part': {'type': 'output_text', 'text': '', 'annotations': []},
			},
		)
		self.send_sse(
			'response.output_text.delta',
			{
				'type': 'response.output_text.delta',
				'item_id': message_id,
				'output_index': output_index,
				'content_index': content_index,
				'delta': text,
			},
		)
		self.send_sse(
			'response.output_text.done',
			{
				'type': 'response.output_text.done',
				'item_id': message_id,
				'output_index': output_index,
				'content_index': content_index,
				'text': text,
			},
		)
		content_part = {'type': 'output_text', 'text': text, 'annotations': []}
		self.send_sse(
			'response.content_part.done',
			{
				'type': 'response.content_part.done',
				'item_id': message_id,
				'output_index': output_index,
				'content_index': content_index,
				'part': content_part,
			},
		)
		message_item = {
			'id': message_id,
			'type': 'message',
			'status': 'completed',
			'role': 'assistant',
			'content': [content_part],
		}
		self.send_sse(
			'response.output_item.done',
			{
				'type': 'response.output_item.done',
				'output_index': output_index,
				'item': message_item,
			},
		)
		completed_response = response_object(response_id, model, text)
		completed_response['created_at'] = now
		completed_response['output'] = [message_item]
		self.send_sse('response.completed', {'type': 'response.completed', 'response': completed_response})
		self.close_connection = True


class DumpServer(ThreadingHTTPServer):
	def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], out_dir: Path, default_model: str):
		super().__init__(server_address, handler_class)
		self.out_dir = out_dir
		self.default_model = default_model


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Dump Codex/OpenAI-compatible HTTP requests and return minimal mock responses.')
	parser.add_argument('--host', default=DEFAULT_HOST, help=f'listen host, default: {DEFAULT_HOST}')
	parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'listen port, default: {DEFAULT_PORT}')
	parser.add_argument('--out-dir', type=Path, default=Path('codex-request-dumps'), help='directory for request dumps')
	parser.add_argument('--model', default=DEFAULT_MODEL, help=f'model id returned by /v1/models, default: {DEFAULT_MODEL}')
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	out_dir = args.out_dir.expanduser().resolve()
	out_dir.mkdir(parents=True, exist_ok=True)

	server = DumpServer((args.host, args.port), DumpHandler, out_dir, args.model)
	print(f'Codex request dump server listening on http://{args.host}:{args.port}', flush=True)
	print(f'Writing dumps to {out_dir}', flush=True)
	print('Use base_url = "http://127.0.0.1:15722/v1" for local testing.', flush=True)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		print('\nStopping dump server.', flush=True)
	finally:
		server.server_close()
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
