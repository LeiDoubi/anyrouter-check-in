#!/usr/bin/env python3
"""
Push local config to GitHub Actions secrets in the production environment.

- ANYROUTER_ACCOUNTS from .accounts.json
- Notification secrets from .env (e.g. FEISHU_WEBHOOK)

When cookies.session is updated locally, api_user is auto-extracted from the session
cookie using the same logic as https://milly.me/anyrouter-check-in/

Requires the GitHub CLI (gh) to be installed and authenticated.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from accounts_utils import load_accounts_file, refresh_api_users, save_accounts_file

DEFAULT_ACCOUNTS_FILE = Path('.accounts.json')
DEFAULT_ENV_FILE = Path('.env')
DEFAULT_ENVIRONMENT = 'production'
SECRET_NAME = 'ANYROUTER_ACCOUNTS'
ENV_SECRET_NAMES = (
	'DINGDING_WEBHOOK',
	'EMAIL_USER',
	'EMAIL_PASS',
	'EMAIL_TO',
	'EMAIL_SENDER',
	'CUSTOM_SMTP_SERVER',
	'PUSHPLUS_TOKEN',
	'SERVERPUSHKEY',
	'FEISHU_WEBHOOK',
	'WEIXIN_WEBHOOK',
	'TELEGRAM_BOT_TOKEN',
	'TELEGRAM_CHAT_ID',
	'GOTIFY_URL',
	'GOTIFY_TOKEN',
	'GOTIFY_PRIORITY',
	'BARK_KEY',
	'BARK_SERVER',
	'PROVIDERS',
)
_GITHUB_REPO_PATTERN = re.compile(r'github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$')


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description='Push .accounts.json and .env secrets to GitHub Actions via gh CLI.',
	)
	parser.add_argument(
		'-f',
		'--file',
		type=Path,
		default=DEFAULT_ACCOUNTS_FILE,
		help=f'Accounts JSON file (default: {DEFAULT_ACCOUNTS_FILE})',
	)
	parser.add_argument(
		'--env-file',
		type=Path,
		default=DEFAULT_ENV_FILE,
		help=f'Dotenv file for notification secrets (default: {DEFAULT_ENV_FILE})',
	)
	parser.add_argument(
		'-e',
		'--env',
		default=DEFAULT_ENVIRONMENT,
		help=f'GitHub Actions environment name (default: {DEFAULT_ENVIRONMENT})',
	)
	parser.add_argument(
		'-R',
		'--repo',
		help='Target repository in OWNER/REPO format (default: origin remote)',
	)
	parser.add_argument(
		'--write',
		action='store_true',
		help='Write refreshed api_user values back to the accounts file',
	)
	parser.add_argument(
		'--no-auto-api-user',
		action='store_true',
		help='Do not auto-extract api_user from cookies.session',
	)
	parser.add_argument(
		'--dry-run',
		action='store_true',
		help='Validate and preview changes without updating GitHub secret',
	)
	return parser.parse_args()


def compact_json(data: list[dict[str, Any]]) -> str:
	checkin_fields = ('name', 'cookies', 'api_user', 'provider')
	payload = [{key: account[key] for key in checkin_fields if key in account} for account in data]
	return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)


def ensure_gh_available() -> str:
	gh = shutil.which('gh')
	if gh is None:
		raise RuntimeError('gh CLI not found. Install it from https://cli.github.com/')
	return gh


def parse_github_repo_url(url: str) -> str | None:
	match = _GITHUB_REPO_PATTERN.search(url.strip())
	if not match:
		return None
	return f'{match.group(1)}/{match.group(2)}'


def resolve_repo(explicit_repo: str | None) -> str:
	if explicit_repo:
		return explicit_repo

	result = subprocess.run(
		['git', 'remote', 'get-url', 'origin'],
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode == 0:
		repo = parse_github_repo_url(result.stdout)
		if repo:
			return repo

	raise RuntimeError(
		'Could not determine target repository from origin remote. '
		'Pass -R owner/repo explicitly.'
	)


def push_named_secret(gh: str, name: str, secret_value: str, environment: str, repo: str) -> None:
	command = [gh, 'secret', 'set', name, '--env', environment, '--repo', repo]

	result = subprocess.run(command, input=secret_value, capture_output=True, text=True)
	if result.returncode != 0:
		message = result.stderr.strip() or result.stdout.strip() or 'unknown error'
		raise RuntimeError(f'Failed to update GitHub secret {name}: {message}')


def load_env_secrets(path: Path) -> dict[str, str]:
	if not path.is_file():
		return {}

	secrets: dict[str, str] = {}
	for raw_line in path.read_text(encoding='utf-8').splitlines():
		line = raw_line.strip()
		if not line or line.startswith('#'):
			continue

		key, separator, value = line.partition('=')
		if not separator:
			continue

		name = key.strip()
		if name not in ENV_SECRET_NAMES:
			continue

		clean_value = value.strip().strip('"').strip("'")
		if clean_value:
			secrets[name] = clean_value

	return secrets


def summarize_accounts(accounts: list[dict[str, Any]]) -> str:
	labels = []
	for index, account in enumerate(accounts):
		name = account.get('name') or f'Account {index + 1}'
		provider = account.get('provider', 'anyrouter')
		api_user = account.get('api_user', '?')
		labels.append(f'{name} ({provider}, api_user={api_user})')
	return ', '.join(labels)


def validate_accounts(accounts: list[dict[str, Any]], auto_api_user: bool) -> None:
	for index, account in enumerate(accounts, start=1):
		if not auto_api_user and not account.get('api_user'):
			raise ValueError(f'Account {index} missing required field: api_user')


def main() -> int:
	args = parse_args()
	auto_api_user = not args.no_auto_api_user

	try:
		accounts = load_accounts_file(args.file)
		validate_accounts(accounts, auto_api_user)

		changes: list[str] = []
		if auto_api_user:
			changes = refresh_api_users(accounts)

		secret_value = compact_json(accounts)
		env_secrets = load_env_secrets(args.env_file)
		repo = resolve_repo(args.repo)
	except (FileNotFoundError, ValueError, RuntimeError) as exc:
		print(f'ERROR: {exc}', file=sys.stderr)
		return 1

	summary = summarize_accounts(accounts)
	env_secret_names = sorted(env_secrets)

	if changes:
		print('Refreshed api_user from session cookie:')
		for change in changes:
			print(f'  - {change}')
	elif auto_api_user:
		print('api_user already matches session cookie')

	if args.write and not args.dry_run:
		save_accounts_file(args.file, accounts)
		print(f'Updated {args.file}')

	if args.dry_run:
		print(f'DRY RUN: would push {len(accounts)} account(s) to {SECRET_NAME}')
		if env_secret_names:
			print(f'DRY RUN: would push env secrets: {", ".join(env_secret_names)}')
		else:
			print(f'DRY RUN: no env secrets found in {args.env_file}')
		print(f'Environment: {args.env}')
		print(f'Repository: {repo}')
		print(f'Accounts: {summary}')
		return 0

	try:
		gh = ensure_gh_available()
		push_named_secret(gh, SECRET_NAME, secret_value, args.env, repo)
		print(f'Updated secret {SECRET_NAME} in environment {args.env} ({repo})')
		print(f'Accounts: {summary}')

		for name in env_secret_names:
			push_named_secret(gh, name, env_secrets[name], args.env, repo)
			print(f'Updated secret {name} in environment {args.env} ({repo})')
	except RuntimeError as exc:
		print(f'ERROR: {exc}', file=sys.stderr)
		return 1

	return 0


if __name__ == '__main__':
	raise SystemExit(main())
