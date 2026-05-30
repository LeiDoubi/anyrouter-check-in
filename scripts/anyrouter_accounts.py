#!/usr/bin/env python3
"""
Interactive AnyRouter account manager for Claude Code and Codex.

Reads .accounts.json, lists account balances (same API as checkin.py), and switches
the active auth_token for local Claude Code / Codex usage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts_utils import load_accounts_file, refresh_api_users

from checkin import get_user_info, parse_cookies, prepare_cookies
from utils.config import AccountConfig, AppConfig

console = Console()
error_console = Console(stderr=True)

STATE_DIR = Path.home() / '.config' / 'anyrouter-check-in'
DEFAULT_ACCOUNTS_FILE_NAME = '.accounts.json'
CONFIG_ACCOUNTS_FILE = STATE_DIR / 'accounts.json'
ACTIVE_STATE_FILE = STATE_DIR / 'active.json'
ENV_EXPORT_FILE = STATE_DIR / 'env.sh'
CODEX_DIR = Path.home() / '.codex'
CODEX_AUTH_FILE = CODEX_DIR / 'auth.json'
CODEX_CONFIG_FILE = CODEX_DIR / 'config.toml'
DEFAULT_BASE_URL = 'https://anyrouter.top'
CODEX_CONFIG_TEMPLATE = """model = "gpt-5-codex"
model_provider = "anyrouter"
preferred_auth_method = "apikey"

