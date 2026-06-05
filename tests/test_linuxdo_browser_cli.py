import codecs
import os

import pytest

from scripts.linuxdo import linuxdo_browser
from scripts.linuxdo.linuxdo_browser import (
	BrowserConfig,
	BrowserState,
	LinuxDoBrowser,
	build_parser,
	extract_json_payload,
	llm_candidate_topics_for_account,
	next_topic_post_number,
	normalize_openai_base_url,
	sync_status_for_account,
	topic_list_has_new_content,
	tui_args,
)
from scripts.linuxdo.linuxdo_store import AccountStore


def test_legacy_entrypoint_module_exports_main():
	from scripts import linuxdo_browser as legacy_linuxdo_browser

	assert legacy_linuxdo_browser.main is linuxdo_browser.main


def test_parser_accepts_account_commands():
	parser = build_parser()

	args = parser.parse_args(['accounts', 'add', 'main', '--target-level', '3'])

	assert args.command == 'accounts'
	assert args.accounts_command == 'add'
	assert args.name == 'main'
	assert args.target_level == 3


def test_parser_accepts_account_scoped_run_and_reply():
	parser = build_parser()

	run_args = parser.parse_args(
		[
			'run',
			'--account',
			'main',
			'--max-topics',
			'5',
			'--max-topic-pages',
			'4',
			'--min-read-minutes',
			'10',
			'--daily-topic-limit',
			'3',
			'--daily-like-limit',
			'2',
			'--no-like',
		]
	)
	reply_args = parser.parse_args(['reply', 'mark', '123', '--account', 'main'])

	assert run_args.command == 'run'
	assert run_args.account == 'main'
	assert run_args.max_topics == 5
	assert run_args.max_topic_pages == 4
	assert run_args.min_read_minutes == 10
	assert run_args.daily_topic_limit == 3
	assert run_args.daily_like_limit == 2
	assert run_args.enable_like is False
	assert reply_args.command == 'reply'
	assert reply_args.reply_command == 'mark'
	assert reply_args.topic_id == '123'


def test_parser_accepts_status_sync_and_reset_commands():
	parser = build_parser()

	status_args = parser.parse_args(['status', '--account', 'main', '--offline'])
	sync_args = parser.parse_args(['sync-status', '--account', 'main', '--headless'])
	reset_args = parser.parse_args(['reset', '--yes'])
	tui_command_args = parser.parse_args(['tui'])
	default_args = parser.parse_args([])

	assert status_args.command == 'status'
	assert status_args.offline is True
	assert sync_args.command == 'sync-status'
	assert sync_args.headless is True
	assert reset_args.command == 'reset'
	assert reset_args.yes is True
	assert tui_command_args.command == 'tui'
	assert default_args.command is None


def test_parser_accepts_review_and_llm_reply_commands():
	parser = build_parser()

	review_args = parser.parse_args(['review-likes', '--account', 'main'])
	llm_args = parser.parse_args(['llm-reply', '--account', 'main', '--count', '2'])
	no_llm_args = parser.parse_args(['llm-reply', '--account', 'main', '--no-llm-reply'])

	assert review_args.command == 'review-likes'
	assert review_args.account == 'main'
	assert llm_args.command == 'llm-reply'
	assert llm_args.count == 2
	assert llm_args.enable_llm_reply is None
	assert no_llm_args.enable_llm_reply is False


def test_parser_accepts_run_all_skip_today_option():
	parser = build_parser()

	default_args = parser.parse_args(['run-all'])
	skip_args = parser.parse_args(['run-all', '--skip-run-today'])
	no_skip_args = parser.parse_args(['run-all', '--no-skip-run-today'])

	assert default_args.skip_run_today is False
	assert skip_args.skip_run_today is True
	assert no_skip_args.skip_run_today is False


def test_tui_args_provides_cli_defaults():
	args = tui_args('run', account='main')

	assert args.command == 'run'
	assert args.account == 'main'
	assert args.headless is False
	assert args.max_topics is None
	assert args.enable_like is None
	assert args.enable_llm_reply is None
	assert args.skip_run_today is False


