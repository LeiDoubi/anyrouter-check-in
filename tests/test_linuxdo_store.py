from pathlib import Path

import pytest

from scripts.linuxdo_store import AccountStore, slugify_account_name


def make_store(tmp_path: Path) -> AccountStore:
	store = AccountStore(tmp_path / 'linuxdo.sqlite3', tmp_path / 'profiles')
	store.init_db()
	return store


def test_slugify_account_name():
	assert slugify_account_name('Main Account') == 'main-account'
	assert slugify_account_name('账号 1') == '1'
	assert slugify_account_name('***') == 'account'


def test_add_and_list_accounts(tmp_path):
	store = make_store(tmp_path)

	first = store.add_account('Main Account', target_level=3)
	second = store.add_account('Main Account 2')

	assert first.slug == 'main-account'
	assert first.target_level == 3
	assert second.slug == 'main-account-2'
	assert Path(first.profile_dir) == tmp_path / 'profiles' / 'main-account'
	assert [account.name for account in store.list_accounts()] == ['Main Account', 'Main Account 2']


def test_duplicate_account_name_fails(tmp_path):
	store = make_store(tmp_path)
	store.add_account('main')

	with pytest.raises(ValueError, match='账号已存在'):
		store.add_account('main')


def test_record_and_aggregate_events(tmp_path):
	store = make_store(tmp_path)
	account = store.add_account('main')

	store.record_event(account.id, 'topic_view', topic_id='101')
	store.record_event(account.id, 'topic_view', topic_id='101')
	store.record_event(account.id, 'post_read', topic_id='101', post_id='1')
	store.record_event(account.id, 'post_read', topic_id='101', post_id='2')
	store.record_event(account.id, 'read_minute', topic_id='101', value=3)
	store.record_event(account.id, 'like_given', post_id='2')
	store.record_event(account.id, 'manual_reply', topic_id='101')

	metrics = store.aggregate_metrics(account.id)

	assert metrics['topics_entered'] == 1
	assert metrics['posts_read'] == 2
	assert metrics['read_minutes'] == 3
	assert metrics['likes_given'] == 1
	assert metrics['replied_topics'] == 1
	assert store.viewed_topics(account.id) == {'101'}
	assert store.liked_posts(account.id) == {'2'}


def test_snapshot_and_clear(tmp_path):
	store = make_store(tmp_path)
	account = store.add_account('main')

	store.record_event(account.id, 'topic_view', topic_id='101')
	store.record_snapshot(account.id, {'level': 1, 'likes_received': 2})

	assert store.latest_snapshot(account.id).level == 1

	store.clear_account_events(account.id)

	assert store.aggregate_metrics(account.id)['topics_entered'] == 0
	assert store.latest_snapshot(account.id) is None
