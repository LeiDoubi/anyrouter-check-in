from scripts.linuxdo_planner import TrustLevelPlanner


def test_level_one_requirements_and_actions():
	planner = TrustLevelPlanner({'topics_entered': 2, 'posts_read': 10, 'read_minutes': 4})

	assert planner.current_level() == 0

	actions = planner.recommended_actions(target_level=2)

	assert actions == {'topics_to_open': 3, 'posts_to_read': 20, 'minutes_to_read': 6}


def test_level_two_requires_manual_like_and_replies():
	planner = TrustLevelPlanner(
		{
			'topics_entered': 20,
			'posts_read': 100,
			'read_minutes': 60,
			'days_visited': 15,
			'likes_given': 1,
			'likes_received': 0,
			'replied_topics': 1,
		}
	)

	statuses = planner.statuses_for_level(2)

	assert planner.current_level() == 1
	assert [status.requirement.key for status in statuses if not status.done] == ['likes_received', 'replied_topics']
	assert [status.requirement.key for status in statuses if status.requirement.manual] == [
		'likes_received',
		'replied_topics',
	]


def test_level_three_boundaries():
	planner = TrustLevelPlanner(
		{
			'topics_entered': 20,
			'posts_read': 100,
			'read_minutes': 60,
			'days_visited': 50,
			'days_visited_100d': 50,
			'likes_given': 30,
			'likes_received': 20,
			'replied_topics': 10,
			'recent_topics_viewed': 500,
			'recent_posts_read': 20_000,
			'flags_received': 5,
			'suspended_or_silenced': False,
		}
	)

	assert planner.current_level() == 3


def test_level_four_is_manual_only():
	planner = TrustLevelPlanner({})
	statuses = planner.statuses_for_level(4)

	assert len(statuses) == 1
	assert statuses[0].requirement.manual is True
	assert statuses[0].done is False