def test_tui_menu_clears_terminal_before_render(monkeypatch):
	class FakeConsole:
		is_terminal = True

		def __init__(self):
			self.cleared = 0
			self.rendered = []

		def clear(self):
			self.cleared += 1

		def print(self, value):
			self.rendered.append(value)

	fake_console = FakeConsole()
	monkeypatch.setattr(linuxdo_browser, 'console', fake_console)
	monkeypatch.setattr(linuxdo_browser.Prompt, 'ask', lambda *_args, **_kwargs: '0')

	choice = linuxdo_browser.tui_menu('测试菜单', [('0', '退出')])

	assert choice == '0'
	assert fake_console.cleared == 1
	assert fake_console.rendered


def test_read_terminal_key_maps_up_and_down_arrows():
	decoder = codecs.getincrementaldecoder('utf-8')('ignore')
	read_fd, write_fd = os.pipe()
	try:
		os.write(write_fd, b'\x1b[A\x1b[B')
		assert linuxdo_browser.read_terminal_key(read_fd, decoder) == 'up'
		assert linuxdo_browser.read_terminal_key(read_fd, decoder) == 'down'
	finally:
		os.close(read_fd)
		os.close(write_fd)


def test_browser_config_enables_llm_reply_by_default():
	assert BrowserConfig().enable_llm_reply is True


def test_openai_base_url_normalization_and_json_extraction():
	assert normalize_openai_base_url('https://dashscope.aliyuncs.com/compatible-mode/v1') == (
		'https://dashscope.aliyuncs.com/compatible-mode/v1'
	)
	assert normalize_openai_base_url('https://example.com/openai') == 'https://example.com/openai/v1'
	assert extract_json_payload('```json\n{"reply": "ok"}\n```') == {'reply': 'ok'}


def test_topic_list_detects_new_ids_without_height_change():
	assert topic_list_has_new_content({'101', '102'}, {'101', '102', '103'}, 3000, 3000) is True
	assert topic_list_has_new_content({'101', '102'}, {'101', '102'}, 3000, 3600) is True
	assert topic_list_has_new_content({'101', '102'}, {'101', '102'}, 3000, 3000) is False


def test_next_topic_post_number_stops_at_highest_post():
	assert next_topic_post_number({1, 2, 3}, 5) == 4
	assert next_topic_post_number({1, 2, 3}, 3) is None
	assert next_topic_post_number(set(), 5) is None
	assert next_topic_post_number({1}, None) is None


def test_llm_candidate_topics_exclude_processed_today(tmp_path):
	store = AccountStore(tmp_path / 'linuxdo.sqlite3', tmp_path / 'profiles')
	store.init_db()
	account = store.add_account('main')

	store.record_event(account.id, 'topic_view', topic_id='101')
	store.record_event(account.id, 'topic_view', topic_id='102')
	store.upsert_topic_snapshot(account.id, '101', 'Processed topic', 'https://linux.do/t/topic/101/1')
	store.upsert_topic_snapshot(account.id, '102', 'Fresh topic', 'https://linux.do/t/topic/102/1')
	store.record_event(account.id, 'llm_processed', topic_id='101')

	assert [topic['topic_id'] for topic in llm_candidate_topics_for_account(store, account)] == ['102']


@pytest.mark.asyncio
async def test_llm_reply_consumes_screened_topics_even_when_none_selected(tmp_path, monkeypatch):
	store = AccountStore(tmp_path / 'linuxdo.sqlite3', tmp_path / 'profiles')
	store.init_db()
	account = store.add_account('main')

	for topic_id in ('101', '102'):
		store.record_event(account.id, 'topic_view', topic_id=topic_id)
		store.upsert_topic_snapshot(account.id, topic_id, f'Topic {topic_id}', f'https://linux.do/t/topic/{topic_id}/1')

	def fake_select_topic_ids(_config, topics, _count):
		assert {topic['topic_id'] for topic in topics} == {'101', '102'}
		return []

	monkeypatch.setattr(linuxdo_browser, 'load_config', BrowserConfig)
	monkeypatch.setattr(linuxdo_browser, 'get_store', lambda: store)
	monkeypatch.setattr(linuxdo_browser, 'select_llm_topic_ids', fake_select_topic_ids)

	result = await linuxdo_browser.cmd_llm_reply(tui_args('llm-reply', account='main', count=1))

	assert result == 1
	assert llm_candidate_topics_for_account(store, account) == []


