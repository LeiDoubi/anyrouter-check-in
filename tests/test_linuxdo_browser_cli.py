from scripts.linuxdo_browser import BrowserConfig, build_parser


def test_parser_accepts_account_commands():
	parser = build_parser()

	args = parser.parse_args(['accounts', 'add', 'main', '--target-level', '3'])

	assert args.command == 'accounts'
	assert args.accounts_command == 'add'
	assert args.name == 'main'
	assert args.target_level == 3


def test_parser_accepts_account_scoped_run_and_reply():
	parser = build_parser()

	run_args = parser.parse_args(['run', '--account', 'main', '--max-topics', '5', '--no-like'])
	reply_args = parser.parse_args(['reply', 'mark', '123', '--account', 'main'])

	assert run_args.command == 'run'
	assert run_args.account == 'main'
	assert run_args.max_topics == 5
	assert run_args.enable_like is False
	assert reply_args.command == 'reply'
	assert reply_args.reply_command == 'mark'
	assert reply_args.topic_id == '123'


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
