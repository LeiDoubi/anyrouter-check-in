from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Requirement:
	key: str
	label: str
	required: int | bool
	level: int
	manual: bool = False


@dataclass(frozen=True)
class RequirementStatus:
	requirement: Requirement
	current: int | bool
	done: bool
	remaining: int | None


LEVEL_REQUIREMENTS = [
	Requirement('topics_entered', '进入至少 5 个话题', 5, 1),
	Requirement('posts_read', '阅读至少 30 篇帖子', 30, 1),
	Requirement('read_minutes', '总共花费 10 分钟阅读帖子', 10, 1),
	Requirement('days_visited', '至少访问 15 天', 15, 2),
	Requirement('likes_given', '至少点赞 1 次', 1, 2),
	Requirement('likes_received', '至少收到 1 次点赞', 1, 2, manual=True),
	Requirement('replied_topics', '回复至少 3 个不同的话题', 3, 2, manual=True),
	Requirement('topics_entered', '进入至少 20 个话题', 20, 2),
	Requirement('posts_read', '阅读至少 100 篇帖子', 100, 2),
	Requirement('read_minutes', '总共花费 60 分钟阅读帖子', 60, 2),
	Requirement('days_visited_100d', '过去 100 天内至少访问 50 天', 50, 3),
	Requirement('replied_topics', '至少在 10 个不同的非私信话题上回复', 10, 3, manual=True),
	Requirement('recent_topics_viewed', '浏览过去 100 天新话题的 25%（上限 500）', 500, 3),
	Requirement('recent_posts_read', '阅读过去 100 天新帖子的 25%（上限 20000）', 20_000, 3),
	Requirement('likes_received', '必须收到 20 个点赞', 20, 3, manual=True),
	Requirement('likes_given', '必须送出 30 个点赞', 30, 3),
	Requirement('flags_received', '不得收到超过 5 个垃圾邮件或冒犯性标记', 5, 3, manual=True),
	Requirement('suspended_or_silenced', '过去 6 个月内不能被暂停或禁言', False, 3, manual=True),
	Requirement('manual_promotion', '只能由工作人员手动提升', True, 4, manual=True),
]


class TrustLevelPlanner:
	def __init__(self, metrics: dict[str, Any]) -> None:
		self.metrics = metrics

	def current_level(self) -> int:
		if self._level_done(3):
			return 3
		if self._level_done(2):
			return 2
		if self._level_done(1):
			return 1
		return 0

	def statuses_for_level(self, level: int) -> list[RequirementStatus]:
		return [self._status(req) for req in LEVEL_REQUIREMENTS if req.level == level]

	def next_level(self, target_level: int = 2) -> int:
		current = self.current_level()
		return min(max(current + 1, 1), target_level, 4)

	def next_statuses(self, target_level: int = 2) -> list[RequirementStatus]:
		return self.statuses_for_level(self.next_level(target_level))

	def recommended_actions(self, target_level: int = 2) -> dict[str, int]:
		actions: dict[str, int] = {}
		for status in self.next_statuses(target_level):
			if status.done or status.remaining is None or status.requirement.manual:
				continue
			if status.requirement.key in {'topics_entered', 'recent_topics_viewed'}:
				actions['topics_to_open'] = max(actions.get('topics_to_open', 0), status.remaining)
			elif status.requirement.key in {'posts_read', 'recent_posts_read'}:
				actions['posts_to_read'] = max(actions.get('posts_to_read', 0), status.remaining)
			elif status.requirement.key == 'read_minutes':
				actions['minutes_to_read'] = max(actions.get('minutes_to_read', 0), status.remaining)
			elif status.requirement.key == 'likes_given':
				actions['likes_to_give'] = max(actions.get('likes_to_give', 0), status.remaining)
		return actions

	def _level_done(self, level: int) -> bool:
		statuses = self.statuses_for_level(level)
		return bool(statuses) and all(status.done for status in statuses if status.requirement.level != 4)

	def _status(self, requirement: Requirement) -> RequirementStatus:
		current = self.metrics.get(requirement.key, 0)
		required = requirement.required
		if isinstance(required, bool):
			done = bool(current) is required
			return RequirementStatus(requirement, bool(current), done, None)
		current_int = int(current or 0)
		if requirement.key == 'flags_received':
			done = current_int <= int(required)
			remaining = None if done else current_int - int(required)
		else:
			done = current_int >= int(required)
			remaining = max(int(required) - current_int, 0)
		return RequirementStatus(requirement, current_int, done, remaining)
