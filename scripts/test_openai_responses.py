#!/usr/bin/env python3
"""
Test POST /v1/responses (same wire as Codex: wire_api = "responses").

Codex config.toml uses:
  base_url = "https://host/codex/v1"   # must end at /v1, NOT /v1/responses
  wire_api = "responses"

Run:
  export OPENAI_BASE_URL="https://new.sharedchat.cc/codex/v1"
  export OPENAI_API_KEY="sk-..."
  export RESPONSES_MODEL="gpt-5.5"
  uv run python scripts/test_responses_api.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI


def normalize_base_url(url: str) -> str:
	"""OpenAI SDK appends /responses; strip if user copied full endpoint from docs."""
	u = url.strip().rstrip('/')
	if u.endswith('/responses'):
		u = u[: -len('/responses')]
	u = u.rstrip('/')
	# Codex providers almost always want base_url ending in /v1.
	# If user provides a root like https://host/codex, make it https://host/codex/v1
	if not u.endswith('/v1') and '/v1/' not in (u + '/'):
		u = f'{u}/v1'
	return u.rstrip('/')


def main() -> int:
	load_dotenv()
	base_url = normalize_base_url(os.environ.get('OPENAI_BASE_URL', '').strip())
	api_key = os.environ.get('OPENAI_API_KEY', '').strip()
	model = os.environ.get('RESPONSES_MODEL', 'octopus-codex').strip()
	use_stream = os.environ.get('RESPONSES_STREAM', '1').strip() not in ('0', 'false', 'no')

	if not base_url or not api_key:
		raise SystemExit('Set OPENAI_BASE_URL (…/v1) and OPENAI_API_KEY.')

	# SDK posts to {base_url}/responses
	print(f'endpoint: {base_url}/responses  model={model}  stream={use_stream}')

	default_headers = {
		# Some proxies/WAFs block unknown clients; give them a stable UA.
		'User-Agent': os.environ.get('OPENAI_USER_AGENT', 'codex-cli/compat-test'),
	}
	client = OpenAI(base_url=base_url, api_key=api_key, default_headers=default_headers)

	try:
		if use_stream:
			text: list[str] = []
			with client.responses.create(model=model, input='Reply with exactly: OK', stream=True) as stream:
				for event in stream:
					if event.type == 'response.output_text.delta':
						delta = getattr(event, 'delta', '') or ''
						print(delta, end='', flush=True)
						text.append(delta)
			print()
			if not text:
				print('No stream deltas (proxy may not support streaming).', file=sys.stderr)
				return 1
			return 0

		resp = client.responses.create(model=model, input='Reply with exactly: OK')
		print(resp.output_text)
		return 0
	except Exception as exc:
		print(f'error: {exc}', file=sys.stderr)
		body = getattr(getattr(exc, 'response', None), 'text', None)
		if body:
			print(f'body: {body[:2000]}', file=sys.stderr)
		return 1


if __name__ == '__main__':
	sys.exit(main())
