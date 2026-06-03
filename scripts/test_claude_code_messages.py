#!/usr/bin/env python3
"""
Claude Code-style smoke test for a Claude/Anthropic-compatible proxy.

This script is intentionally aligned with real Claude Code traffic observed via MITM:
  - POST /v1/messages?beta=true
  - Authorization: Bearer <token>
  - Anthropic-Beta includes claude-code + 1m flags
  - Uses SSE streaming by default

Env vars:
  - ANTHROPIC_BASE_URL: host root WITHOUT trailing /v1 (e.g. https://anyrouter.top)
  - ANTHROPIC_AUTH_TOKEN (preferred) or ANTHROPIC_API_KEY: token/key
  - CLAUDE_MODEL: model id (no [1m] suffix is sent on the wire)
  - CLAUDE_STREAM: 1 (default) to use SSE, 0 for JSON
  - CLAUDE_ENABLE_1M: 1 to include context-1m beta flag
  - CLAUDE_BETAS: override full Anthropic-Beta header value (comma-separated)
  - CLAUDE_DEBUG: 1 to print redacted headers + URL
  - CLAUDE_MAX_RETRIES: default 2; set 0 to disable retries
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

import httpx
from dotenv import load_dotenv

CONTEXT_1M_BETA = 'context-1m-2025-08-07'
CLAUDE_CODE_BETA = 'claude-code-20250219'


def normalize_base_url(url: str) -> str:
	"""We will call {base_url}/v1/messages; base_url must NOT include /v1."""
	u = url.strip().rstrip('/')
	for suffix in ('/v1/messages', '/messages'):
		if u.endswith(suffix):
			u = u[: -len(suffix)].rstrip('/')
	if u.endswith('/v1'):
		u = u[: -len('/v1')].rstrip('/')
	return u


def resolve_token() -> str:
	for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'):
		value = os.environ.get(key, '').strip()
		if value:
			return value
	raise SystemExit('Set ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY).')


def parse_extra_headers() -> dict[str, str]:
	raw = os.environ.get('CLAUDE_EXTRA_HEADERS_JSON', '').strip()
	if not raw:
		return {}
	try:
		data = json.loads(raw)
	except json.JSONDecodeError as exc:
		raise SystemExit(f'Invalid CLAUDE_EXTRA_HEADERS_JSON: {exc}') from exc
	if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
		raise SystemExit('CLAUDE_EXTRA_HEADERS_JSON must be a JSON object of string->string.')
	return data


def build_anthropic_beta(*, enable_1m: bool) -> str:
	"""
	Default to the minimal Claude Code-compatible betas.
	User can override via CLAUDE_BETAS with the full comma-separated list.
	"""
	override = os.environ.get('CLAUDE_BETAS', '').strip()
	if override:
		return override
	flags = [CLAUDE_CODE_BETA]
	if enable_1m:
		flags.append(CONTEXT_1M_BETA)
	return ','.join(flags)


def iter_sse_data_lines(response: httpx.Response) -> Iterator[str]:
	for line in response.iter_lines():
		if not line.startswith('data:'):
			continue
		payload = line[5:].lstrip()
		if payload:
			yield payload


def extract_stream_text(event: dict[str, Any]) -> str | None:
	# Anthropic streaming commonly emits: {type:"content_block_delta", delta:{text:"..."}}
	if event.get('type') != 'content_block_delta':
		return None
	delta = event.get('delta')
	if not isinstance(delta, dict):
		return None
	text = delta.get('text')
	return text if isinstance(text, str) and text else None


def sleep_backoff(attempt: int) -> None:
	# small deterministic backoff; avoid hammering a flaky upstream
	time.sleep(min(2.0, 0.25 * (2**attempt)))


def main() -> int:
	load_dotenv()
	base_url = normalize_base_url(os.environ.get('ANTHROPIC_BASE_URL', '').strip())
	token = resolve_token()
	model = os.environ.get('CLAUDE_MODEL', 'claude-opus-4-8').strip()
	enable_1m = os.environ.get('CLAUDE_ENABLE_1M', '0').strip() not in ('0', 'false', 'no')
	use_stream = os.environ.get('CLAUDE_STREAM', '1').strip() not in ('0', 'false', 'no')
	max_tokens = int(os.environ.get('CLAUDE_MAX_TOKENS', '64'))
	debug = os.environ.get('CLAUDE_DEBUG', '0').strip() not in ('0', 'false', 'no')
	max_retries = int(os.environ.get('CLAUDE_MAX_RETRIES', '2'))

	if not base_url:
		raise SystemExit('Set ANTHROPIC_BASE_URL (host root, without /v1).')

	endpoint = f'{base_url}/v1/messages'
	params = {'beta': 'true'}

	extra_headers = parse_extra_headers()
	headers: dict[str, str] = {
		'Accept': 'application/json',
		'Authorization': f'Bearer {token}',
		'Content-Type': 'application/json',
		'User-Agent': os.environ.get('CLAUDE_USER_AGENT', 'claude-cli/2.1.160 (external, cli)'),
		'Anthropic-Version': os.environ.get('ANTHROPIC_VERSION', '2023-06-01'),
		'Anthropic-Beta': build_anthropic_beta(enable_1m=enable_1m),
		'X-App': 'cli',
		**extra_headers,
	}

	body: dict[str, Any] = {
		'model': model,
		'max_tokens': max_tokens,
		'stream': use_stream,
		'messages': [{'role': 'user', 'content': 'Reply with exactly: OK'}],
	}

	print(f'endpoint: {endpoint}?beta=true  model={model}  stream={use_stream}  1m={"on" if enable_1m else "off"}')

	client = httpx.Client(http2=True, timeout=60.0)
	try:
		attempt = 0
		while True:
			try:
				if use_stream:
					text_parts: list[str] = []
					with client.stream(
						'POST',
						endpoint,
						params=params,
						headers={**headers, 'Accept': 'text/event-stream'},
						json=body,
					) as resp:
						if debug:
							redacted = dict(resp.request.headers)
							if 'Authorization' in redacted:
								redacted['Authorization'] = '***'
							print(f'[debug] POST {resp.request.url}', file=sys.stderr)
							print(f'[debug] headers={redacted}', file=sys.stderr)
						if resp.is_error:
							raw = resp.read()
							body_text = raw.decode(errors='replace')
							req_id = resp.headers.get('x-oneapi-request-id') or resp.headers.get('X-Oneapi-Request-Id')
							if req_id:
								print(f'x-oneapi-request-id: {req_id}', file=sys.stderr)
							print(f'error: HTTP {resp.status_code}', file=sys.stderr)
							print(f'body: {body_text[:2000]}', file=sys.stderr)
							if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
								sleep_backoff(attempt)
								attempt += 1
								continue
							return 1
						for data_line in iter_sse_data_lines(resp):
							if data_line == '[DONE]':
								break
							try:
								event = json.loads(data_line)
							except json.JSONDecodeError:
								continue
							if isinstance(event, dict) and event.get('type') == 'error':
								print(f'error: {event}', file=sys.stderr)
								return 1
							if isinstance(event, dict):
								chunk = extract_stream_text(event)
								if chunk:
									print(chunk, end='', flush=True)
									text_parts.append(chunk)
					print()
					if not text_parts:
						print('No stream deltas.', file=sys.stderr)
						return 1
					return 0

				resp = client.post(endpoint, params=params, headers=headers, json=body)
				if debug:
					redacted = dict(resp.request.headers)
					if 'Authorization' in redacted:
						redacted['Authorization'] = '***'
					print(f'[debug] POST {resp.request.url}', file=sys.stderr)
					print(f'[debug] headers={redacted}', file=sys.stderr)
				if resp.is_error:
					raise httpx.HTTPStatusError('http error', request=resp.request, response=resp)
				data = resp.json()
				out: list[str] = []
				for block in data.get('content', []):
					if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
						out.append(block['text'])
				print(''.join(out).strip())
				return 0

			except httpx.HTTPStatusError as exc:
				status = exc.response.status_code if exc.response is not None else None
				if exc.response is not None:
					try:
						text = exc.response.text
					except httpx.ResponseNotRead:
						text = exc.response.read().decode(errors='replace')
				else:
					text = str(exc)
				print(f'error: HTTP {status}', file=sys.stderr)
				print(f'body: {text[:2000]}', file=sys.stderr)
				if status not in (429, 500, 502, 503, 504) or attempt >= max_retries:
					return 1
				sleep_backoff(attempt)
				attempt += 1
				continue
	finally:
		client.close()


if __name__ == '__main__':
	sys.exit(main())
