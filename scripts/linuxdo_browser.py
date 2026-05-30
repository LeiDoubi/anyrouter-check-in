#!/usr/bin/env python3
"""
Linux.do auto browsing helper powered by Playwright.

Port of the Tampermonkey "Linux.do 自动浏览助手" userscript.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import platform
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
error_console = Console(stderr=True)

BASE_URL = 'https://linux.do'
CONFIG_DIR = Path.home() / '.config' / 'linuxdo-browser'
PROFILE_DIR = CONFIG_DIR / 'profile'
CONFIG_FILE = CONFIG_DIR / 'config.json'
STATE_FILE = CONFIG_DIR / 'state.json'

TOPIC_ID_PATTERN = re.compile(r'/t/topic/(\d+)')

SPEED_PRESETS = {
	'slow': {
		'scroll_step': 300,
		'scroll_interval_ms': (2500, 3250),
		'load_wait_ms': (4000, 4800),
		'read_ms': (2000, 4000),
		'no_new_content_retry': 4,
	},
	'normal': {
		'scroll_step': 400,
		'scroll_interval_ms': (1500, 1950),
		'load_wait_ms': (2500, 3000),
		'read_ms': (800, 1500),
		'no_new_content_retry': 3,
	},
	'fast': {
		'scroll_step': 500,
		'scroll_interval_ms': (800, 1040),
		'load_wait_ms': (1500, 1800),
		'read_ms': (300, 800),
		'no_new_content_retry': 3,
	},
	'turbo': {
		'scroll_step': 600,
		'scroll_interval_ms': (400, 520),
		'load_wait_ms': (1000, 1200),
		'read_ms': (100, 300),
		'no_new_content_retry': 2,
	},
}

LIST_OPTIONS = {
	'latest': '/latest',
	'new': '/new',
	'unread': '/unread',
}

LIKE_CHANCE_PRESETS = {
	'low': 0.05,
	'medium': 0.15,
	'high': 0.25,
	'veryHigh': 0.40,
}

LAUNCH_ARGS = [
	'--disable-blink-features=AutomationControlled',
	*(
		['--disable-dev-shm-usage', '--no-sandbox']
		if platform.system() == 'Linux'
		else []
	),
]

STEALTH_INIT_SCRIPT = """
(() => {
	Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
	window.chrome = window.chrome || { runtime: {} };
})();
"""

VERIFY_BUTTON_SELECTORS = (
	'.d-modal button.btn-primary:has-text("Verify")',
	'.modal button.btn-primary:has-text("Verify")',
	'button.btn-primary:has-text("Verify")',
	'button:has-text("Verify")',
	'button:has-text("验证")',
)


@dataclass
class BrowserConfig:
	speed: str = 'normal'
	list_type: str = 'latest'
	enable_like: bool = True
	like_chance: str = 'medium'
	max_topics_per_session: int = 50
	max_likes_per_session: int = 50
	min_like_interval_ms: int = 2000
	return_to_list_delay_ms: int = 1000
	headless: bool = False
	stuck_timeout_sec: int = 30
	use_chrome: bool = True
	human_verify_timeout_sec: int = 300

	def validate(self) -> None:
		if self.speed not in SPEED_PRESETS:
			raise ValueError(f'Unknown speed preset: {self.speed}')
		if self.list_type not in LIST_OPTIONS:
			raise ValueError(f'Unknown list type: {self.list_type}')
		if self.like_chance not in LIKE_CHANCE_PRESETS:
			raise ValueError(f'Unknown like chance preset: {self.like_chance}')

	@property
	def speed_config(self) -> dict[str, Any]:
		return SPEED_PRESETS[self.speed]

	@property
	def like_chance_value(self) -> float:
		return LIKE_CHANCE_PRESETS[self.like_chance]

	@property
	def list_path(self) -> str:
		return LIST_OPTIONS[self.list_type]


@dataclass
class BrowserState:
	viewed_topics: set[str] = field(default_factory=set)
	liked_posts: set[str] = field(default_factory=set)
	total_replies: int = 0
	session_viewed: int = 0
	session_liked: int = 0
	session_replies: int = 0

	@classmethod
	def load(cls) -> BrowserState:
		if not STATE_FILE.is_file():
			return cls()
		try:
			raw = json.loads(STATE_FILE.read_text(encoding='utf-8'))
			return cls(
				viewed_topics=set(map(str, raw.get('viewed_topics', []))),
				liked_posts=set(map(str, raw.get('liked_posts', []))),
				total_replies=int(raw.get('total_replies', 0)),
			)
		except (json.JSONDecodeError, TypeError, ValueError):
			return cls()

	def save(self) -> None:
		CONFIG_DIR.mkdir(parents=True, exist_ok=True)
		payload = {
			'viewed_topics': sorted(self.viewed_topics),
			'liked_posts': sorted(self.liked_posts),
			'total_replies': self.total_replies,
		}
		STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

	def clear(self) -> None:
		self.viewed_topics.clear()
		self.liked_posts.clear()
		self.total_replies = 0
		self.save()


def load_config() -> BrowserConfig:
	if not CONFIG_FILE.is_file():
		return BrowserConfig()
	try:
		raw = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
		config = BrowserConfig(**{k: v for k, v in raw.items() if k in BrowserConfig.__dataclass_fields__})
		config.validate()
		return config
	except (json.JSONDecodeError, TypeError, ValueError) as exc:
		console.print(f'[yellow]配置无效，使用默认值:[/] {exc}')
		return BrowserConfig()


def save_config(config: BrowserConfig) -> None:
	CONFIG_DIR.mkdir(parents=True, exist_ok=True)
	CONFIG_FILE.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def random_delay_ms(min_ms: int, max_ms: int) -> float:
	return random.randint(min_ms, max_ms) / 1000


async def sleep_range(range_ms: tuple[int, int]) -> None:
	await asyncio.sleep(random_delay_ms(*range_ms))


def get_topic_id_from_url(url: str) -> str | None:
	match = TOPIC_ID_PATTERN.search(url)
	return match.group(1) if match else None


def page_type_from_url(url: str) -> str:
	if TOPIC_ID_PATTERN.search(url):
		return 'topic'
	path = url.replace(BASE_URL, '')
	if path in LIST_OPTIONS.values() or path == '/' or path.startswith('/c/'):
		return 'list'
	return 'other'


class LinuxDoBrowser:
	def __init__(self, config: BrowserConfig, state: BrowserState) -> None:
		self.config = config
		self.state = state
		self.running = False
		self.last_activity = time.monotonic()
		self.last_like_time = 0.0
		self.like_disabled = not config.enable_like
		self._context: BrowserContext | None = None
		self._page: Page | None = None

	def heartbeat(self) -> None:
		self.last_activity = time.monotonic()

	def log(self, message: str) -> None:
		console.log(f'[cyan]linuxdo[/] {message}')

	async def launch(self, playwright: Playwright) -> Page:
		PROFILE_DIR.mkdir(parents=True, exist_ok=True)
		context_kwargs: dict[str, Any] = {
			'user_data_dir': str(PROFILE_DIR),
			'headless': self.config.headless,
			'viewport': {'width': 1360, 'height': 900},
			'locale': 'zh-CN',
			'args': LAUNCH_ARGS,
			'ignore_default_args': ['--enable-automation'],
		}

		if self.config.use_chrome:
			try:
				self._context = await playwright.chromium.launch_persistent_context(
					channel='chrome',
					**context_kwargs,
				)
				self.log('使用系统 Chrome 启动（hCaptcha 兼容性更好）')
			except Exception as exc:
				console.print(f'[yellow]无法启动 Chrome ({exc})，回退到 Chromium[/]')
				self._context = await playwright.chromium.launch_persistent_context(**context_kwargs)
		else:
			self._context = await playwright.chromium.launch_persistent_context(**context_kwargs)

		await self._context.add_init_script(STEALTH_INIT_SCRIPT)
		self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
		return self._page

	async def close(self) -> None:
		if self._context is not None:
			await self._context.close()
			self._context = None
			self._page = None

	@property
	def page(self) -> Page:
		if self._page is None:
			raise RuntimeError('Browser page is not ready')
		return self._page

	@property
	def context(self) -> BrowserContext:
		if self._context is None:
			raise RuntimeError('Browser context is not ready')
		return self._context

	async def is_logged_in(self) -> bool:
		selectors = (
			'#current-user',
			'#toggle-current-user',
			'.header-dropdown-toggle.current-user',
			'.current-user-avatar',
			'li.current-user',
		)
		for selector in selectors:
			if await self.page.locator(selector).count() > 0:
				return True
		return False

	async def has_human_verification_modal(self) -> bool:
		checks = (
			'.d-modal:has-text("Human Verification")',
			'.modal:has-text("Human Verification")',
			'text=Human Verification',
			'iframe[src*="hcaptcha.com"]',
		)
		for selector in checks:
			try:
				locator = self.page.locator(selector).first
				if await locator.count() > 0 and await locator.is_visible():
					return True
			except Exception:
				continue
		return False

	async def get_hcaptcha_response_token(self) -> str | None:
		token = await self.page.evaluate(
			"""() => {
				for (const el of document.querySelectorAll(
					'textarea[name="h-captcha-response"], textarea[id*="h-captcha-response"]'
				)) {
					if (el.value && el.value.length > 10) return el.value;
				}
				const widget = document.querySelector('[data-hcaptcha-response]');
				if (widget) {
					const response = widget.getAttribute('data-hcaptcha-response');
					if (response) return response;
				}
				return null;
			}"""
		)
		return token if token else None

	async def try_submit_human_verification(self) -> bool:
		token = await self.get_hcaptcha_response_token()
		if not token:
			return False

		await asyncio.sleep(0.4)

		for selector in VERIFY_BUTTON_SELECTORS:
			button = self.page.locator(selector).first
			if await button.count() == 0:
				continue
			try:
				if await button.is_enabled():
					await button.click(timeout=2000)
					return True
			except Exception:
				pass

		clicked = await self.page.evaluate(
			"""(token) => {
				for (const textarea of document.querySelectorAll('textarea')) {
					if (textarea.name && textarea.name.includes('h-captcha') && !textarea.value) {
						textarea.value = token;
						textarea.dispatchEvent(new Event('input', { bubbles: true }));
						textarea.dispatchEvent(new Event('change', { bubbles: true }));
					}
				}
				for (const button of document.querySelectorAll('button')) {
					const text = (button.textContent || '').trim();
					if (text === 'Verify' || text === '验证') {
						button.disabled = false;
						button.removeAttribute('disabled');
						button.classList.remove('disabled');
						button.click();
						return true;
					}
				}
				const form = document.querySelector('.human-verification form, .d-modal form');
				if (form) {
					if (typeof form.requestSubmit === 'function') {
						form.requestSubmit();
					} else {
						form.submit();
					}
					return true;
				}
				return false;
			}""",
			token,
		)
		return bool(clicked)

	async def handle_human_verification(self, timeout_sec: int | None = None) -> None:
		if not await self.has_human_verification_modal():
			return

		timeout = timeout_sec or self.config.human_verify_timeout_sec
		console.print(
			Panel(
				'检测到 [bold]Human Verification[/] 弹窗\n'
				'1. 勾选 hCaptcha「I am human」\n'
				'2. 脚本会在验证完成后自动点击 [bold]Verify[/]\n'
				'3. 若仍无法点击，请删除 profile 后重试:\n'
				f'   [dim]rm -rf {PROFILE_DIR}[/]',
				title='人机验证',
				border_style='yellow',
			)
		)

		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			if not await self.has_human_verification_modal():
				console.print('[green]✓ 人机验证通过[/]')
				return

			if await self.try_submit_human_verification():
				await sleep_range((1500, 2500))
				if not await self.has_human_verification_modal():
					console.print('[green]✓ 人机验证通过[/]')
					return

			await asyncio.sleep(0.8)

		raise TimeoutError('人机验证超时：Verify 按钮无法点击，请删除 profile 后用 Chrome 重新 login')

	async def watch_human_verification(self) -> None:
		while self.running:
			try:
				await self.handle_human_verification(timeout_sec=3600)
			except TimeoutError:
				pass
			except asyncio.CancelledError:
				raise
			await asyncio.sleep(1)

	async def ensure_logged_in(self) -> None:
		await self.page.goto(f'{BASE_URL}{self.config.list_path}', wait_until='domcontentloaded')
		await sleep_range((1500, 2500))
		await self.handle_human_verification()
		if await self.is_logged_in():
			return
		raise RuntimeError('未检测到登录状态，请先运行: linuxdo-browser login')

	async def get_csrf_token(self) -> str:
		token = await self.page.locator('meta[name="csrf-token"]').get_attribute('content')
		if not token:
			raise RuntimeError('无法获取 CSRF Token')
		return token

	async def send_like(self, post_id: str) -> dict[str, Any]:
		# 必须在页面上下文内 fetch，context.request 会缺少 WAF/会话绑定导致 403
		result = await self.page.evaluate(
			"""async (postId) => {
				const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
				if (!csrf) return { success: false, error: '无 CSRF Token' };

				const url = `/discourse-reactions/posts/${postId}/custom-reactions/heart/toggle.json`;
				try {
					const response = await fetch(url, {
						method: 'PUT',
						credentials: 'same-origin',
						headers: {
							'Content-Type': 'application/json',
							'X-CSRF-Token': csrf,
							'X-Requested-With': 'XMLHttpRequest',
							'Discourse-Present': 'true',
						},
					});

					let data = {};
					try {
						data = await response.json();
					} catch (e) {}

					if (response.ok) {
						return { success: true };
					}

					if (response.status === 429 || data.error_type === 'rate_limit') {
						const errors = data.errors;
						return {
							success: false,
							rate_limited: true,
							error: Array.isArray(errors) ? errors[0] : '达到点赞上限',
						};
					}

					const errors = data.errors;
					const error = Array.isArray(errors) ? errors[0] : `HTTP ${response.status}`;
					return { success: false, error, status: response.status };
				} catch (e) {
					return { success: false, error: String(e) };
				}
			}""",
			post_id,
		)
		return result if isinstance(result, dict) else {'success': False, 'error': '未知错误'}

	async def click_like_button(self, like_btn) -> dict[str, Any]:
		try:
			await like_btn.first.scroll_into_view_if_needed()
			await sleep_range((100, 300))
			await like_btn.first.click(timeout=3000)
			await sleep_range((400, 800))
			class_name = await like_btn.first.get_attribute('class') or ''
			if any(flag in class_name for flag in ('has-like', 'my-likes', 'liked')):
				return {'success': True}
			return {'success': False, 'error': '点击后未检测到已点赞状态'}
		except Exception as exc:
			return {'success': False, 'error': str(exc)}

	def _record_like_success(self, post_key: str) -> None:
		self.state.liked_posts.add(post_key)
		self.state.session_liked += 1
		self.last_like_time = time.monotonic()
		self.state.save()
		self.log(f'点赞帖子 #{post_key}')
		self.heartbeat()

	async def scroll_down(self) -> None:
		step = self.config.speed_config['scroll_step'] + random.randint(-30, 30)
		await self.page.evaluate('step => window.scrollBy(0, step)', step)

	async def scroll_to_top(self) -> None:
		await self.page.evaluate('window.scrollTo(0, 0)')
		await sleep_range((200, 400))

	async def is_at_bottom(self) -> bool:
		return await self.page.evaluate(
			"""() => {
				const top = window.pageYOffset || document.documentElement.scrollTop;
				const height = document.documentElement.scrollHeight;
				const client = document.documentElement.clientHeight;
				return top + client >= height - 100;
			}"""
		)

	async def get_scroll_height(self) -> int:
		return int(await self.page.evaluate('document.documentElement.scrollHeight'))

	def should_like(self) -> bool:
		if self.like_disabled:
			return False
		if self.state.session_liked >= self.config.max_likes_per_session:
			return False
		if time.monotonic() - self.last_like_time < self.config.min_like_interval_ms / 1000:
			return False
		return random.random() < self.config.like_chance_value

	async def try_like_post(self, post_locator, post_key: str, actual_post_id: str | None) -> None:
		if not actual_post_id or post_key in self.state.liked_posts:
			return

		like_btn = post_locator.locator(
			'button[title="点赞此帖子"], '
			'button.btn-toggle-reaction-like, '
			'button.discourse-reactions-reaction-button'
		)
		if await like_btn.count() == 0:
			return

		class_name = await like_btn.first.get_attribute('class') or ''
		if any(flag in class_name for flag in ('has-like', 'my-likes', 'liked')):
			return

		await sleep_range((200, 500))
		result = await self.send_like(actual_post_id)
		if not result.get('success') and not result.get('rate_limited'):
			self.log(f'API 点赞失败 ({result.get("error")})，尝试点击按钮')
			result = await self.click_like_button(like_btn)

		if result.get('success'):
			self._record_like_success(post_key)
			return

		if result.get('rate_limited'):
			self.like_disabled = True
			self.log(f'达到点赞上限，已关闭点赞: {result.get("error")}')
			return

		self.log(f'点赞失败: {result.get("error")}')

	async def browse_topic(self, topic_id: str) -> None:
		speed = self.config.speed_config
		topic_url = f'{BASE_URL}/t/topic/{topic_id}/1'
		self.log(f'浏览话题 {topic_id}')
		await self.page.goto(topic_url, wait_until='domcontentloaded')
		await sleep_range((1500, 2500))

		if topic_id not in self.state.viewed_topics:
			self.state.viewed_topics.add(topic_id)
			self.state.session_viewed += 1
			self.state.save()

		await self.scroll_to_top()
		last_scroll_height = await self.get_scroll_height()
		no_new_content_count = 0
		viewed_posts: set[str] = set()

		while self.running:
			self.check_stuck()
			posts = self.page.locator('article[id^="post_"]')
			count = await posts.count()
			for index in range(count):
				if not self.running:
					break
				post = posts.nth(index)
				post_dom_id = await post.get_attribute('id') or f'post_{index}'
				post_key = post_dom_id.replace('post_', '')
				if post_key in viewed_posts:
					continue

				box = await post.bounding_box()
				if not box:
					continue
				viewport = self.page.viewport_size or {'width': 1360, 'height': 900}
				if box['y'] + box['height'] < viewport['height'] * 0.1:
					continue
				if box['y'] > viewport['height'] * 0.9:
					continue

				viewed_posts.add(post_key)
				self.state.session_replies += 1
				self.state.total_replies += 1
				if self.state.session_replies % 10 == 0:
					self.state.save()
				self.heartbeat()

				read_range = speed['read_ms']
				if read_range[1] > 0:
					await sleep_range(tuple(read_range))

				if self.should_like():
					actual_post_id = await post.get_attribute('data-post-id')
					await self.try_like_post(post, post_key, actual_post_id)

			if await self.is_at_bottom():
				await sleep_range(tuple(speed['load_wait_ms']))
				current_height = await self.get_scroll_height()
				if current_height > last_scroll_height:
					last_scroll_height = current_height
					no_new_content_count = 0
					self.log('检测到新回复加载')
				else:
					no_new_content_count += 1
					self.log(f'无新内容 ({no_new_content_count}/{speed["no_new_content_retry"]})')
					if no_new_content_count >= speed['no_new_content_retry']:
						self.log('话题浏览完成')
						break
			else:
				no_new_content_count = 0

			await self.scroll_down()
			await sleep_range(tuple(speed['scroll_interval_ms']))

		await sleep_range((self.config.return_to_list_delay_ms, int(self.config.return_to_list_delay_ms * 1.5)))

	async def find_unviewed_topic(self) -> str | None:
		rows = self.page.locator('.topic-list-item, tr[data-topic-id], .topic-list tr')
		count = await rows.count()
		for index in range(count):
			row = rows.nth(index)
			link = row.locator('.title a[href*="/t/topic/"], .link-top-line a[href*="/t/topic/"], a.title[href*="/t/topic/"]').first
			if await link.count() == 0:
				continue
			href = await link.get_attribute('href')
			if not href:
				continue
			topic_id = get_topic_id_from_url(urljoin(BASE_URL, href))
			if not topic_id or topic_id in self.state.viewed_topics:
				continue
			if self.state.session_viewed >= self.config.max_topics_per_session:
				return None
			await link.scroll_into_view_if_needed()
			await sleep_range((300, 600))
			self.log(f'进入未浏览话题 {topic_id}')
			await link.click()
			await self.page.wait_for_load_state('domcontentloaded')
			await sleep_range((1200, 1800))
			return topic_id
		return None

	async def browse_list_until_topic(self) -> str | None:
		speed = self.config.speed_config
		last_scroll_height = await self.get_scroll_height()
		no_new_content_count = 0

		while self.running:
			self.check_stuck()
			topic_id = await self.find_unviewed_topic()
			if topic_id:
				return topic_id

			if await self.is_at_bottom():
				await sleep_range(tuple(speed['load_wait_ms']))
				current_height = await self.get_scroll_height()
				if current_height > last_scroll_height:
					last_scroll_height = current_height
					no_new_content_count = 0
				else:
					no_new_content_count += 1
					if no_new_content_count >= speed['no_new_content_retry']:
						self.log('当前列表已到底，刷新列表')
						await self.page.goto(f'{BASE_URL}{self.config.list_path}', wait_until='domcontentloaded')
						await sleep_range((1000, 2000))
						last_scroll_height = await self.get_scroll_height()
						no_new_content_count = 0
						continue
			await self.scroll_down()
			await sleep_range(tuple(speed['scroll_interval_ms']))

		return None

	async def return_to_list(self) -> None:
		await self.page.goto(f'{BASE_URL}{self.config.list_path}', wait_until='domcontentloaded')
		await sleep_range((1200, 1800))

	def check_stuck(self) -> None:
		elapsed = time.monotonic() - self.last_activity
		if elapsed > self.config.stuck_timeout_sec:
			raise TimeoutError(f'操作超时 ({int(elapsed)}s 无进展)')

	async def run(self) -> None:
		self.running = True
		self.heartbeat()
		await self.ensure_logged_in()
		verify_task = asyncio.create_task(self.watch_human_verification())

		try:
			while self.running:
				if self.state.session_viewed >= self.config.max_topics_per_session:
					self.log('已达到本次会话最大浏览话题数')
					break

				await self.handle_human_verification()

				current_type = page_type_from_url(self.page.url)
				if current_type != 'list':
					await self.return_to_list()

				topic_id = await self.browse_list_until_topic()
				if not topic_id:
					self.log('未找到可浏览的话题')
					break

				await self.browse_topic(topic_id)
				await self.return_to_list()
				self.print_stats()
		finally:
			verify_task.cancel()
			with contextlib.suppress(asyncio.CancelledError):
				await verify_task

		self.state.save()
		self.running = False

	def stop(self) -> None:
		self.running = False

	def print_stats(self) -> None:
		table = Table(title='浏览统计', show_header=True, header_style='bold cyan')
		table.add_column('项目')
		table.add_column('本次', justify='right')
		table.add_column('总计', justify='right')
		table.add_row('帖子', str(self.state.session_viewed), str(len(self.state.viewed_topics)))
		table.add_row('回复', str(self.state.session_replies), str(self.state.total_replies))
		table.add_row('点赞', str(self.state.session_liked), str(len(self.state.liked_posts)))
		console.print(table)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description='Linux.do auto browsing helper (Playwright)')
	subparsers = parser.add_subparsers(dest='command')

	login_parser = subparsers.add_parser('login', help='Open browser and save login session')
	login_parser.add_argument('--headless', action='store_true', help='Run browser headless (not recommended)')

	run_parser = subparsers.add_parser('run', help='Start auto browsing')
	run_parser.add_argument('--speed', choices=list(SPEED_PRESETS), help='Speed preset')
	run_parser.add_argument('--list', choices=list(LIST_OPTIONS), dest='list_type', help='Topic list type')
	run_parser.add_argument('--like', dest='enable_like', action=argparse.BooleanOptionalAction, help='Enable random likes')
	run_parser.add_argument('--like-chance', choices=list(LIKE_CHANCE_PRESETS), help='Like probability preset')
	run_parser.add_argument('--max-topics', type=int, help='Max topics per session')
	run_parser.add_argument('--headless', action='store_true', help='Run browser headless')

	subparsers.add_parser('stats', help='Show browsing stats')
	subparsers.add_parser('clear', help='Clear browsing history')

	subparsers.add_parser('config', help='Show current config')

	import_parser = subparsers.add_parser('import-cookies', help='Import cookies from normal browser (skip login)')
	import_parser.add_argument('cookies', nargs='?', help='Cookie string: name1=val1; name2=val2')
	import_parser.add_argument('--file', '-f', help='Read cookies from file')

	return parser


def apply_cli_overrides(config: BrowserConfig, args: argparse.Namespace) -> None:
	if getattr(args, 'speed', None):
		config.speed = args.speed
	if getattr(args, 'list_type', None):
		config.list_type = args.list_type
	if getattr(args, 'enable_like', None) is not None:
		config.enable_like = args.enable_like
	if getattr(args, 'like_chance', None):
		config.like_chance = args.like_chance
	if getattr(args, 'max_topics', None):
		config.max_topics_per_session = args.max_topics
	if getattr(args, 'headless', False):
		config.headless = True
	config.validate()


async def cmd_login(args: argparse.Namespace) -> int:
	config = load_config()
	if args.headless:
		config.headless = True
	save_config(config)

	async with async_playwright() as playwright:
		browser = LinuxDoBrowser(config, BrowserState.load())
		page = await browser.launch(playwright)
		await page.goto(f'{BASE_URL}/latest', wait_until='domcontentloaded')
		await browser.handle_human_verification()
		browser.running = True
		verify_task = asyncio.create_task(browser.watch_human_verification())
		console.print(
			Panel(
				'请在打开的浏览器中登录 [bold]linux.do[/]\n'
				'若出现 Human Verification，完成 hCaptcha 后脚本会自动点 Verify\n'
				'登录完成后回到终端，按 Enter 保存 session',
				title='Linux.do 登录',
				border_style='bright_blue',
			)
		)
		try:
			await asyncio.to_thread(input)
		finally:
			browser.running = False
			verify_task.cancel()
			with contextlib.suppress(asyncio.CancelledError):
				await verify_task
		await browser.handle_human_verification()
		if await browser.is_logged_in():
			console.print('[green]✓ 登录状态已保存[/]')
		else:
			console.print('[yellow]未检测到登录元素，session 仍会保存，运行 run 时再验证[/]')
		await browser.close()
	return 0


def parse_cookie_string(cookie_str: str) -> list[dict[str, Any]]:
	cookies: list[dict[str, Any]] = []
	for part in cookie_str.strip().split(';'):
		part = part.strip()
		if '=' not in part:
			continue
		name, _, value = part.partition('=')
		name = name.strip()
		value = value.strip()
		if not name:
			continue
		cookies.append({'name': name, 'value': value, 'domain': '.linux.do', 'path': '/'})
	return cookies


async def cmd_import_cookies(args: argparse.Namespace) -> int:
	cookie_str = args.cookies
	if args.file:
		cookie_str = Path(args.file).read_text(encoding='utf-8').strip()
	if not cookie_str:
		error_console.print('[bold red]请提供 cookie 字符串或 --file[/]')
		return 1

	cookies = parse_cookie_string(cookie_str)
	if not cookies:
		error_console.print('[bold red]Cookie 解析失败[/]')
		return 1

	config = load_config()
	async with async_playwright() as playwright:
		browser = LinuxDoBrowser(config, BrowserState.load())
		await browser.launch(playwright)
		await browser.context.add_cookies(cast(Any, cookies))
		await browser.page.goto(f'{BASE_URL}/latest', wait_until='domcontentloaded')
		await browser.handle_human_verification()
		if await browser.is_logged_in():
			console.print(f'[green]✓ 已导入 {len(cookies)} 个 Cookie 并验证登录成功[/]')
		else:
			console.print(f'[yellow]已导入 {len(cookies)} 个 Cookie，但未检测到登录状态，Cookie 可能已过期[/]')
		await browser.close()
	return 0


async def cmd_run(args: argparse.Namespace) -> int:
	config = load_config()
	apply_cli_overrides(config, args)
	save_config(config)
	state = BrowserState.load()
	browser = LinuxDoBrowser(config, state)

	console.print(
		Panel(
			f'速度: [cyan]{config.speed}[/]  列表: [cyan]{config.list_type}[/]  '
			f'点赞: [cyan]{"开" if config.enable_like else "关"}[/]  '
			f'概率: [cyan]{config.like_chance}[/]',
			title='Linux.do 自动浏览',
			border_style='bright_blue',
		)
	)

	try:
		async with async_playwright() as playwright:
			await browser.launch(playwright)
			await browser.run()
			await browser.close()
	except KeyboardInterrupt:
		browser.stop()
		state.save()
		console.print('\n[dim]已停止[/]')
	except RuntimeError as exc:
		error_console.print(f'[bold red]ERROR:[/] {exc}')
		return 1
	except TimeoutError as exc:
		error_console.print(f'[bold red]TIMEOUT:[/] {exc}')
		return 1

	browser.print_stats()
	return 0


def cmd_stats() -> int:
	state = BrowserState.load()
	config = load_config()
	table = Table(title='Linux.do 浏览记录', header_style='bold cyan')
	table.add_column('项目')
	table.add_column('数量', justify='right')
	table.add_row('已浏览话题', str(len(state.viewed_topics)))
	table.add_row('已点赞帖子', str(len(state.liked_posts)))
	table.add_row('累计浏览回复', str(state.total_replies))
	console.print(table)
	console.print(
		f'当前配置: speed={config.speed}, list={config.list_type}, like={config.enable_like}, chance={config.like_chance}'
	)
	return 0


def cmd_clear() -> int:
	state = BrowserState.load()
	state.clear()
	console.print('[green]已清除浏览记录[/]')
	return 0


def cmd_config() -> int:
	config = load_config()
	console.print_json(json.dumps(asdict(config), ensure_ascii=False, indent=2))
	return 0


async def async_main(args: argparse.Namespace) -> int:
	command = args.command or 'run'
	if command == 'login':
		return await cmd_login(args)
	if command == 'run':
		return await cmd_run(args)
	if command == 'stats':
		return cmd_stats()
	if command == 'clear':
		return cmd_clear()
	if command == 'config':
		return cmd_config()
	if command == 'import-cookies':
		return await cmd_import_cookies(args)
	error_console.print(f'[bold red]未知命令:[/] {command}')
	return 1


def main() -> int:
	parser = build_parser()
	args = parser.parse_args()
	if args.command is None and '--help' not in sys.argv and '-h' not in sys.argv:
		args.command = 'run'
	return asyncio.run(async_main(args))


if __name__ == '__main__':
	raise SystemExit(main())
