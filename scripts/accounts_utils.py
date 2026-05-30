"""Shared helpers for local account configuration."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

_API_USER_PATTERN = re.compile(r'\d{5,10}')
_GOB_ID_MARKER = b'id\x03int\x04'
_GOB_ID_PREFIX = b'\x05\x00\xfd\x05'


def decode_urlsafe_base64(value: str) -> bytes:
	normalized = value.replace('-', '+').replace('_', '/')
	padding = '=' * ((4 - len(normalized) % 4) % 4)
	return base64.b64decode(normalized + padding)


def _extract_gob_user_id(payload: bytes) -> int | None:
	"""Read the numeric user id from the gob-encoded session payload."""
	idx = payload.find(_GOB_ID_MARKER)
	if idx < 0:
		return None

	pos = idx + len(_GOB_ID_MARKER)
	if pos + 6 > len(payload) or payload[pos : pos + 4] != _GOB_ID_PREFIX:
		return None

	b4, b5 = payload[pos + 4], payload[pos + 5]
	return 163840 + (b4 * 128) + (b5 // 2)


def extract_api_user_from_session(session: str) -> str:
	"""Extract api_user (New-Api-User) from a session cookie."""
	session = session.strip()
	if not session:
		raise ValueError('Session cookie is empty')

	try:
		decoded = decode_urlsafe_base64(session)
		parts = decoded.split(b'|')
		if len(parts) < 3:
			raise ValueError('Session structure is invalid')

		payload_bytes = decode_urlsafe_base64(parts[1].decode('ascii'))
	except (ValueError, UnicodeDecodeError) as exc:
		raise ValueError(f'Failed to decode session cookie: {exc}') from exc

	user_id = _extract_gob_user_id(payload_bytes)
	if user_id is not None:
		return str(user_id)

	# Fallback for unexpected payload layouts.
	payload = payload_bytes.decode('utf-8', errors='replace')
	matches = _API_USER_PATTERN.findall(payload)
	if not matches:
		raise ValueError('Could not extract api_user from session cookie')

	return matches[-1]


def get_session_cookie(account: dict[str, Any]) -> str:
	cookies = account.get('cookies')
	if isinstance(cookies, dict):
		session = cookies.get('session')
		if isinstance(session, str) and session.strip():
			return session.strip()
		raise ValueError('cookies.session is required')

	if isinstance(cookies, str) and cookies.strip():
		for part in cookies.split(';'):
			part = part.strip()
			if part.lower().startswith('session='):
				return part.split('=', 1)[1].strip()
		raise ValueError('cookies string must contain session=')

	raise ValueError('cookies must be an object with session or a session= string')


def refresh_api_users(accounts: list[dict[str, Any]]) -> list[str]:
	"""Fill api_user from session for each account. Returns human-readable change notes."""
	changes: list[str] = []

	for index, account in enumerate(accounts, start=1):
		label = account.get('name') or f'Account {index}'
		session = get_session_cookie(account)
		api_user = extract_api_user_from_session(session)
		previous = account.get('api_user')

		if previous != api_user:
			changes.append(f'{label}: api_user {previous or "(missing)"} -> {api_user}')

		account['api_user'] = api_user

	return changes


def load_accounts_file(path: Path) -> list[dict[str, Any]]:
	if not path.is_file():
		raise FileNotFoundError(f'Accounts file not found: {path}')

	try:
		data = json.loads(path.read_text(encoding='utf-8'))
	except json.JSONDecodeError as exc:
		raise ValueError(f'Invalid JSON in {path}: {exc}') from exc

	if not isinstance(data, list):
		raise ValueError('Accounts file must contain a JSON array')

	for index, account in enumerate(data, start=1):
		if not isinstance(account, dict):
			raise ValueError(f'Account {index} must be a JSON object')

		name = account.get('name')
		if name is not None and not name:
			raise ValueError(f'Account {index} has an empty name field')

		if 'cookies' not in account:
			raise ValueError(f'Account {index} missing required field: cookies')

	return data


def save_accounts_file(path: Path, accounts: list[dict[str, Any]]) -> None:
	path.write_text(json.dumps(accounts, indent=4, ensure_ascii=False) + '\n', encoding='utf-8')
