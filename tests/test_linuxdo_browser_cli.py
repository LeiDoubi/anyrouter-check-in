import pytest

from scripts.linuxdo import linuxdo_browser
from scripts.linuxdo.linuxdo_browser import (
	BrowserConfig,
	BrowserState,
	LinuxDoBrowser,
	build_parser,
	sync_status_for_account,
	tui_args,
)


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


def test_tui_args_provides_cli_defaults():
	args = tui_args('run', account='main')

	assert args.command == 'run'
	assert args.account == 'main'
	assert args.headless is False
	assert args.max_topics is None
	assert args.enable_like is None


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