@pytest.mark.asyncio
async def test_review_likes_only_likes_one_post_per_topic(tmp_path, monkeypatch):
	store = AccountStore(tmp_path / 'linuxdo.sqlite3', tmp_path / 'profiles')
	store.init_db()
	account = store.add_account('main')
	store.record_event(account.id, 'topic_view', topic_id='101')
	store.upsert_topic_snapshot(account.id, '101', 'Useful topic', 'https://linux.do/t/topic/101/1')
	store.upsert_post_snapshot(account.id, '101', 'p1', 'short candidate', author='a')
	store.upsert_post_snapshot(account.id, '101', 'p2', 'longer candidate that should be reviewed first', author='b')

	class FakeStdin:
		def isatty(self):
			return True

	class FakePage:
		def __init__(self):
			self.urls = []

		async def goto(self, url, wait_until):
			self.urls.append((url, wait_until))

	class FakeBrowser:
		def __init__(self):
			self.config = BrowserConfig(max_likes_per_session=2, daily_like_limit=0, headless=False)
			self.state = BrowserState()
			self.page = FakePage()
			self.sent_likes = []

		def daily_topic_limit_reached(self):
			return True

		async def handle_human_verification(self):
			return None

		async def send_like(self, post_id):
			self.sent_likes.append(post_id)
			return {'success': True}

		def _record_like_success(self, post_id, topic_id=None):
			self.state.liked_posts.add(post_id)
			self.state.session_liked += 1
			store.record_event(account.id, 'like_given', topic_id=topic_id, post_id=post_id)

	prompted = []

	def fake_prompt_ask(*_args, **_kwargs):
		prompted.append(True)
		return 'y'

	browser = FakeBrowser()
	monkeypatch.setattr(linuxdo_browser.sys, 'stdin', FakeStdin())
	monkeypatch.setattr(linuxdo_browser.Prompt, 'ask', fake_prompt_ask)

	await linuxdo_browser.review_like_candidates(browser, store, account)

	assert prompted == [True]
	assert browser.sent_likes == ['p2']
	assert store.liked_topics(account.id) == {'101'}


@pytest.mark.asyncio
async def test_run_all_skips_accounts_run_today(tmp_path, monkeypatch):
	store = AccountStore(tmp_path / 'linuxdo.sqlite3', tmp_path / 'profiles')
	store.init_db()
	first = store.add_account('first')
	second = store.add_account('second')
	store.start_run(first.id)
	run_accounts = []

	async def fake_cmd_run(args):
		run_accounts.append(args.account)
		return 0

	monkeypatch.setattr(linuxdo_browser, 'load_config', BrowserConfig)
	monkeypatch.setattr(linuxdo_browser, 'save_config', lambda _config: None)
	monkeypatch.setattr(linuxdo_browser, 'get_store', lambda: store)
	monkeypatch.setattr(linuxdo_browser, 'cmd_run', fake_cmd_run)

	result = await linuxdo_browser.cmd_run_all(tui_args('run-all', skip_run_today=True))

	assert result == 0
	assert run_accounts == [second.slug]


@pytest.mark.asyncio
async def test_scroll_down_uses_mouse_wheel():
	class FakeMouse:
		def __init__(self):
			self.moves = []
			self.wheels = []

		async def move(self, x, y):
			self.moves.append((x, y))

		async def wheel(self, delta_x, delta_y):
			self.wheels.append((delta_x, delta_y))

	class FakePage:
		viewport_size = {'width': 1000, 'height': 800}

		def __init__(self):
			self.mouse = FakeMouse()

		async def evaluate(self, *_args):
			raise AssertionError('scroll_down should use mouse wheel before JS fallback')

	page = FakePage()
	browser = LinuxDoBrowser(BrowserConfig(), BrowserState())
	browser._page = page

	await browser.scroll_down({'scroll_step': 400})

	assert page.mouse.moves
	assert page.mouse.wheels
	assert page.mouse.wheels[0][0] == 0
	assert page.mouse.wheels[0][1] > 0


