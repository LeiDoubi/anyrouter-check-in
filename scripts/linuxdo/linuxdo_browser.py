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
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text
from sqlalchemy import select

from scripts.linuxdo.linuxdo_connect import ConnectStatus, parse_connect_status
from scripts.linuxdo.linuxdo_planner import TrustLevelPlanner
from scripts.linuxdo.linuxdo_store import Account, AccountStore, ConnectSnapshot, RunSession

console = Console()
error_console = Console(stderr=True)

BASE_URL = 'https://linux.do'
CONNECT_URL = 'https://connect.linux.do'
CONFIG_DIR = Path.home() / '.config' / 'linuxdo-browser'
LEGACY_PROFILE_DIR = CONFIG_DIR / 'profile'
PROFILES_DIR = CONFIG_DIR / 'profiles'
CONFIG_FILE = CONFIG_DIR / 'config.json'
STATE_FILE = CONFIG_DIR / 'state.json'
DB_FILE = CONFIG_DIR / 'linuxdo.sqlite3'

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

LIKE_TOPIC_INTERVAL_FALLBACKS = {
	'low': 4,
	'medium': 3,
	'high': 2,
	'veryHigh': 1,
}

SPEED_VARIATION_PROFILES = (
	{'name': '快', 'weight': 5, 'delay_factor': 0.55, 'scroll_factor': 1.35},
	{'name': '正常', 'weight': 3, 'delay_factor': 0.80, 'scroll_factor': 1.10},
	{'name': '慢', 'weight': 2, 'delay_factor': 1.15, 'scroll_factor': 0.90},
)

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
	max_topic_pages: int = 5
	min_read_minutes_per_session: int = 0
	return_to_list_delay_ms: int = 1000
	headless: bool = False
	stuck_timeout_sec: int = 30
	use_chrome: bool = True
	human_verify_timeout_sec: int = 300
	target_level: int = 2
	daily_topic_limit: int = 50
	daily_like_limit: int = 30
	run_all_cooldown_min_sec: int = 30
	run_all_cooldown_max_sec: int = 90

	def validate(self) -> None:
		if self.speed not in SPEED_PRESETS:
			raise ValueError(f'Unknown speed preset: {self.speed}')
		if self.list_type not in LIST_OPTIONS:
			raise ValueError(f'Unknown list type: {self.list_type}')
		if self.like_chance not in LIKE_CHANCE_PRESETS:
			raise ValueError(f'Unknown like chance preset: {self.like_chance}')
		if self.target_level < 1 or self.target_level > 4:
			raise ValueError(f'target_level must be between 1 and 4: {self.target_level}')
		if self.daily_topic_limit < 0:
			raise ValueError(f'daily_topic_limit must be >= 0: {self.daily_topic_limit}')
		if self.daily_like_limit < 0:
			raise ValueError(f'daily_like_limit must be >= 0: {self.daily_like_limit}')
		if self.max_topic_pages < 1:
			raise ValueError(f'max_topic_pages must be >= 1: {self.max_topic_pages}')
		if self.min_read_minutes_per_session < 0:
			raise ValueError(f'min_read_minutes_per_session must be >= 0: {self.min_read_minutes_per_session}')

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
	session_read_minutes: int = 0
	persist_file: bool = True

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

	@classmethod
	def load_for_account(cls, store: AccountStore, account: Account) -> BrowserState:
		metrics = store.aggregate_metrics(account.id)
		return cls(
			viewed_topics=store.viewed_topics(account.id),
			liked_posts=store.liked_posts(account.id),
			total_replies=int(metrics.get('posts_read', 0)),
			persist_file=False,
		)

	def save(self) -> None:
		if not self.persist_file:
			return
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


async def safe_locator_attribute(locator: Any, name: str, timeout_ms: int = 1000) -> str | None:
	try:
		return await locator.get_attribute(name, timeout=timeout_ms)
	except (PlaywrightError, PlaywrightTimeoutError):
		return None


async def safe_locator_box(locator: Any, timeout_ms: int = 1000) -> dict[str, float] | None:
	try:
		return await locator.bounding_box(timeout=timeout_ms)
	except (PlaywrightError, PlaywrightTimeoutError):
		return None


def scale_ms_range(range_ms: tuple[int, int], factor: float, minimum: int = 80) -> tuple[int, int]:
	return (max(minimum, int(range_ms[0] * factor)), max(minimum, int(range_ms[1] * factor)))


