#!/usr/bin/env python3
"""
Smoke-test a Claude/Anthropic-compatible proxy via Messages API (POST /v1/messages).

Environment variables:
  - ANTHROPIC_BASE_URL: proxy root or /v1 base (e.g. https://proxy.example.com OR https://proxy.example.com/v1)
  - ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY: your key/token

Examples:
  export ANTHROPIC_BASE_URL="https://your-proxy.example.com"
  export ANTHROPIC_AUTH_TOKEN="sk-ant-..."
  uv run python scripts/test_claude_messages_api.py -m claude-3-5-sonnet-latest -i "ping"

  # Match Claude Code (bearer, ?beta=true, SSE, full Anthropic-Beta)
  uv run python scripts/test_claude_messages_api.py --claude-code -i "你好"

  # non-streaming (often 503 on beta gateways — prefer default stream)
  uv run python scripts/test_claude_messages_api.py --no-stream

  # just print URL + payload
  uv run python scripts/test_claude_messages_api.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

console = Console()
error_console = Console(stderr=True)

DEFAULT_MODEL = 'claude-opus-4-8'
DEFAULT_INPUT = 'Reply with exactly: OK'
DEFAULT_SYSTEM = 'You are a connectivity test assistant. Be brief.'
CONTEXT_1M_BETA = 'context-1m-2025-08-07'
CLAUDE_CODE_BETA = 'claude-code-20250219'
# From real Claude Code 2.1.160 traffic capture (use with --claude-code or CLAUDE_FULL_BETAS=1)
CLAUDE_CODE_BETAS_CAPTURED = (
	'claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,'
	'redact-thinking-2026-02-12,thinking-token-count-2026-05-13,context-management-2025-06-27,'
	'prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,advisor-tool-2026-03-01,effort-2025-11-24'
)


def normalize_messages_url(base_url: str) -> str:
	raw = base_url.strip().rstrip('/')
	if raw.endswith('/messages'):
		return raw
	if raw.endswith('/v1'):
		return f'{raw}/messages'
	return f'{raw}/v1/messages'


def resolve_base_url(cli_base: str | None) -> str:
	for key in ('ANTHROPIC_BASE_URL', 'CLAUDE_BASE_URL'):
		value = os.environ.get(key, '').strip()
		if value:
			return value
	if cli_base:
		return cli_base.strip()
	raise SystemExit('Missing base URL. Set ANTHROPIC_BASE_URL or pass --base-url.')


def resolve_api_key(cli_key: str | None) -> str:
	for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'):
		value = os.environ.get(key, '').strip()
		if value:
			return value
	if cli_key:
		return cli_key.strip()
	raise SystemExit('Missing key. Set ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) or pass --api-key.')

def build_anthropic_beta(*, enable_1m: bool, full_capture: bool) -> str:
	"""
	Build the Anthropic-Beta header.
	- CLAUDE_BETAS overrides everything (comma-separated list).
	- CLAUDE_FULL_BETAS=1 or --claude-code uses CLAUDE_CODE_BETAS_CAPTURED.
	- Otherwise minimal: claude-code + optional context-1m.
	"""
	override = os.environ.get('CLAUDE_BETAS', '').strip()
	if override:
		return override
	if full_capture or os.environ.get('CLAUDE_FULL_BETAS', '0').strip() not in ('0', 'false', 'no'):
		return CLAUDE_CODE_BETAS_CAPTURED
	flags = [CLAUDE_CODE_BETA]
	if enable_1m:
		flags.append(CONTEXT_1M_BETA)
	return ','.join(flags)


def read_error_body(response: httpx.Response) -> str:
	try:
		return response.text
	except httpx.ResponseNotRead:
		return response.read().decode(errors='replace')


def print_response_meta(response: httpx.Response) -> None:
	for key in ('x-oneapi-request-id', 'X-Oneapi-Request-Id', 'server', 'via'):
		if key in response.headers:
			error_console.print(f'[dim]{key}:[/] {response.headers[key]}')

def sleep_backoff(attempt: int) -> None:
	time.sleep(min(2.0, 0.25 * (2**attempt)))


def build_payload(*, model: str, user_input: str, system: str | None, stream: bool, max_tokens: int) -> dict[str, Any]:
	body: dict[str, Any] = {
		'model': model,
		'max_tokens': max_tokens,
		'messages': [{'role': 'user', 'content': user_input}],
		'stream': stream,
	}
	if system:
		body['system'] = system
	return body


def iter_sse_data_lines(response: httpx.Response) -> Iterator[str]:
	for line in response.iter_lines():
		if not line.startswith('data:'):
			continue
		payload = line[5:].lstrip()
		if payload:
			yield payload


def extract_stream_text(event: dict[str, Any]) -> str | None:
	etype = event.get('type', '')
	if etype == 'content_block_delta':
		delta = event.get('delta')
		if isinstance(delta, dict):
			text = delta.get('text')
			return text if isinstance(text, str) else None
	return None


def run_stream(client: httpx.Client, url: str, headers: dict[str, str], params: dict[str, str] | None, body: dict[str, Any]) -> int:
	console.print(f'[dim]POST[/] {url}  [dim]stream=true[/]  [cyan]model={body["model"]}[/]')
	text_parts: list[str] = []

	with client.stream('POST', url, params=params, headers={**headers, 'Accept': 'text/event-stream'}, json=body) as response:
		if response.is_error:
			raw = response.read()
			error_console.print(f'[red]HTTP {response.status_code}[/] {response.request.method} {response.request.url}')
			print_response_meta(response)
			try:
				error_console.print_json(data=json.loads(raw))
			except json.JSONDecodeError:
				error_console.print(raw.decode(errors='replace')[:2000])
			return 1

		console.print('[bold]Assistant[/] ', end='')
		for data_line in iter_sse_data_lines(response):
			if data_line == '[DONE]':
				break
			try:
				event = json.loads(data_line)
			except json.JSONDecodeError:
				error_console.print(f'[yellow]non-JSON SSE:[/] {data_line[:160]}')
				continue

			if not isinstance(event, dict):
				continue

			if event.get('type') == 'error':
				error_console.print_json(data=event)
				return 1

			chunk = extract_stream_text(event)
			if chunk:
				print(chunk, end='', flush=True)
				text_parts.append(chunk)

	print()
	if not text_parts:
		error_console.print('[yellow]No text deltas received. Proxy may use a different streaming shape.[/]')
		return 1
	console.print('[green]Stream OK[/]')
	return 0


def run_json(client: httpx.Client, url: str, headers: dict[str, str], params: dict[str, str] | None, body: dict[str, Any]) -> int:
	console.print(f'[dim]POST[/] {url}  [dim]stream=false[/]  [cyan]model={body["model"]}[/]')
	resp = client.post(url, params=params, headers=headers, json=body)
	if resp.is_error:
		error_console.print(f'[red]HTTP {resp.status_code}[/] {resp.request.method} {resp.request.url}')
		print_response_meta(resp)
		try:
			error_console.print_json(data=resp.json())
		except json.JSONDecodeError:
			error_console.print(read_error_body(resp)[:2000])
		return 1

	data = resp.json()
	# Typical non-streaming response: { content: [{type:"text", text:"..."}], ... }
	text_parts: list[str] = []
	for block in data.get('content', []):
		if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
			text_parts.append(block['text'])
	text = ''.join(text_parts).strip()
	console.print(Panel(text or '(no text blocks in response)', title='Assistant', border_style='green'))
	if not text:
		console.print_json(data=data)
		error_console.print('[yellow]No content text blocks found.[/]')
		return 1
	console.print('[green]Request OK[/]')
	return 0


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='Test POST /v1/messages on your Claude/Anthropic-compatible proxy.')
	parser.add_argument('--base-url', help='Proxy root or /v1 base (env: ANTHROPIC_BASE_URL)')
	parser.add_argument('--api-key', help='Key/token (env: ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY)')
	parser.add_argument('-m', '--model', default=os.environ.get('CLAUDE_MODEL', DEFAULT_MODEL))
	parser.add_argument('-i', '--input', default=DEFAULT_INPUT)
	parser.add_argument('--system', default=os.environ.get('CLAUDE_SYSTEM', DEFAULT_SYSTEM))
	parser.add_argument('--no-system', action='store_true', help='Do not send system field')
	parser.add_argument(
		'--auth-mode',
		choices=['bearer', 'x-api-key'],
		default=os.environ.get('CLAUDE_AUTH_MODE', 'x-api-key'),
		help='Auth header mode. Claude Code uses bearer.',
	)
	parser.add_argument('--beta', action='store_true', default=os.environ.get('CLAUDE_BETA', '0') not in ('0', 'false', 'no'))
	parser.add_argument(
		'--claude-code',
		action='store_true',
		help='Shorthand: bearer + beta + stream + full Anthropic-Beta capture + CC extra headers',
	)
	parser.add_argument('--enable-1m', action='store_true', default=os.environ.get('CLAUDE_ENABLE_1M', '0') not in ('0', 'false', 'no'))
	parser.add_argument('--user-agent', default=os.environ.get('CLAUDE_USER_AGENT', 'claude-cli/2.1.160 (external, cli)'))
	parser.add_argument('--x-app', default=os.environ.get('CLAUDE_X_APP', 'cli'))
	parser.add_argument('--debug', action='store_true', default=os.environ.get('CLAUDE_DEBUG', '0') not in ('0', 'false', 'no'))
	parser.add_argument('--max-retries', type=int, default=int(os.environ.get('CLAUDE_MAX_RETRIES', '2')))
	parser.add_argument('--no-stream', action='store_true', help='Use non-streaming JSON response')
	parser.add_argument('--max-tokens', type=int, default=int(os.environ.get('CLAUDE_MAX_TOKENS', '128')))
	parser.add_argument('--timeout', type=float, default=120.0)
	parser.add_argument('--dry-run', action='store_true')
	return parser.parse_args()


def main() -> int:
	load_dotenv()
	args = parse_args()

	base_url = resolve_base_url(args.base_url)
	api_key = resolve_api_key(args.api_key)
	url = normalize_messages_url(base_url)

	if args.claude_code:
		args.auth_mode = 'bearer'
		args.beta = True
		if not args.enable_1m and os.environ.get('CLAUDE_ENABLE_1M', '').strip() not in ('1', 'true', 'yes'):
			args.enable_1m = True

	claude_code_mode = args.claude_code or args.beta
	stream = not args.no_stream
	if claude_code_mode and args.no_stream:
		error_console.print(
			'[yellow]Warning:[/] --no-stream with --beta often returns 503 on some proxies; omit --no-stream to match Claude Code'
		)
	system = None if args.no_system else args.system
	body = build_payload(model=args.model, user_input=args.input, system=system, stream=stream, max_tokens=args.max_tokens)

	params = {'beta': 'true'} if (args.beta or args.claude_code) else None

	full_betas = args.claude_code or os.environ.get('CLAUDE_FULL_BETAS', '0').strip() not in ('0', 'false', 'no')
	headers: dict[str, str] = {
		'Content-Type': 'application/json',
		'Anthropic-Version': os.environ.get('ANTHROPIC_VERSION', '2023-06-01'),
		'User-Agent': args.user_agent,
		'X-App': args.x_app,
		'Anthropic-Beta': build_anthropic_beta(enable_1m=args.enable_1m, full_capture=full_betas),
	}
	if claude_code_mode:
		headers['Anthropic-Dangerous-Direct-Browser-Access'] = 'true'
		session_id = os.environ.get('CLAUDE_SESSION_ID', '').strip()
		if session_id:
			headers['X-Claude-Code-Session-Id'] = session_id

	if args.auth_mode == 'bearer' or args.claude_code:
		headers['Authorization'] = f'Bearer {api_key}'
	else:
		headers['x-api-key'] = api_key

	if args.dry_run:
		console.print(f'[bold]URL[/] {url}')
		if params:
			console.print(f'[bold]Query[/] {params}')
		redacted = dict(headers)
		if 'Authorization' in redacted:
			redacted['Authorization'] = '***'
		if 'x-api-key' in redacted:
			redacted['x-api-key'] = '***'
		console.print_json(data=redacted)
		console.print_json(data=body)
		return 0

	client = httpx.Client(http2=True, timeout=args.timeout)
	try:
		attempt = 0
		while True:
			try:
				if args.debug:
					redacted = dict(headers)
					if 'Authorization' in redacted:
						redacted['Authorization'] = '***'
					if 'x-api-key' in redacted:
						redacted['x-api-key'] = '***'
					error_console.print(f'[dim]debug url:[/] {url} params={params}')
					error_console.print_json(data=redacted)

				if stream:
					return run_stream(client, url, headers, params, body)
				return run_json(client, url, headers, params, body)
			except httpx.HTTPStatusError as exc:
				status = exc.response.status_code if exc.response is not None else None
				if exc.response is not None:
					print_response_meta(exc.response)
					text = read_error_body(exc.response)
				else:
					text = str(exc)
				error_console.print(f'[red]HTTP {status}[/] {text[:2000]}')
				if status not in (429, 500, 502, 503, 504) or attempt >= args.max_retries:
					return 1
				sleep_backoff(attempt)
				attempt += 1
	finally:
		client.close()


if __name__ == '__main__':
	sys.exit(main())

