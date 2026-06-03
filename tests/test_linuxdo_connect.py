import json

from scripts.linuxdo.linuxdo_connect import parse_connect_status


def test_parse_connect_status_locked_level_sample():
	status = parse_connect_status(
		"""
		信任级别 3 的要求

		当前 1 级，达到 2 级可查看 3 级进度详情。
		继续参与社区，解锁更多功能！
		"""
	)

	assert status.current_level == 1
	assert status.requirement_level == 3
	assert status.visible_level == 2
	assert status.locked_message == '当前 1 级，达到 2 级可查看 3 级进度详情。'
	assert status.requirements == []


def test_parse_connect_status_requirement_progress():
	status = parse_connect_status(
		"""
		信任级别 2 的要求
		当前 1 级
		访问天数 10 / 15 待完成
		阅读帖子 100 / 100 已完成
		"""
	)

	payload = status.to_store_payload()
	requirements = json.loads(payload['requirements_json'])

	assert status.current_level == 1
	assert status.requirement_level == 2
	assert requirements[0]['current'] == 10
	assert requirements[0]['required'] == 15
	assert requirements[0]['done'] is False
	assert requirements[1]['done'] is True
