from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

CURRENT_LEVEL_PATTERN = re.compile(r'当前\s*(\d+)\s*级')
REQUIREMENT_LEVEL_PATTERN = re.compile(r'信任级别\s*(\d+)\s*的要求')
VISIBLE_LEVEL_PATTERN = re.compile(r'达到\s*(\d+)\s*级可查看')
PROGRESS_PATTERN = re.compile(r'(?P<current>\d+)\s*/\s*(?P<required>\d+)')


@dataclass(frozen=True)
class ConnectRequirement:
	label: str
	current: int | None = None
	required: int | None = None
	done: bool | None = None


@dataclass(frozen=True)
class ConnectStatus:
	current_level: int | None
	requirement_level: int | None
	visible_level: int | None
	locked_message: str | None
	requirements: list[ConnectRequirement]
	raw_text: str

	def to_store_payload(self) -> dict[str, Any]:
		return {
			'current_level': self.current_level,
			'requirement_level': self.requirement_level,
			'visible_level': self.visible_level,
			'locked_message': self.locked_message,
			'requirements_json': json.dumps([asdict(requirement) for requirement in self.requirements], ensure_ascii=False),
			'raw_text': self.raw_text,
		}


def parse_connect_status(raw_text: str) -> ConnectStatus:
	text = normalize_text(raw_text)
	current_level = first_int(CURRENT_LEVEL_PATTERN, text)
	requirement_level = first_int(REQUIREMENT_LEVEL_PATTERN, text)
	visible_level = first_int(VISIBLE_LEVEL_PATTERN, text)
	locked_message = first_matching_line(text, ('可查看', '解锁更多功能'))
	requirements = parse_requirements(text)
	return ConnectStatus(
		current_level=current_level,
		requirement_level=requirement_level,
		visible_level=visible_level,
		locked_message=locked_message,
		requirements=requirements,
		raw_text=text,
	)


def normalize_text(raw_text: str) -> str:
	lines = [' '.join(line.strip().split()) for line in raw_text.splitlines()]
	return '\n'.join(line for line in lines if line)


def first_int(pattern: re.Pattern[str], text: str) -> int | None:
	match = pattern.search(text)
	return int(match.group(1)) if match else None


def first_matching_line(text: str, needles: tuple[str, ...]) -> str | None:
	for line in text.splitlines():
		if any(needle in line for needle in needles):
			return line
	return None


def parse_requirements(text: str) -> list[ConnectRequirement]:
	requirements: list[ConnectRequirement] = []
	for line in text.splitlines():
		if not line or line.startswith('信任级别') or line.startswith('当前'):
			continue
		progress = PROGRESS_PATTERN.search(line)
		done = parse_done_state(line)
		if progress is None and done is None:
			continue
		requirements.append(
			ConnectRequirement(
				label=line,
				current=int(progress.group('current')) if progress else None,
				required=int(progress.group('required')) if progress else None,
				done=done,
			)
		)
	return requirements


def parse_done_state(line: str) -> bool | None:
	if any(token in line for token in ('未完成', '未达成', '还需', '不足', '待完成')):
		return False
	if any(token in line for token in ('已完成', '完成', '达成')):
		return True
	return None