def scale_speed_config(speed: dict[str, Any], delay_factor: float, scroll_factor: float) -> dict[str, Any]:
	return {
		**speed,
		'scroll_step': max(160, int(speed['scroll_step'] * scroll_factor)),
		'scroll_interval_ms': scale_ms_range(tuple(speed['scroll_interval_ms']), delay_factor, 120),
		'load_wait_ms': scale_ms_range(tuple(speed['load_wait_ms']), delay_factor, 300),
		'read_ms': scale_ms_range(tuple(speed['read_ms']), delay_factor, 80),
	}


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
	def __init__(
		self,
		config: BrowserConfig,
		state: BrowserState,
		account: Account | None = None,
		store: AccountStore | None = None,
	) -> None:
		self.config = config
		self.state = state
		self.account = account
		self.store = store
		self.running = False
		self.last_activity = time.monotonic()
		self.last_like_time = 0.0
		self.like_disabled = not config.enable_like
		self.read_seconds_carry = 0.0
		self.max_topics_override_logged = False
		self._context: BrowserContext | None = None
		self._page: Page | None = None

	@property
	def profile_dir(self) -> Path:
		if self.account is not None:
			return Path(self.account.profile_dir)
		return LEGACY_PROFILE_DIR

	def heartbeat(self) -> None:
		self.last_activity = time.monotonic()

	def log(self, message: str) -> None:
		console.log(f'[cyan]linuxdo[/] {message}')

	async def launch(self, playwright: Playwright) -> Page:
		self.profile_dir.mkdir(parents=True, exist_ok=True)
		context_kwargs: dict[str, Any] = {
			'user_data_dir': str(self.profile_dir),
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
				f'   [dim]rm -rf {self.profile_dir}[/]',
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
			if self.store is not None and self.account is not None:
				self.store.update_username(self.account.id, await self.get_current_username())
			return
		raise RuntimeError('未检测到登录状态，请先运行: linuxdo-browser login')

	async def get_current_username(self) -> str | None:
		return await self.page.evaluate(
			"""() => {
				const avatar = document.querySelector('#current-user img.avatar, .current-user-avatar img.avatar');
				const title = avatar?.getAttribute('title') || avatar?.getAttribute('alt');
				if (title) return title.replace(/^@/, '').trim();
				const user = document.querySelector('#current-user, #toggle-current-user, li.current-user');
				return user?.getAttribute('data-username') || null;
			}"""
		)

	async def sync_connect_status(self) -> ConnectStatus:
		await self.page.goto(CONNECT_URL, wait_until='domcontentloaded')
		await sleep_range((1500, 2500))
		text = await self.page.locator('body').inner_text(timeout=15000)
		status = parse_connect_status(text)
		if self.store is not None and self.account is not None:
			self.store.record_connect_snapshot(self.account.id, status.to_store_payload())
		return status

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
		if self.store is not None and self.account is not None:
			self.store.record_event(self.account.id, 'like_given', post_id=post_key)
		self.state.save()
		self.log(f'点赞帖子 #{post_key}')
		if self.daily_like_limit_reached():
			self.like_disabled = True
			self.log('今日点赞上限已达，已关闭点赞')
		self.heartbeat()

	def sample_speed_config(self) -> tuple[str, dict[str, Any]]:
		profile = random.choices(
			SPEED_VARIATION_PROFILES,
			weights=[float(item['weight']) for item in SPEED_VARIATION_PROFILES],
			k=1,
		)[0]
		delay_factor = float(profile['delay_factor']) * random.uniform(0.9, 1.1)
		scroll_factor = float(profile['scroll_factor']) * random.uniform(0.9, 1.1)
		return str(profile['name']), scale_speed_config(self.config.speed_config, delay_factor, scroll_factor)

	async def scroll_down(self, speed: dict[str, Any] | None = None, *, page_scroll: bool = False) -> None:
		active_speed = speed or self.config.speed_config
		step = int(active_speed['scroll_step']) + random.randint(-30, 30)
		if page_scroll:
			viewport = self.page.viewport_size or {'width': 1360, 'height': 900}
			viewport_step = int(viewport['height'] * random.uniform(0.65, 0.95))
			step = max(step, viewport_step)
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

	def topic_like_interval(self) -> int:
		if self.config.daily_topic_limit > 0 and self.config.daily_like_limit > 0:
			return max(1, round(self.config.daily_topic_limit / self.config.daily_like_limit))
		if 0 < self.config.max_likes_per_session < self.config.max_topics_per_session:
			return max(1, round(self.config.max_topics_per_session / self.config.max_likes_per_session))
		return LIKE_TOPIC_INTERVAL_FALLBACKS[self.config.like_chance]

	def should_like_topic(self) -> bool:
		if self.like_disabled:
			return False
		if self.state.session_liked >= self.config.max_likes_per_session:
			return False
		if self.daily_like_limit_reached():
			self.like_disabled = True
			self.log('今日点赞上限已达，已关闭点赞')
			return False
		if time.monotonic() - self.last_like_time < self.config.min_like_interval_ms / 1000:
			return False
		interval = self.topic_like_interval()
		if self.store is not None and self.account is not None:
			counts = self.daily_counts()
			topics_viewed = counts['topic_view']
			likes_given = counts['like_given']
		else:
			topics_viewed = self.state.session_viewed
			likes_given = self.state.session_liked
		return likes_given < topics_viewed // interval

	async def post_text_length(self, post_locator) -> int:
		text = ''
		body = post_locator.locator('.cooked').first
		try:
			if await body.count() > 0:
				text = await body.inner_text(timeout=1000)
			else:
				text = await post_locator.inner_text(timeout=1000)
		except Exception:
			with contextlib.suppress(Exception):
				text = await post_locator.inner_text(timeout=1000)
		return len(re.sub(r'\s+', '', text))

	def daily_counts(self) -> dict[str, int]:
		if self.store is None or self.account is None:
			return {'topic_view': 0, 'like_given': 0}
		return self.store.daily_event_counts(self.account.id)

	def daily_topic_limit_reached(self) -> bool:
		if self.config.daily_topic_limit <= 0:
			return False
		return self.daily_counts()['topic_view'] >= self.config.daily_topic_limit

	def daily_like_limit_reached(self) -> bool:
		if self.config.daily_like_limit <= 0:
			return False
		return self.daily_counts()['like_given'] >= self.config.daily_like_limit

	def read_minutes_target_reached(self) -> bool:
		target = self.config.min_read_minutes_per_session
		return target <= 0 or self.state.session_read_minutes >= target

	def max_topics_limit_reached(self) -> bool:
		if self.state.session_viewed < self.config.max_topics_per_session:
			return False
		if self.read_minutes_target_reached():
			return True
		if not self.max_topics_override_logged:
			self.log(
				f'已达到本次话题数上限，但阅读分钟未达标 '
				f'({self.state.session_read_minutes}/{self.config.min_read_minutes_per_session})，继续浏览'
			)
			self.max_topics_override_logged = True
		return False

	def record_read_time(self, topic_id: str, elapsed_seconds: float) -> None:
		self.read_seconds_carry += elapsed_seconds
		read_minutes = int(self.read_seconds_carry // 60)
		if read_minutes <= 0:
			return
		self.read_seconds_carry -= read_minutes * 60
		self.state.session_read_minutes += read_minutes
		if self.store is not None and self.account is not None:
			self.store.record_event(self.account.id, 'read_minute', topic_id=topic_id, value=read_minutes)

	async def try_like_post(self, post_locator, post_key: str, actual_post_id: str | None) -> bool:
		if not actual_post_id or post_key in self.state.liked_posts:
			return False

		like_btn = None
		if post_locator is not None:
			like_btn = post_locator.locator(
				'button[title="点赞此帖子"], '
				'button.btn-toggle-reaction-like, '
				'button.discourse-reactions-reaction-button'
			)
			if await like_btn.count() > 0:
				class_name = await safe_locator_attribute(like_btn.first, 'class') or ''
				if any(flag in class_name for flag in ('has-like', 'my-likes', 'liked')):
					return False

		await sleep_range((200, 500))
		result = await self.send_like(actual_post_id)
		if not result.get('success') and not result.get('rate_limited') and like_btn is not None and await like_btn.count() > 0:
			self.log(f'API 点赞失败 ({result.get("error")})，尝试点击按钮')
			result = await self.click_like_button(like_btn)

		if result.get('success'):
			self._record_like_success(post_key)
			return True

		if result.get('rate_limited'):
			self.like_disabled = True
			self.log(f'达到点赞上限，已关闭点赞: {result.get("error")}')
			return False

		self.log(f'点赞失败: {result.get("error")}')
		return False

	async def browse_topic(self, topic_id: str) -> None:
		speed_label, speed = self.sample_speed_config()
		topic_url = f'{BASE_URL}/t/topic/{topic_id}/1'
		self.log(f'浏览话题 {topic_id}（节奏: {speed_label}）')
		topic_started = time.monotonic()
		await self.page.goto(topic_url, wait_until='domcontentloaded')
		await sleep_range((1500, 2500))
		topic_should_like = False

		if topic_id not in self.state.viewed_topics:
			if self.daily_topic_limit_reached():
				self.log('今日话题浏览上限已达，跳过新话题记录')
				return
			self.state.viewed_topics.add(topic_id)
			self.state.session_viewed += 1
			if self.store is not None and self.account is not None:
				self.store.record_event(self.account.id, 'topic_view', topic_id=topic_id)
			self.state.save()
			topic_should_like = self.should_like_topic()
			if topic_should_like:
				self.log(f'本话题计划点赞（约每 {self.topic_like_interval()} 个话题 1 次）')

		await self.scroll_to_top()
		last_scroll_height = await self.get_scroll_height()
		no_new_content_count = 0
		viewed_posts: set[str] = set()
		like_candidate_keys: set[str] = set()
		like_candidates: list[tuple[int, str, str, Any]] = []
		topic_pages_viewed = 1

		while self.running:
			self.check_stuck()
			posts = self.page.locator('article[id^="post_"]')
			count = await posts.count()
			for index in range(count):
				if not self.running:
					break
				post = posts.nth(index)
				post_dom_id = await safe_locator_attribute(post, 'id')
				if not post_dom_id:
					continue
				post_key = post_dom_id.replace('post_', '')
				if post_key in viewed_posts:
					continue

				box = await safe_locator_box(post)
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
				if self.store is not None and self.account is not None:
					self.store.record_event(self.account.id, 'post_read', topic_id=topic_id, post_id=post_key)
				if self.state.session_replies % 10 == 0:
					self.state.save()
				self.heartbeat()

				read_range = speed['read_ms']
				if read_range[1] > 0:
					await sleep_range(tuple(read_range))

				if topic_should_like and post_key not in like_candidate_keys:
					actual_post_id = await safe_locator_attribute(post, 'data-post-id')
					if actual_post_id:
						text_length = await self.post_text_length(post)
						like_candidates.append((text_length, post_key, actual_post_id, post))
						like_candidate_keys.add(post_key)

			if await self.is_at_bottom():
				await sleep_range(tuple(speed['load_wait_ms']))
				current_height = await self.get_scroll_height()
				if current_height > last_scroll_height:
					last_scroll_height = current_height
					no_new_content_count = 0
					self.log('检测到更多帖子加载')
				else:
					no_new_content_count += 1
					self.log(f'无新内容 ({no_new_content_count}/{speed["no_new_content_retry"]})')
					if no_new_content_count >= speed['no_new_content_retry']:
						self.log('话题浏览完成')
						break
			else:
				no_new_content_count = 0

			if topic_pages_viewed >= self.config.max_topic_pages:
				self.log(f'已浏览 {topic_pages_viewed} 页，结束本话题')
				break

			await self.scroll_down(speed, page_scroll=True)
			topic_pages_viewed += 1
			await sleep_range(tuple(speed['scroll_interval_ms']))

		if topic_should_like:
			liked = False
			for text_length, post_key, actual_post_id, post in sorted(like_candidates, reverse=True)[:5]:
				self.log(f'准备点赞候选帖子 #{post_key}（正文约 {text_length} 字）')
				liked = await self.try_like_post(post, post_key, actual_post_id)
				if liked or self.like_disabled:
					break
			if not liked and not self.like_disabled:
				self.log('本话题未找到可点赞的候选帖子')

		self.record_read_time(topic_id, time.monotonic() - topic_started)

		await sleep_range((self.config.return_to_list_delay_ms, int(self.config.return_to_list_delay_ms * 1.5)))

	async def find_unviewed_topic(self) -> str | None:
		if self.daily_topic_limit_reached():
			self.log('今日话题浏览上限已达')
			return None
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
			if self.max_topics_limit_reached():
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
			await self.scroll_down(speed)
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
				if self.max_topics_limit_reached():
					self.log('已达到本次会话最大浏览话题数')
					break
				if self.daily_topic_limit_reached():
					self.log('已达到今日话题浏览上限')
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
		table.add_row('话题', str(self.state.session_viewed), str(len(self.state.viewed_topics)))
		table.add_row('已读帖子', str(self.state.session_replies), str(self.state.total_replies))
		table.add_row('点赞', str(self.state.session_liked), str(len(self.state.liked_posts)))
		if self.store is not None and self.account is not None:
			daily = self.daily_counts()
			metrics = self.store.aggregate_metrics(self.account.id)
			table.add_row('阅读分钟', str(self.state.session_read_minutes), str(metrics.get('read_minutes', 0)))
			table.add_row('今日话题', str(daily['topic_view']), str(self.config.daily_topic_limit or '不限'))
			table.add_row('今日点赞', str(daily['like_given']), str(self.config.daily_like_limit or '不限'))
		console.print(table)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description='Linux.do auto browsing helper (Playwright)')
	subparsers = parser.add_subparsers(dest='command')

	accounts_parser = subparsers.add_parser('accounts', help='Manage Linux.do accounts')
	accounts_subparsers = accounts_parser.add_subparsers(dest='accounts_command')
	accounts_add = accounts_subparsers.add_parser('add', help='Add an account and open login browser')
	accounts_add.add_argument('name', help='Account display name')
	accounts_add.add_argument('--target-level', type=int, default=2, help='Target trust level (1-4)')
	accounts_subparsers.add_parser('list', help='List accounts')

	login_parser = subparsers.add_parser('login', help='Open browser and save login session')
	login_parser.add_argument('--account', help='Account name or slug')
	login_parser.add_argument('--headless', action='store_true', help='Run browser headless (not recommended)')

	run_parser = subparsers.add_parser('run', help='Start auto browsing')
	run_parser.add_argument('--account', help='Account name or slug')
	run_parser.add_argument('--speed', choices=list(SPEED_PRESETS), help='Speed preset')
	run_parser.add_argument('--list', choices=list(LIST_OPTIONS), dest='list_type', help='Topic list type')
	run_parser.add_argument('--like', dest='enable_like', action=argparse.BooleanOptionalAction, help='Enable topic-paced likes')
	run_parser.add_argument('--like-chance', choices=list(LIKE_CHANCE_PRESETS), help='Fallback like pacing preset')
	run_parser.add_argument('--max-topics', type=int, help='Max topics per session')
	run_parser.add_argument('--max-topic-pages', type=int, help='Max viewport pages to browse per topic')
	run_parser.add_argument('--min-read-minutes', type=int, help='Minimum read minutes for this session (0 disables)')
	run_parser.add_argument('--daily-topic-limit', type=int, help='Max newly viewed topics per local day (0 disables)')
	run_parser.add_argument('--daily-like-limit', type=int, help='Max likes per local day (0 disables)')
	run_parser.add_argument('--headless', action='store_true', help='Run browser headless')

	run_all_parser = subparsers.add_parser('run-all', help='Run all enabled accounts')
	run_all_parser.add_argument('--speed', choices=list(SPEED_PRESETS), help='Speed preset')
	run_all_parser.add_argument('--list', choices=list(LIST_OPTIONS), dest='list_type', help='Topic list type')
	run_all_parser.add_argument('--like', dest='enable_like', action=argparse.BooleanOptionalAction, help='Enable topic-paced likes')
	run_all_parser.add_argument('--like-chance', choices=list(LIKE_CHANCE_PRESETS), help='Fallback like pacing preset')
	run_all_parser.add_argument('--max-topics', type=int, help='Max topics per account')
	run_all_parser.add_argument('--max-topic-pages', type=int, help='Max viewport pages to browse per topic')
	run_all_parser.add_argument('--min-read-minutes', type=int, help='Minimum read minutes per account (0 disables)')
	run_all_parser.add_argument('--daily-topic-limit', type=int, help='Max newly viewed topics per local day (0 disables)')
	run_all_parser.add_argument('--daily-like-limit', type=int, help='Max likes per local day (0 disables)')
	run_all_parser.add_argument('--headless', action='store_true', help='Run browser headless')

	stats_parser = subparsers.add_parser('stats', help='Show browsing stats')
	stats_parser.add_argument('--account', help='Account name or slug')
	status_parser = subparsers.add_parser('status', help='Show trust-level progress')
	status_parser.add_argument('--account', help='Account name or slug')
	status_parser.add_argument('--offline', action='store_true', help='Use cached status without opening browser')
	status_parser.add_argument('--headless', action='store_true', help='Run browser headless during sync')
	sync_parser = subparsers.add_parser('sync-status', help='Sync status from connect.linux.do')
	sync_parser.add_argument('--account', help='Account name or slug')
	sync_parser.add_argument('--headless', action='store_true', help='Run browser headless')
	reply_parser = subparsers.add_parser('reply', help='Record manual replies')
	reply_subparsers = reply_parser.add_subparsers(dest='reply_command')
	reply_mark = reply_subparsers.add_parser('mark', help='Mark a topic as manually replied')
	reply_mark.add_argument('topic_id', help='Linux.do topic id')
	reply_mark.add_argument('--account', help='Account name or slug')
	clear_parser = subparsers.add_parser('clear', help='Clear browsing history')
	clear_parser.add_argument('--account', help='Account name or slug')
	reset_parser = subparsers.add_parser('reset', help='Reset all linuxdo-browser local data')
	reset_parser.add_argument('--yes', action='store_true', help='Skip confirmation prompt')

	subparsers.add_parser('config', help='Show current config')

	import_parser = subparsers.add_parser('import-cookies', help='Import cookies from normal browser (skip login)')
	import_parser.add_argument('--account', help='Account name or slug')
	import_parser.add_argument('cookies', nargs='?', help='Cookie string: name1=val1; name2=val2')
	import_parser.add_argument('--file', '-f', help='Read cookies from file')

	subparsers.add_parser('tui', help='Open interactive terminal UI')

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
	if getattr(args, 'max_topic_pages', None) is not None:
		config.max_topic_pages = args.max_topic_pages
	if getattr(args, 'min_read_minutes', None) is not None:
		config.min_read_minutes_per_session = args.min_read_minutes
	if getattr(args, 'daily_topic_limit', None) is not None:
		config.daily_topic_limit = args.daily_topic_limit
	if getattr(args, 'daily_like_limit', None) is not None:
		config.daily_like_limit = args.daily_like_limit
	if getattr(args, 'headless', False):
		config.headless = True
	config.validate()


def get_store() -> AccountStore:
	store = AccountStore(DB_FILE, PROFILES_DIR)
	store.init_db()
	return store


def resolve_account(store: AccountStore, name_or_slug: str | None) -> Account:
	account = store.get_account(name_or_slug) if name_or_slug else store.get_or_create_default_account()
	if account.slug == 'default' and Path(account.profile_dir) == PROFILES_DIR / 'default' and LEGACY_PROFILE_DIR.exists():
		store.update_profile_dir(account.id, LEGACY_PROFILE_DIR)
		account = store.get_account(account.slug)
	return account


def migrate_legacy_state(store: AccountStore, account: Account) -> None:
	if not STATE_FILE.is_file():
		return
	if store.viewed_topics(account.id) or store.liked_posts(account.id):
		return
	state = BrowserState.load()
	for topic_id in state.viewed_topics:
		store.record_event(account.id, 'topic_view', topic_id=topic_id)
	for post_id in state.liked_posts:
		store.record_event(account.id, 'like_given', post_id=post_id)
	for _ in range(state.total_replies):
		store.record_event(account.id, 'post_read')


def build_browser_for_account(config: BrowserConfig, store: AccountStore, account: Account) -> LinuxDoBrowser:
	migrate_legacy_state(store, account)
	state = BrowserState.load_for_account(store, account)
	return LinuxDoBrowser(config, state, account=account, store=store)


def latest_metrics(store: AccountStore, account: Account) -> dict[str, Any]:
	metrics = store.aggregate_metrics(account.id)
	snapshot = store.latest_snapshot(account.id)
	if snapshot is not None:
		metrics.update(
			{
				'level': snapshot.level,
				'days_visited': max(int(metrics.get('days_visited', 0)), snapshot.days_visited),
				'topics_entered': max(int(metrics.get('topics_entered', 0)), snapshot.topics_entered),
				'posts_read': max(int(metrics.get('posts_read', 0)), snapshot.posts_read),
				'read_minutes': max(int(metrics.get('read_minutes', 0)), snapshot.read_minutes),
				'likes_given': max(int(metrics.get('likes_given', 0)), snapshot.likes_given),
				'likes_received': snapshot.likes_received,
				'replied_topics': max(int(metrics.get('replied_topics', 0)), snapshot.replied_topics),
				'flags_received': snapshot.flags_received,
				'suspended_or_silenced': snapshot.suspended_or_silenced,
			}
			)
	connect_snapshot = store.latest_connect_snapshot(account.id)
	if connect_snapshot is not None and connect_snapshot.current_level is not None:
		metrics['level'] = connect_snapshot.current_level
	metrics.setdefault('days_visited_100d', metrics.get('days_visited', 0))
	metrics.setdefault('recent_topics_viewed', metrics.get('topics_entered', 0))
	metrics.setdefault('recent_posts_read', metrics.get('posts_read', 0))
	metrics.setdefault('manual_promotion', False)
	metrics.setdefault('level', TrustLevelPlanner(metrics).current_level())
	return metrics


async def sync_status_for_account(config: BrowserConfig, store: AccountStore, account: Account) -> ConnectStatus:
	browser = build_browser_for_account(config, store, account)
	async with async_playwright() as playwright:
		try:
			await browser.launch(playwright)
			return await browser.sync_connect_status()
		finally:
			await browser.close()


async def try_sync_status_for_account(config: BrowserConfig, store: AccountStore, account: Account) -> bool:
	try:
		status = await sync_status_for_account(config, store, account)
	except Exception as exc:
		console.print(f'[yellow]Connect 状态同步失败，保留本地缓存:[/] {exc}')
		return False
	level = '-' if status.current_level is None else str(status.current_level)
	console.print(f'[green]✓ Connect 状态已同步，当前等级: {level}[/]')
	return True


async def cmd_login(args: argparse.Namespace) -> int:
	config = load_config()
	if args.headless:
		config.headless = True
	save_config(config)
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	browser = build_browser_for_account(config, store, account)

	async with async_playwright() as playwright:
		try:
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
				await asyncio.to_thread(tui_pause, '按 Enter 保存 session')
			finally:
				browser.running = False
				verify_task.cancel()
				with contextlib.suppress(asyncio.CancelledError):
					await verify_task
			await browser.handle_human_verification()
			if await browser.is_logged_in():
				store.update_username(account.id, await browser.get_current_username())
				console.print('[green]✓ 登录状态已保存[/]')
			else:
				console.print('[yellow]未检测到登录元素，session 仍会保存，运行 run 时再验证[/]')
		finally:
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
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	browser = build_browser_for_account(config, store, account)
	async with async_playwright() as playwright:
		try:
			await browser.launch(playwright)
			await browser.context.add_cookies(cast(Any, cookies))
			await browser.page.goto(f'{BASE_URL}/latest', wait_until='domcontentloaded')
			await browser.handle_human_verification()
			if await browser.is_logged_in():
				store.update_username(account.id, await browser.get_current_username())
				console.print(f'[green]✓ 已导入 {len(cookies)} 个 Cookie 并验证登录成功[/]')
			else:
				console.print(f'[yellow]已导入 {len(cookies)} 个 Cookie，但未检测到登录状态，Cookie 可能已过期[/]')
		finally:
			await browser.close()
	return 0


async def cmd_run(args: argparse.Namespace) -> int:
	config = load_config()
	apply_cli_overrides(config, args)
	save_config(config)
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	browser = build_browser_for_account(config, store, account)
	run_id = store.start_run(account.id)

	console.print(
		Panel(
			f'账号: [cyan]{account.name}[/]  '
			f'速度: [cyan]{config.speed}[/]  列表: [cyan]{config.list_type}[/]  '
			f'点赞: [cyan]{"开" if config.enable_like else "关"}[/]  '
			f'节奏: [cyan]{config.like_chance}[/]  '
			f'每话题: [cyan]{config.max_topic_pages} 页[/]  '
			f'目标分钟: [cyan]{config.min_read_minutes_per_session or "无"}[/]',
			title='Linux.do 自动浏览',
			border_style='bright_blue',
		)
	)

	try:
		async with async_playwright() as playwright:
			try:
				await browser.launch(playwright)
				await browser.run()
			finally:
				await browser.close()
	except KeyboardInterrupt:
		browser.stop()
		store.finish_run(
			run_id,
			'stopped',
			topics_viewed=browser.state.session_viewed,
			posts_read=browser.state.session_replies,
			likes_given=browser.state.session_liked,
		)
		console.print('\n[dim]已停止[/]')
	except RuntimeError as exc:
		store.finish_run(run_id, 'error', error=str(exc))
		error_console.print(f'[bold red]ERROR:[/] {exc}')
		return 1
	except TimeoutError as exc:
		store.finish_run(run_id, 'timeout', error=str(exc))
		error_console.print(f'[bold red]TIMEOUT:[/] {exc}')
		return 1
	else:
		store.finish_run(
			run_id,
			'success',
			topics_viewed=browser.state.session_viewed,
			posts_read=browser.state.session_replies,
			likes_given=browser.state.session_liked,
		)
	finally:
		store.record_snapshot(account.id, latest_metrics(store, account))

	await try_sync_status_for_account(config, store, account)
	browser.print_stats()
	cmd_status_for_account(store, account)
	return 0


async def cmd_run_all(args: argparse.Namespace) -> int:
	config = load_config()
	apply_cli_overrides(config, args)
	save_config(config)
	store = get_store()
	accounts = [account for account in store.list_accounts() if account.enabled]
	if not accounts:
		error_console.print('[bold red]没有可运行的账号，请先执行 accounts add[/]')
		return 1

	exit_code = 0
	for index, account in enumerate(accounts):
		args.account = account.slug
		code = await cmd_run(args)
		exit_code = max(exit_code, code)
		if index < len(accounts) - 1:
			delay = random.randint(config.run_all_cooldown_min_sec, config.run_all_cooldown_max_sec)
			console.print(f'[dim]账号间冷却 {delay}s[/]')
			await asyncio.sleep(delay)
	return exit_code


def cmd_stats(args: argparse.Namespace) -> int:
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	metrics = latest_metrics(store, account)
	table = Table(title=f'Linux.do 浏览记录 - {account.name}', header_style='bold cyan')
	table.add_column('项目')
	table.add_column('数量', justify='right')
	table.add_row('已浏览话题', str(metrics.get('topics_entered', 0)))
	table.add_row('已点赞帖子', str(metrics.get('likes_given', 0)))
	table.add_row('累计已读帖子', str(metrics.get('posts_read', 0)))
	table.add_row('累计阅读分钟', str(metrics.get('read_minutes', 0)))
	daily = store.daily_event_counts(account.id)
	table.add_row('今日浏览话题', str(daily['topic_view']))
	table.add_row('今日点赞', str(daily['like_given']))
	console.print(table)
	config = load_config()
	console.print(
		f'当前配置: speed={config.speed}, list={config.list_type}, like={config.enable_like}, '
		f'like_pacing={config.like_chance}, max_topic_pages={config.max_topic_pages}, '
		f'min_read_minutes={config.min_read_minutes_per_session}'
	)
	return 0


def cmd_clear(args: argparse.Namespace) -> int:
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	store.clear_account_events(account.id)
	console.print(f'[green]已清除账号 {account.name} 的浏览记录和状态快照[/]')
	return 0


def cmd_reset(args: argparse.Namespace) -> int:
	if CONFIG_DIR.exists() and not args.yes:
		if not Confirm.ask(f'确认删除 {CONFIG_DIR} 下的 Linux.do 本地数据？', default=False):
			console.print('[dim]已取消[/]')
			return 1
	if CONFIG_DIR.exists():
		shutil.rmtree(CONFIG_DIR)
	console.print(f'[green]已重置 Linux.do 本地数据:[/] {CONFIG_DIR}')
	return 0


def cmd_config() -> int:
	config = load_config()
	console.print_json(json.dumps(asdict(config), ensure_ascii=False, indent=2))
	return 0


async def cmd_accounts(args: argparse.Namespace) -> int:
	store = get_store()
	subcommand = args.accounts_command or 'list'
	if subcommand == 'add':
		try:
			account = store.add_account(args.name, target_level=args.target_level)
		except ValueError as exc:
			error_console.print(f'[bold red]ERROR:[/] {exc}')
			return 1
		console.print(f'[green]已添加账号:[/] {account.name} ({account.slug})')
		console.print(f'[dim]登录目录: {account.profile_dir}[/]')
		args.account = account.slug
		args.headless = getattr(args, 'headless', False)
		return await cmd_login(args)
	if subcommand == 'list':
		table = Table(title='Linux.do 账号', header_style='bold cyan')
		table.add_column('ID', justify='right')
		table.add_column('名称')
		table.add_column('Slug')
		table.add_column('用户名')
		table.add_column('目标等级', justify='right')
		table.add_column('Profile')
		for account in store.list_accounts():
			table.add_row(
				str(account.id),
				account.name,
				account.slug,
				account.username or '-',
				str(account.target_level),
				account.profile_dir,
			)
		console.print(table)
		return 0
	error_console.print(f'[bold red]未知 accounts 命令:[/] {subcommand}')
	return 1


async def cmd_status(args: argparse.Namespace) -> int:
	config = load_config()
	if getattr(args, 'headless', False):
		config.headless = True
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	if not getattr(args, 'offline', False):
		await try_sync_status_for_account(config, store, account)
	cmd_status_for_account(store, account)
	return 0


async def cmd_sync_status(args: argparse.Namespace) -> int:
	config = load_config()
	if getattr(args, 'headless', False):
		config.headless = True
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	if not await try_sync_status_for_account(config, store, account):
		return 1
	cmd_status_for_account(store, account)
	return 0


def cmd_reply(args: argparse.Namespace) -> int:
	if (args.reply_command or 'mark') != 'mark':
		error_console.print(f'[bold red]未知 reply 命令:[/] {args.reply_command}')
		return 1
	store = get_store()
	account = resolve_account(store, getattr(args, 'account', None))
	topic_id = str(args.topic_id).strip()
	if not topic_id:
		error_console.print('[bold red]请提供 topic_id[/]')
		return 1
	store.record_event(account.id, 'manual_reply', topic_id=topic_id)
	console.print(f'[green]已记录账号 {account.name} 手动回复话题 {topic_id}[/]')
	cmd_status_for_account(store, account)
	return 0


def print_connect_snapshot(snapshot: ConnectSnapshot | None) -> None:
	if snapshot is None:
		console.print('[dim]Connect 状态: 暂无缓存，运行 status 或 sync-status 后更新[/]')
		return
	table = Table(title='Connect 状态', header_style='bold cyan')
	table.add_column('项目')
	table.add_column('值')
	table.add_row('当前等级', '-' if snapshot.current_level is None else str(snapshot.current_level))
	table.add_row('要求等级', '-' if snapshot.requirement_level is None else str(snapshot.requirement_level))
	table.add_row('可见等级', '-' if snapshot.visible_level is None else str(snapshot.visible_level))
	if snapshot.locked_message:
		table.add_row('提示', snapshot.locked_message)
	table.add_row('同步时间', snapshot.created_at.isoformat())
	console.print(table)

	try:
		requirements = json.loads(snapshot.requirements_json)
	except json.JSONDecodeError:
		requirements = []
	if requirements:
		req_table = Table(title='Connect 要求', header_style='bold cyan')
		req_table.add_column('要求')
		req_table.add_column('当前', justify='right')
		req_table.add_column('目标', justify='right')
		req_table.add_column('状态')
		for item in requirements:
			done = item.get('done')
			state = '-' if done is None else ('完成' if done else '待完成')
			req_table.add_row(
				str(item.get('label') or '-'),
				'-' if item.get('current') is None else str(item.get('current')),
				'-' if item.get('required') is None else str(item.get('required')),
				state,
			)
		console.print(req_table)


def cmd_status_for_account(store: AccountStore, account: Account) -> None:
	metrics = latest_metrics(store, account)
	planner = TrustLevelPlanner(metrics)
	connect_snapshot = store.latest_connect_snapshot(account.id)
	print_connect_snapshot(connect_snapshot)
	current_level = connect_snapshot.current_level if connect_snapshot and connect_snapshot.current_level is not None else planner.current_level()
	next_level = min(max(current_level + 1, 1), account.target_level, 4)
	table = Table(title=f'升级进度 - {account.name}', header_style='bold cyan')
	table.add_column('等级', justify='right')
	table.add_column('要求')
	table.add_column('当前', justify='right')
	table.add_column('缺口', justify='right')
	table.add_column('状态')
	for status in planner.statuses_for_level(next_level):
		remaining = '-' if status.remaining is None else str(status.remaining)
		state = '完成' if status.done else ('需人工' if status.requirement.manual else '待完成')
		table.add_row(
			str(status.requirement.level),
			status.requirement.label,
			str(status.current),
			remaining,
			state,
		)
	console.print(table)
	actions = planner.recommended_actions(account.target_level)
	if actions:
		console.print(
			'[dim]建议: '
			+ ', '.join(f'{key}={value}' for key, value in actions.items())
			+ '[/]'
		)


def tui_pause(message: str = '按 Enter 返回') -> None:
	Prompt.ask(f'[dim]{message}[/]', default='', show_default=False)


def tui_args(command: str, **overrides: Any) -> argparse.Namespace:
	defaults: dict[str, Any] = {
		'command': command,
		'account': None,
		'headless': False,
		'speed': None,
		'list_type': None,
		'enable_like': None,
		'like_chance': None,
		'max_topics': None,
		'max_topic_pages': None,
		'min_read_minutes': None,
		'daily_topic_limit': None,
		'daily_like_limit': None,
		'accounts_command': None,
		'name': None,
		'target_level': 2,
		'offline': False,
		'reply_command': None,
		'topic_id': None,
		'yes': False,
		'cookies': None,
		'file': None,
	}
	defaults.update(overrides)
	return argparse.Namespace(**defaults)


def tui_menu(title: str, options: list[tuple[str, str]], default: str = '0') -> str:
	table = Table(title=title, header_style='bold cyan')
	table.add_column('选择', justify='center', no_wrap=True)
	table.add_column('操作')
	for key, label in options:
		table.add_row(key, label)
	console.print(table)
	return Prompt.ask('请选择', choices=[key for key, _ in options], default=default)


def tui_prompt_int(label: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
	while True:
		value = IntPrompt.ask(label, default=default)
		if value < minimum:
			console.print(f'[yellow]请输入不小于 {minimum} 的数字[/]')
			continue
		if maximum is not None and value > maximum:
			console.print(f'[yellow]请输入不大于 {maximum} 的数字[/]')
			continue
		return value


def local_date_from_datetime(value) -> Any:
	return value.date() if value.tzinfo is None else value.astimezone().date()


def format_local_datetime(value) -> str:
	local_value = value if value.tzinfo is None else value.astimezone()
	return local_value.strftime('%Y-%m-%d %H:%M')


def account_run_statuses(store: AccountStore, accounts: list[Account]) -> dict[int, tuple[bool, str]]:
	account_ids = [account.id for account in accounts]
	statuses = {account.id: (False, '-') for account in accounts}
	if not account_ids:
		return statuses
	today = datetime.now().astimezone().date()
	with store.session() as session:
		runs = list(
			session.scalars(
				select(RunSession)
				.where(RunSession.account_id.in_(account_ids))
				.order_by(RunSession.started_at.desc(), RunSession.id.desc())
			).all()
		)
	for run in runs:
		has_run_today, last_run = statuses.get(run.account_id, (False, '-'))
		if last_run == '-':
			last_run = format_local_datetime(run.started_at)
		has_run_today = has_run_today or local_date_from_datetime(run.started_at) == today
		statuses[run.account_id] = (has_run_today, last_run)
	return statuses


def tui_select_account(
	store: AccountStore,
	title: str = '选择账号',
	allow_cancel: bool = True,
	show_run_status: bool = False,
) -> str | None:
	accounts = store.list_accounts()
	if not accounts:
		accounts = [store.get_or_create_default_account()]
	run_statuses = account_run_statuses(store, accounts) if show_run_status else {}

	table = Table(title=title, header_style='bold cyan')
	table.add_column('选择', justify='center', no_wrap=True)
	table.add_column('名称')
	table.add_column('Slug')
	table.add_column('用户名')
	table.add_column('目标等级', justify='right')
	if show_run_status:
		table.add_column('今日执行', justify='center')
		table.add_column('上次执行', no_wrap=True)
	for index, account in enumerate(accounts, start=1):
		row: list[str | Text] = [
			str(index),
			account.name,
			account.slug,
			account.username or '-',
			str(account.target_level),
		]
		if show_run_status:
			has_run_today, last_run = run_statuses[account.id]
			row.extend([Text('🟢') if has_run_today else Text('', style='dim'), last_run])
		table.add_row(*row)
	if allow_cancel:
		row = ['0', '返回', '-', '-', '-']
		if show_run_status:
			row.extend(['-', '-'])
		table.add_row(*row)
	console.print(table)

	choices = [str(index) for index in range(1, len(accounts) + 1)]
	if allow_cancel:
		choices.append('0')
	choice = Prompt.ask('请选择账号', choices=choices, default='1')
	if choice == '0':
		return None
	return accounts[int(choice) - 1].slug


def tui_collect_run_args(account_slug: str | None, command: str = 'run') -> argparse.Namespace:
	config = load_config()
	speed = Prompt.ask('速度', choices=list(SPEED_PRESETS), default=config.speed)
	list_type = Prompt.ask('列表', choices=list(LIST_OPTIONS), default=config.list_type)
	max_topics = tui_prompt_int('本次最多话题', config.max_topics_per_session, 1)
	max_topic_pages = tui_prompt_int('每个话题最多浏览页数', config.max_topic_pages, 1)
	min_read_minutes = tui_prompt_int('本次目标阅读分钟（0 表示不启用）', config.min_read_minutes_per_session, 0)
	daily_topic_limit = tui_prompt_int('今日话题上限（0 表示不限）', config.daily_topic_limit, 0)
	daily_like_limit = tui_prompt_int('今日点赞上限（0 表示不限）', config.daily_like_limit, 0)
	enable_like = Confirm.ask('开启点赞', default=config.enable_like)
	like_chance = config.like_chance
	if enable_like:
		like_chance = Prompt.ask('备用点赞节奏', choices=list(LIKE_CHANCE_PRESETS), default=config.like_chance)
	headless = Confirm.ask('无头运行', default=config.headless)
	return tui_args(
		command,
		account=account_slug,
		speed=speed,
		list_type=list_type,
		max_topics=max_topics,
		max_topic_pages=max_topic_pages,
		min_read_minutes=min_read_minutes,
		daily_topic_limit=daily_topic_limit,
		daily_like_limit=daily_like_limit,
		enable_like=enable_like,
		like_chance=like_chance,
		headless=headless,
	)


def tui_edit_config() -> None:
	config = load_config()
	config.speed = Prompt.ask('默认速度', choices=list(SPEED_PRESETS), default=config.speed)
	config.list_type = Prompt.ask('默认列表', choices=list(LIST_OPTIONS), default=config.list_type)
	config.enable_like = Confirm.ask('默认开启点赞', default=config.enable_like)
	config.like_chance = Prompt.ask('默认备用点赞节奏', choices=list(LIKE_CHANCE_PRESETS), default=config.like_chance)
	config.max_topics_per_session = tui_prompt_int('默认本次最多话题', config.max_topics_per_session, 1)
	config.max_likes_per_session = tui_prompt_int('默认本次最多点赞', config.max_likes_per_session, 0)
	config.max_topic_pages = tui_prompt_int('默认每话题最多页数', config.max_topic_pages, 1)
	config.min_read_minutes_per_session = tui_prompt_int(
		'默认本次目标阅读分钟（0 表示不启用）',
		config.min_read_minutes_per_session,
		0,
	)
	config.daily_topic_limit = tui_prompt_int('默认今日话题上限（0 表示不限）', config.daily_topic_limit, 0)
	config.daily_like_limit = tui_prompt_int('默认今日点赞上限（0 表示不限）', config.daily_like_limit, 0)
	config.return_to_list_delay_ms = tui_prompt_int('返回列表延迟 ms', config.return_to_list_delay_ms, 0)
	config.validate()
	save_config(config)
	console.print('[green]配置已保存[/]')


async def tui_accounts_menu() -> None:
	while True:
		choice = tui_menu(
			'账号管理',
			[
				('1', '账号列表'),
				('2', '新增账号并登录'),
				('3', '登录或刷新选中账号'),
				('4', '导入 Cookie 到选中账号'),
				('0', '返回主菜单'),
			],
		)
		if choice == '0':
			return
		if choice == '1':
			await cmd_accounts(tui_args('accounts', accounts_command='list'))
			tui_pause()
		elif choice == '2':
			name = Prompt.ask('账号名称').strip()
			if not name:
				console.print('[yellow]账号名称不能为空[/]')
				continue
			target_level = tui_prompt_int('目标等级', 2, 1, 4)
			await cmd_accounts(tui_args('accounts', accounts_command='add', name=name, target_level=target_level))
			tui_pause()
		elif choice == '3':
			store = get_store()
			account = tui_select_account(store)
			if account is not None:
				await cmd_login(tui_args('login', account=account))
				tui_pause()
		elif choice == '4':
			store = get_store()
			account = tui_select_account(store)
			if account is not None:
				cookie_str = Prompt.ask('粘贴 Cookie 字符串', password=True).strip()
				if cookie_str:
					await cmd_import_cookies(tui_args('import-cookies', account=account, cookies=cookie_str))
				else:
					console.print('[yellow]Cookie 不能为空[/]')
				tui_pause()


async def tui_browse_menu() -> None:
	while True:
		choice = tui_menu(
			'浏览执行',
			[
				('1', '选择账号并按当前配置运行'),
				('2', '选择账号并自定义本次运行'),
				('3', '运行所有启用账号（当前配置）'),
				('4', '运行所有启用账号（自定义本次运行）'),
				('0', '返回主菜单'),
			],
		)
		if choice == '0':
			return
		if choice in {'1', '2'}:
			store = get_store()
			account = tui_select_account(store, show_run_status=True)
			if account is None:
				continue
			if choice == '1':
				await cmd_run(tui_args('run', account=account))
			else:
				await cmd_run(tui_collect_run_args(account))
			tui_pause()
		elif choice == '3':
			if Confirm.ask('按当前配置运行所有启用账号？', default=True):
				await cmd_run_all(tui_args('run-all'))
				tui_pause()
		elif choice == '4':
			await cmd_run_all(tui_collect_run_args(None, command='run-all'))
			tui_pause()


async def tui_status_menu() -> None:
	while True:
		choice = tui_menu(
			'状态与统计',
			[
				('1', '同步并查看升级状态'),
				('2', '查看本地缓存状态'),
				('3', '查看浏览统计'),
				('4', '记录手动回复话题'),
				('0', '返回主菜单'),
			],
		)
		if choice == '0':
			return
		store = get_store()
		account = tui_select_account(store)
		if account is None:
			continue
		if choice == '1':
			await cmd_status(tui_args('status', account=account, offline=False))
		elif choice == '2':
			await cmd_status(tui_args('status', account=account, offline=True))
		elif choice == '3':
			cmd_stats(tui_args('stats', account=account))
		elif choice == '4':
			topic_id = Prompt.ask('Topic ID').strip()
			if topic_id:
				cmd_reply(tui_args('reply', account=account, reply_command='mark', topic_id=topic_id))
			else:
				console.print('[yellow]Topic ID 不能为空[/]')
		tui_pause()


def tui_config_menu() -> None:
	while True:
		choice = tui_menu(
			'配置',
			[
				('1', '查看当前配置'),
				('2', '编辑默认配置'),
				('0', '返回主菜单'),
			],
		)
		if choice == '0':
			return
		if choice == '1':
			cmd_config()
		elif choice == '2':
			tui_edit_config()
		tui_pause()


def tui_data_menu() -> None:
	while True:
		choice = tui_menu(
			'数据管理',
			[
				('1', '清理选中账号的浏览记录'),
				('2', '重置全部 Linux.do 本地数据'),
				('0', '返回主菜单'),
			],
		)
		if choice == '0':
			return
		if choice == '1':
			store = get_store()
			account = tui_select_account(store)
			if account is not None and Confirm.ask('确认清理该账号的浏览记录？', default=False):
				cmd_clear(tui_args('clear', account=account))
				tui_pause()
		elif choice == '2':
			if Confirm.ask('确认重置全部 Linux.do 本地数据？', default=False):
				cmd_reset(tui_args('reset', yes=True))
				tui_pause()


async def cmd_tui(args: argparse.Namespace) -> int:
	_ = args
	console.print(
		Panel(
			'使用数字选择子菜单；现有 CLI 命令仍可直接调用。',
			title='Linux.do TUI',
			border_style='bright_blue',
		)
	)
	while True:
		try:
			choice = tui_menu(
				'主菜单',
				[
					('1', '账号管理'),
					('2', '浏览执行'),
					('3', '状态与统计'),
					('4', '配置'),
					('5', '数据管理'),
					('0', '退出'),
				],
			)
			if choice == '0':
				console.print('[dim]已退出[/]')
				return 0
			if choice == '1':
				await tui_accounts_menu()
			elif choice == '2':
				await tui_browse_menu()
			elif choice == '3':
				await tui_status_menu()
			elif choice == '4':
				tui_config_menu()
			elif choice == '5':
				tui_data_menu()
		except (KeyboardInterrupt, EOFError):
			console.print('\n[dim]已退出[/]')
			return 0
		except ValueError as exc:
			error_console.print(f'[bold red]ERROR:[/] {exc}')
			tui_pause()


async def async_main(args: argparse.Namespace) -> int:
	command = args.command or 'tui'
	if command == 'tui':
		return await cmd_tui(args)
	if command == 'accounts':
		return await cmd_accounts(args)
	if command == 'login':
		return await cmd_login(args)
	if command == 'run':
		return await cmd_run(args)
	if command == 'run-all':
		return await cmd_run_all(args)
	if command == 'stats':
		return cmd_stats(args)
	if command == 'status':
		return await cmd_status(args)
	if command == 'sync-status':
		return await cmd_sync_status(args)
	if command == 'reply':
		return cmd_reply(args)
	if command == 'clear':
		return cmd_clear(args)
	if command == 'reset':
		return cmd_reset(args)
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
		args.command = 'tui'
	return asyncio.run(async_main(args))


if __name__ == '__main__':
	raise SystemExit(main())