@pytest.mark.asyncio
async def test_page_scroll_uses_programmatic_scroll_before_wheel():
	class FakeMouse:
		def __init__(self):
			self.wheels = []

		async def move(self, *_args):
			raise AssertionError('page_scroll should use JS scroll before mouse movement')

		async def wheel(self, delta_x, delta_y):
			self.wheels.append((delta_x, delta_y))

	class FakePage:
		viewport_size = {'width': 1000, 'height': 800}

		def __init__(self):
			self.mouse = FakeMouse()
			self.evaluations = []

		async def evaluate(self, script, step):
			self.evaluations.append((script, step))
			return True

	page = FakePage()
	browser = LinuxDoBrowser(BrowserConfig(), BrowserState())
	browser._page = page

	await browser.scroll_down({'scroll_step': 400}, page_scroll=True)

	assert page.evaluations
	assert not page.mouse.wheels


def test_browser_config_validates_target_level():
	config = BrowserConfig(target_level=4)
	config.validate()

	config.target_level = 5
	try:
		config.validate()
	except ValueError as exc:
		assert 'target_level' in str(exc)
	else:
		raise AssertionError('expected invalid target_level to fail')


def test_browser_config_validates_daily_limits():
	config = BrowserConfig(daily_topic_limit=0, daily_like_limit=0)
	config.validate()

	config.daily_topic_limit = -1
	try:
		config.validate()
	except ValueError as exc:
		assert 'daily_topic_limit' in str(exc)
	else:
		raise AssertionError('expected invalid daily_topic_limit to fail')


def test_browser_config_validates_max_topic_pages():
	config = BrowserConfig(max_topic_pages=1)
	config.validate()

	config.max_topic_pages = 0
	try:
		config.validate()
	except ValueError as exc:
		assert 'max_topic_pages' in str(exc)
	else:
		raise AssertionError('expected invalid max_topic_pages to fail')


def test_browser_config_validates_min_read_minutes():
	config = BrowserConfig(min_read_minutes_per_session=0)
	config.validate()

	config.min_read_minutes_per_session = -1
	try:
		config.validate()
	except ValueError as exc:
		assert 'min_read_minutes_per_session' in str(exc)
	else:
		raise AssertionError('expected invalid min_read_minutes_per_session to fail')


def test_min_read_minutes_overrides_max_topics_limit():
	browser = LinuxDoBrowser(
		BrowserConfig(max_topics_per_session=1, min_read_minutes_per_session=2),
		BrowserState(session_viewed=1, session_read_minutes=1),
	)

	assert browser.max_topics_limit_reached() is False

	browser.state.session_read_minutes = 2
	assert browser.max_topics_limit_reached() is True


def test_read_minutes_accumulate_across_short_topics():
	browser = LinuxDoBrowser(BrowserConfig(), BrowserState())

	browser.record_read_time('101', 59)
	assert browser.state.session_read_minutes == 0

	browser.record_read_time('102', 1)
	assert browser.state.session_read_minutes == 1


def test_topic_like_interval_uses_daily_limit_ratio():
	browser = LinuxDoBrowser(BrowserConfig(daily_topic_limit=20, daily_like_limit=5), BrowserState(session_viewed=3))

	assert browser.topic_like_interval() == 4
	assert browser.should_like_topic() is False

	browser.state.session_viewed = 4
	assert browser.should_like_topic() is True

	browser.state.session_liked = 1
	assert browser.should_like_topic() is False


@pytest.mark.asyncio
async def test_sync_status_closes_browser_on_error(monkeypatch):
	class FakeBrowser:
		closed = False

		async def launch(self, playwright):
			return None

		async def sync_connect_status(self):
			raise RuntimeError('boom')

		async def close(self):
			self.closed = True

	class FakePlaywright:
		async def __aenter__(self):
			return object()

		async def __aexit__(self, exc_type, exc, tb):
			return False

	fake_browser = FakeBrowser()

	monkeypatch.setattr(linuxdo_browser, 'build_browser_for_account', lambda config, store, account: fake_browser)
	monkeypatch.setattr(linuxdo_browser, 'async_playwright', lambda: FakePlaywright())

	with pytest.raises(RuntimeError, match='boom'):
		await sync_status_for_account(BrowserConfig(), object(), object())

	assert fake_browser.closed is True