[model_providers.anyrouter]
name = "Any Router"
base_url = "{base_url}/v1"
wire_api = "responses"
"""
ZSHRC_HOOK_START = '# >>> anyrouter-check-in >>>'
ZSHRC_HOOK_END = '# <<< anyrouter-check-in <<<'
ZSHRC_FILE = Path.home() / '.zshrc'


@dataclass
class AccountBalance:
	index: int
	name: str
	provider: str
	quota: float | None
	used_quota: float | None
	error: str | None = None
	is_active: bool = False
	has_auth_token: bool = False

	@property
	def quota_ok(self) -> bool:
		return self.quota is not None


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description='List balances and switch AnyRouter accounts locally.')
	parser.add_argument(
		'-f',
		'--file',
		type=Path,
		default=None,
		help=(
			'Accounts JSON file. Defaults to ANYROUTER_ACCOUNTS_FILE, '
			f'./{DEFAULT_ACCOUNTS_FILE_NAME}, {CONFIG_ACCOUNTS_FILE}, then project ./{DEFAULT_ACCOUNTS_FILE_NAME}.'
		),
	)
	parser.add_argument(
		'--plain',
		action='store_true',
		help='Plain text output (for scripting)',
	)
	subparsers = parser.add_subparsers(dest='command')

	subparsers.add_parser('list', help='List accounts and balances')
	subparsers.add_parser('current', help='Show active account')
	subparsers.add_parser('apply', help='Re-apply the active account without switching')

	switch_parser = subparsers.add_parser('switch', help='Switch to an account by index')
	switch_parser.add_argument('index', type=int, nargs='?', help='Account index (1-based)')

	subparsers.add_parser('env', help='Print shell exports for the active account')
	subparsers.add_parser('interactive', help='Interactive account switcher (default)')

	list_parser = subparsers.add_parser('refresh', help='Alias for list')
	list_parser.set_defaults(command='list')

	return parser.parse_args()


def resolve_accounts_file(path: Path | None) -> Path:
	if path is not None:
		return path.expanduser()

	env_path = os.environ.get('ANYROUTER_ACCOUNTS_FILE')
	if env_path:
		return Path(env_path).expanduser()

	candidates = [
		Path.cwd() / DEFAULT_ACCOUNTS_FILE_NAME,
		CONFIG_ACCOUNTS_FILE,
		PROJECT_ROOT / DEFAULT_ACCOUNTS_FILE_NAME,
	]
	for candidate in candidates:
		if candidate.is_file():
			return candidate

	return CONFIG_ACCOUNTS_FILE


def load_accounts(path: Path) -> list[dict[str, Any]]:
	accounts = load_accounts_file(path)
	refresh_api_users(accounts)
	return accounts


def load_active_state() -> dict[str, Any] | None:
	if not ACTIVE_STATE_FILE.is_file():
		return None
	try:
		return json.loads(ACTIVE_STATE_FILE.read_text(encoding='utf-8'))
	except json.JSONDecodeError:
		return None


def save_active_state(index: int, name: str) -> None:
	STATE_DIR.mkdir(parents=True, exist_ok=True)
	payload = {'index': index, 'name': name}
	ACTIVE_STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def resolve_base_url(provider_name: str, app_config: AppConfig) -> str:
	provider = app_config.get_provider(provider_name)
	if provider is None:
		return DEFAULT_BASE_URL
	return provider.domain.rstrip('/')


def get_auth_token(account: dict[str, Any]) -> str:
	token = account.get('auth_token')
	if not isinstance(token, str) or not token.strip():
		raise ValueError(f'Account "{account.get("name", "unknown")}" missing auth_token')
	return token.strip()


@contextlib.contextmanager
def suppress_checkin_logs():
	buffer = io.StringIO()
	with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
		yield


async def fetch_account_balance(
	account: dict[str, Any],
	index: int,
	app_config: AppConfig,
	active_index: int | None,
) -> AccountBalance:
	name = account.get('name') or f'Account {index + 1}'
	provider_name = account.get('provider', 'anyrouter')
	has_auth_token = bool(str(account.get('auth_token', '')).strip())

	try:
		account_cfg = AccountConfig.from_dict(account, index)
	except Exception as exc:
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error=str(exc),
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)

	provider_config = app_config.get_provider(account_cfg.provider)
	if provider_config is None:
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error=f'Unknown provider: {provider_name}',
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)

	user_cookies = parse_cookies(account_cfg.cookies)
	if not user_cookies:
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error='Invalid cookies',
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)

	with suppress_checkin_logs():
		all_cookies = await prepare_cookies(name, provider_config, user_cookies, headless=True)
	if not all_cookies:
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error='Failed to get WAF cookies',
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)

	client = httpx.Client(http2=True, timeout=30.0)
	try:
		client.cookies.update(all_cookies)
		headers = {
			'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
			'Accept': 'application/json, text/plain, */*',
			'Referer': provider_config.domain,
			'Origin': provider_config.domain,
			provider_config.api_user_key: account_cfg.api_user,
		}
		user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
		with suppress_checkin_logs():
			user_info = get_user_info(client, headers, user_info_url)
	except Exception as exc:
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error=str(exc)[:60],
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)
	finally:
		client.close()

	if not user_info.get('success'):
		return AccountBalance(
			index=index,
			name=name,
			provider=provider_name,
			quota=None,
			used_quota=None,
			error=user_info.get('error', 'Failed to fetch balance'),
			is_active=index == active_index,
			has_auth_token=has_auth_token,
		)

	return AccountBalance(
		index=index,
		name=name,
		provider=provider_name,
		quota=user_info['quota'],
		used_quota=user_info['used_quota'],
		is_active=index == active_index,
		has_auth_token=has_auth_token,
	)


async def fetch_all_balances(
	accounts: list[dict[str, Any]],
	active_index: int | None,
	*,
	plain: bool = False,
) -> list[AccountBalance]:
	app_config = AppConfig.load_from_env()
	results: list[AccountBalance] = []

	if plain:
		for index, account in enumerate(accounts):
			label = account.get('name') or f'Account {index + 1}'
			print(f'[INFO] Fetching balance for {label}...')
			results.append(await fetch_account_balance(account, index, app_config, active_index))
		return results

	with Progress(
		SpinnerColumn(style='cyan'),
		TextColumn('[bold cyan]{task.description}'),
		console=console,
		transient=True,
	) as progress:
		for index, account in enumerate(accounts):
			label = account.get('name') or f'Account {index + 1}'
			task = progress.add_task(f'正在查询 {label} 余额...', total=None)
			results.append(await fetch_account_balance(account, index, app_config, active_index))
			progress.remove_task(task)

	return results


def render_header() -> None:
	console.print()
	console.print(
		Panel(
			Text.from_markup(
				'[bold white]AnyRouter 账号切换[/]\n'
				'[dim]Claude Code · Codex · 本地 auth_token 管理[/]'
			),
			border_style='bright_blue',
			padding=(1, 2),
		)
	)


def render_account_table(balances: list[AccountBalance]) -> None:
	table = Table(
		title='账号列表',
		box=box.ROUNDED,
		border_style='bright_black',
		header_style='bold cyan',
		title_style='bold white',
		show_lines=False,
		padding=(0, 1),
	)
	table.add_column('#', justify='right', style='dim', width=3)
	table.add_column('名称', style='bold', min_width=16)
	table.add_column('余额', justify='right', min_width=10)
	table.add_column('已用', justify='right', min_width=10)
	table.add_column('平台', style='dim')
	table.add_column('Token', justify='center', width=6)
	table.add_column('当前', justify='center', width=4)

	for item in balances:
		if item.quota_ok:
			quota_cell = Text(f'${item.quota:.2f}', style='bold green')
			used_cell = Text(f'${item.used_quota:.2f}', style='yellow')
		else:
			error_text = (item.error or 'N/A')[:28]
			quota_cell = Text(error_text, style='bold red')
			used_cell = Text('-', style='dim')

		token_cell = Text('✓', style='green') if item.has_auth_token else Text('✗', style='red')
		current_cell = Text('●', style='bold green') if item.is_active else Text('', style='dim')
		name_style = 'bold white on dark_green' if item.is_active else 'white'

		table.add_row(
			str(item.index + 1),
			Text(item.name, style=name_style),
			quota_cell,
			used_cell,
			item.provider,
			token_cell,
			current_cell,
		)

	console.print(table)
	console.print()


def print_account_table_plain(balances: list[AccountBalance]) -> None:
	name_width = max(len(item.name) for item in balances)
	name_width = max(name_width, len('名称'))
	print()
	print(f' {"#":>2}  {"名称":<{name_width}}  {"余额":>10}  {"已用":>10}  {"平台":<12}  Token  当前')
	print(f' {"-" * 2}  {"-" * name_width}  {"-" * 10}  {"-" * 10}  {"-" * 12}  -----  ----')
	for item in balances:
		quota = f'${item.quota:.2f}' if item.quota_ok else (item.error or 'N/A')
		used = f'${item.used_quota:.2f}' if item.used_quota is not None else '-'
		current = '✓' if item.is_active else ''
		token_flag = 'yes' if item.has_auth_token else 'no'
		print(
			f' {item.index + 1:>2}  {item.name:<{name_width}}  {quota:>10}  '
			f'{used:>10}  {item.provider:<12}  {token_flag:<5}  {current}'
		)
	print()


def ensure_codex_config(base_url: str) -> None:
	CODEX_DIR.mkdir(parents=True, exist_ok=True)
	v1_url = f'{base_url.rstrip("/")}/v1'

	if not CODEX_CONFIG_FILE.is_file():
		CODEX_CONFIG_FILE.write_text(CODEX_CONFIG_TEMPLATE.format(base_url=base_url.rstrip('/')), encoding='utf-8')
		return

	content = CODEX_CONFIG_FILE.read_text(encoding='utf-8')
	updated = re.sub(r'base_url\s*=\s*"[^"]*"', f'base_url = "{v1_url}"', content, count=1)
	if updated != content:
		CODEX_CONFIG_FILE.write_text(updated, encoding='utf-8')


def sync_zshrc_hook() -> list[str]:
	"""Ensure ~/.zshrc sources env.sh and remove conflicting ANTHROPIC exports."""
	actions: list[str] = []
	lines: list[str] = []
	if ZSHRC_FILE.is_file():
		lines = ZSHRC_FILE.read_text(encoding='utf-8').splitlines()

	cleaned: list[str] = []
	skip_block = False
	for line in lines:
		stripped = line.strip()
		if stripped == ZSHRC_HOOK_START:
			skip_block = True
			if 'removed old hook block' not in actions:
				actions.append('removed old hook block')
			continue
		if stripped == ZSHRC_HOOK_END:
			skip_block = False
			continue
		if skip_block:
			continue

		if stripped.startswith('export =export'):
			actions.append(f'removed broken export: {stripped}')
			continue
		if stripped.startswith('export ANTHROPIC_AUTH_TOKEN=') or stripped.startswith('export ANTHROPIC_BASE_URL='):
			actions.append(f'removed conflicting export: {stripped}')
			continue

		cleaned.append(line)

	while cleaned and not cleaned[-1].strip():
		cleaned.pop()

	cleaned.extend(
		[
			'',
			ZSHRC_HOOK_START,
			'# Managed by anyrouter-accounts',
			f'[[ -f "{ENV_EXPORT_FILE}" ]] && source "{ENV_EXPORT_FILE}"',
			ZSHRC_HOOK_END,
			'',
		]
	)
	ZSHRC_FILE.write_text('\n'.join(cleaned), encoding='utf-8')
	actions.append(f'updated {ZSHRC_FILE}')
	return actions


def apply_account(account: dict[str, Any], index: int, app_config: AppConfig) -> tuple[str, list[str]]:
	auth_token = get_auth_token(account)
	provider_name = account.get('provider', 'anyrouter')
	base_url = resolve_base_url(provider_name, app_config)
	name = account.get('name') or f'Account {index + 1}'

	STATE_DIR.mkdir(parents=True, exist_ok=True)
	env_content = (
		f'# Active account: {name}\n'
		f'export ANTHROPIC_AUTH_TOKEN="{auth_token}"\n'
		f'export ANTHROPIC_BASE_URL="{base_url}"\n'
	)
	ENV_EXPORT_FILE.write_text(env_content, encoding='utf-8')

	ensure_codex_config(base_url)
	CODEX_AUTH_FILE.write_text(json.dumps({'OPENAI_API_KEY': auth_token}, indent=2) + '\n', encoding='utf-8')
	save_active_state(index, name)
	zshrc_actions = sync_zshrc_hook()
	return name, zshrc_actions


def render_apply_result(name: str, zshrc_actions: list[str] | None = None) -> None:
	zshrc_note = ''
	if zshrc_actions:
		zshrc_note = '\n[dim]~/.zshrc[/] 已同步（source env.sh，已清理冲突的 ANTHROPIC 变量）\n'

	body = (
		f'[bold green]✓[/] 已切换至 [bold white]{name}[/]\n\n'
		f'[dim]Claude Code[/]  [cyan]{ENV_EXPORT_FILE}[/]\n'
		f'[dim]Codex[/]         [cyan]{CODEX_AUTH_FILE}[/]\n'
		f'{zshrc_note}\n'
		f'[bold]当前终端立即生效：[/]\n'
		f'  [yellow]source {ENV_EXPORT_FILE}[/]\n\n'
		f'[dim]新开终端会自动加载；~/.zshrc 已通过 source env.sh 引用配置[/]'
	)
	console.print(Panel(body, title='切换成功', border_style='green', padding=(1, 2)))


def print_apply_result_plain(name: str, zshrc_actions: list[str] | None = None) -> None:
	print()
	print(f'已切换至: {name}')
	print(f'已更新: {ENV_EXPORT_FILE}')
	print(f'已更新: {CODEX_AUTH_FILE}')
	if zshrc_actions:
		print('已同步 ~/.zshrc')
		for action in zshrc_actions:
			print(f'  - {action}')
	print()
	print('当前终端立即生效:')
	print(f'  source {ENV_EXPORT_FILE}')


async def cmd_list(accounts: list[dict[str, Any]], *, plain: bool = False) -> None:
	active = load_active_state()
	active_index = active['index'] if active and isinstance(active.get('index'), int) else None
	if not plain:
		render_header()
	balances = await fetch_all_balances(accounts, active_index, plain=plain)
	if plain:
		print_account_table_plain(balances)
	else:
		render_account_table(balances)


def cmd_current(accounts: list[dict[str, Any]], *, plain: bool = False) -> None:
	active = load_active_state()
	if not active:
		message = '当前没有激活账号，运行 anyrouter-accounts 进行切换。'
		if plain:
			print(message)
		else:
			console.print(Panel(message, title='当前账号', border_style='yellow'))
		return

	index = active.get('index')
	name = active.get('name', 'unknown')
	if isinstance(index, int) and 0 <= index < len(accounts):
		account = accounts[index]
		token = account.get('auth_token', '')
		masked = f'{token[:7]}...{token[-4:]}' if len(token) > 11 else '(missing)'
		if plain:
			print(f'当前账号: {name} (#{index + 1})')
			print(f'auth_token: {masked}')
			print(f'Claude env: {ENV_EXPORT_FILE}')
			print(f'Codex auth: {CODEX_AUTH_FILE}')
			return

		body = (
			f'[bold white]{name}[/]  [dim](#{index + 1})[/]\n\n'
			f'[dim]auth_token[/]  {masked}\n'
			f'[dim]Claude[/]      [cyan]{ENV_EXPORT_FILE}[/]\n'
			f'[dim]Codex[/]       [cyan]{CODEX_AUTH_FILE}[/]'
		)
		console.print(Panel(body, title='当前账号', border_style='bright_blue', padding=(1, 2)))
	else:
		message = f'当前记录: {name} (#{index})'
		if plain:
			print(message)
		else:
			console.print(Panel(message, title='当前账号', border_style='yellow'))


def cmd_env(accounts: list[dict[str, Any]]) -> int:
	active = load_active_state()
	if not active or not isinstance(active.get('index'), int):
		print('No active account selected.', file=sys.stderr)
		return 1

	index = active['index']
	if index < 0 or index >= len(accounts):
		print('Active account index is out of range.', file=sys.stderr)
		return 1

	account = accounts[index]
	app_config = AppConfig.load_from_env()
	auth_token = get_auth_token(account)
	base_url = resolve_base_url(account.get('provider', 'anyrouter'), app_config)
	print(f'export ANTHROPIC_AUTH_TOKEN="{auth_token}"')
	print(f'export ANTHROPIC_BASE_URL="{base_url}"')
	return 0


async def cmd_switch(accounts: list[dict[str, Any]], index: int | None, *, plain: bool = False) -> int:
	app_config = AppConfig.load_from_env()
	active = load_active_state()
	active_index = active['index'] if active and isinstance(active.get('index'), int) else None

	if index is None:
		if not plain:
			render_header()
		balances = await fetch_all_balances(accounts, active_index, plain=plain)
		if plain:
			print_account_table_plain(balances)
		else:
			render_account_table(balances)

		if plain:
			while True:
				choice = input(f'选择账号 [1-{len(accounts)}]，或 q 退出: ').strip().lower()
				if choice in {'q', 'quit', 'exit'}:
					return 0
				if choice.isdigit():
					selected = int(choice)
					if 1 <= selected <= len(accounts):
						index = selected
						break
				print('输入无效，请重试。')
		else:
			console.print('[dim]输入 q 退出[/]')
			while True:
				choice = Prompt.ask(
					'[bold cyan]选择要切换的账号[/]',
					default='q',
					show_default=False,
				).strip().lower()
				if choice in {'q', 'quit', 'exit', ''}:
					console.print('[dim]已退出[/]')
					return 0
				if choice.isdigit():
					selected = int(choice)
					if 1 <= selected <= len(accounts):
						index = selected
						break
				error_console.print('[red]输入无效，请输入账号编号或 q[/]')
	else:
		if index < 1 or index > len(accounts):
			message = f'index must be between 1 and {len(accounts)}'
			if plain:
				print(f'ERROR: {message}', file=sys.stderr)
			else:
				error_console.print(f'[bold red]ERROR:[/] {message}')
			return 1

	account = accounts[index - 1]
	try:
		name, zshrc_actions = apply_account(account, index - 1, app_config)
	except ValueError as exc:
		if plain:
			print(f'ERROR: {exc}', file=sys.stderr)
		else:
			error_console.print(Panel(str(exc), title='切换失败', border_style='red'))
		return 1

	if plain:
		print_apply_result_plain(name, zshrc_actions)
	else:
		render_apply_result(name, zshrc_actions)
	return 0


async def async_main() -> int:
	args = parse_args()
	command = args.command or 'interactive'
	plain = args.plain or command == 'env'
	accounts_file = resolve_accounts_file(args.file)

	try:
		accounts = load_accounts(accounts_file)
	except (FileNotFoundError, ValueError) as exc:
		if plain:
			print(f'ERROR: {exc}', file=sys.stderr)
		else:
			error_console.print(f'[bold red]ERROR:[/] {exc}')
			error_console.print(
				f'[dim]请将账号文件放到当前目录 ./{DEFAULT_ACCOUNTS_FILE_NAME}，'
				f'或 {CONFIG_ACCOUNTS_FILE}，也可以用 -f 指定。[/]'
			)
		return 1

	if not accounts:
		message = 'no accounts found'
		if plain:
			print(f'ERROR: {message}', file=sys.stderr)
		else:
			error_console.print(f'[bold red]ERROR:[/] {message}')
		return 1

	if command == 'list':
		await cmd_list(accounts, plain=plain)
		return 0

	if command == 'current':
		cmd_current(accounts, plain=plain)
		return 0

	if command == 'env':
		return cmd_env(accounts)

	if command == 'apply':
		active = load_active_state()
		if not active or not isinstance(active.get('index'), int):
			error_console.print('[bold red]ERROR:[/] no active account to apply')
			return 1
		index = active['index']
		if index < 0 or index >= len(accounts):
			error_console.print('[bold red]ERROR:[/] active account index is out of range')
			return 1
		try:
			name, zshrc_actions = apply_account(accounts[index], index, AppConfig.load_from_env())
		except ValueError as exc:
			error_console.print(f'[bold red]ERROR:[/] {exc}')
			return 1
		if plain:
			print_apply_result_plain(name, zshrc_actions)
		else:
			render_apply_result(name, zshrc_actions)
		return 0

	if command in {'switch', 'interactive'}:
		selected_index = args.index if command == 'switch' else None
		return await cmd_switch(accounts, selected_index, plain=plain)

	message = f'unknown command: {command}'
	if plain:
		print(f'ERROR: {message}', file=sys.stderr)
	else:
		error_console.print(f'[bold red]ERROR:[/] {message}')
	return 1


def main() -> int:
	try:
		return asyncio.run(async_main())
	except KeyboardInterrupt:
		console.print('\n[dim]已取消[/]')
		return 130


if __name__ == '__main__':
	raise SystemExit(main())
