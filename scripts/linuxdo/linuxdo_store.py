from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utc_now() -> datetime:
	return datetime.now(timezone.utc)


def slugify_account_name(name: str) -> str:
	slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', name.strip().lower()).strip('-')
	return slug or 'account'


class Base(DeclarativeBase):
	pass


class Account(Base):
	__tablename__ = 'accounts'

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
	slug: Mapped[str] = mapped_column(String(140), unique=True, nullable=False)
	profile_dir: Mapped[str] = mapped_column(Text, nullable=False)
	username: Mapped[str | None] = mapped_column(String(120), nullable=True)
	target_level: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
	enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
	updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

	events: Mapped[list[ActivityEvent]] = relationship(back_populates='account', cascade='all, delete-orphan')
	snapshots: Mapped[list[MetricSnapshot]] = relationship(back_populates='account', cascade='all, delete-orphan')
	run_sessions: Mapped[list[RunSession]] = relationship(back_populates='account', cascade='all, delete-orphan')
	connect_snapshots: Mapped[list[ConnectSnapshot]] = relationship(
		back_populates='account',
		cascade='all, delete-orphan',
	)


class ActivityEvent(Base):
	__tablename__ = 'activity_events'

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
	topic_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
	post_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
	value: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

	account: Mapped[Account] = relationship(back_populates='events')


class MetricSnapshot(Base):
	__tablename__ = 'metric_snapshots'

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	days_visited: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	topics_entered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	posts_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	read_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	likes_given: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	likes_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	replied_topics: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	flags_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	suspended_or_silenced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

	account: Mapped[Account] = relationship(back_populates='snapshots')


class RunSession(Base):
	__tablename__ = 'run_sessions'

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
	ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
	status: Mapped[str] = mapped_column(String(30), default='running', nullable=False)
	error: Mapped[str | None] = mapped_column(Text, nullable=True)
	topics_viewed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	posts_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	likes_given: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

	account: Mapped[Account] = relationship(back_populates='run_sessions')


class ConnectSnapshot(Base):
	__tablename__ = 'connect_snapshots'

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	current_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
	requirement_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
	visible_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
	locked_message: Mapped[str | None] = mapped_column(Text, nullable=True)
	requirements_json: Mapped[str] = mapped_column(Text, default='[]', nullable=False)
	raw_text: Mapped[str] = mapped_column(Text, default='', nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

	account: Mapped[Account] = relationship(back_populates='connect_snapshots')


class TopicSnapshot(Base):
	__tablename__ = 'topic_snapshots'
	__table_args__ = (UniqueConstraint('account_id', 'topic_id', name='uq_topic_snapshots_account_topic'),)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	topic_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
	title: Mapped[str] = mapped_column(Text, default='', nullable=False)
	url: Mapped[str] = mapped_column(Text, default='', nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
	updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class PostSnapshot(Base):
	__tablename__ = 'post_snapshots'
	__table_args__ = (UniqueConstraint('account_id', 'post_id', name='uq_post_snapshots_account_post'),)

	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	account_id: Mapped[int] = mapped_column(ForeignKey('accounts.id'), nullable=False, index=True)
	topic_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
	post_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
	author: Mapped[str | None] = mapped_column(String(120), nullable=True)
	text: Mapped[str] = mapped_column(Text, default='', nullable=False)
	excerpt: Mapped[str] = mapped_column(Text, default='', nullable=False)
	text_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
	url: Mapped[str] = mapped_column(Text, default='', nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
	updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class AccountStore:
	def __init__(self, db_path: Path, profiles_dir: Path) -> None:
		self.db_path = db_path
		self.profiles_dir = profiles_dir
		self.db_path.parent.mkdir(parents=True, exist_ok=True)
		self.profiles_dir.mkdir(parents=True, exist_ok=True)
		self.engine = create_engine(f'sqlite:///{self.db_path}', future=True)
		self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

	def init_db(self) -> None:
		Base.metadata.create_all(self.engine)

	@contextmanager
	def session(self) -> Iterator[Session]:
		session = self.Session()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	def unique_slug(self, session: Session, name: str) -> str:
		base = slugify_account_name(name)
		slug = base
		index = 2
		while session.scalar(select(Account).where(Account.slug == slug)) is not None:
			slug = f'{base}-{index}'
			index += 1
		return slug

	def add_account(self, name: str, target_level: int = 2) -> Account:
		with self.session() as session:
			existing = session.scalar(select(Account).where(Account.name == name))
			if existing is not None:
				raise ValueError(f'账号已存在: {name}')
			slug = self.unique_slug(session, name)
			account = Account(
				name=name,
				slug=slug,
				profile_dir=str(self.profiles_dir / slug),
				target_level=target_level,
			)
			session.add(account)
			session.flush()
			return account

	def get_account(self, name_or_slug: str | None = None) -> Account:
		with self.session() as session:
			if name_or_slug:
				account = session.scalar(
					select(Account).where((Account.name == name_or_slug) | (Account.slug == name_or_slug))
				)
			else:
				account = session.scalar(select(Account).order_by(Account.id))
			if account is None:
				label = name_or_slug or '默认账号'
				raise ValueError(f'账号不存在: {label}')
			return account

	def get_or_create_default_account(self) -> Account:
		with self.session() as session:
			account = session.scalar(select(Account).order_by(Account.id))
			if account is not None:
				return account
			account = Account(name='default', slug='default', profile_dir=str(self.profiles_dir / 'default'), target_level=2)
			session.add(account)
			session.flush()
			return account

	def list_accounts(self) -> list[Account]:
		with self.session() as session:
			return list(session.scalars(select(Account).order_by(Account.id)).all())

	def update_username(self, account_id: int, username: str | None) -> None:
		if not username:
			return
		with self.session() as session:
			account = session.get(Account, account_id)
			if account is not None:
				account.username = username
				account.updated_at = utc_now()

	def update_profile_dir(self, account_id: int, profile_dir: Path) -> None:
		with self.session() as session:
			account = session.get(Account, account_id)
			if account is not None:
				account.profile_dir = str(profile_dir)
				account.updated_at = utc_now()

	def record_event(
		self,
		account_id: int,
		event_type: str,
		topic_id: str | None = None,
		post_id: str | None = None,
		value: int = 1,
		created_at: datetime | None = None,
	) -> None:
		with self.session() as session:
			session.add(
				ActivityEvent(
					account_id=account_id,
					event_type=event_type,
					topic_id=topic_id,
					post_id=post_id,
					value=value,
					created_at=created_at or datetime.now().astimezone(),
				)
			)

	def record_snapshot(self, account_id: int, metrics: dict[str, Any]) -> MetricSnapshot:
		allowed = {
			'level',
			'days_visited',
			'topics_entered',
			'posts_read',
			'read_minutes',
			'likes_given',
			'likes_received',
			'replied_topics',
			'flags_received',
			'suspended_or_silenced',
		}
		payload = {key: value for key, value in metrics.items() if key in allowed}
		with self.session() as session:
			snapshot = MetricSnapshot(account_id=account_id, **payload)
			session.add(snapshot)
			session.flush()
			return snapshot

	def latest_snapshot(self, account_id: int) -> MetricSnapshot | None:
		with self.session() as session:
			return session.scalar(
				select(MetricSnapshot)
				.where(MetricSnapshot.account_id == account_id)
				.order_by(MetricSnapshot.created_at.desc(), MetricSnapshot.id.desc())
			)

	def aggregate_metrics(self, account_id: int) -> dict[str, int | bool]:
		with self.session() as session:
			events = list(session.scalars(select(ActivityEvent).where(ActivityEvent.account_id == account_id)).all())
			return {
				'topics_entered': len({event.topic_id for event in events if event.event_type == 'topic_view' and event.topic_id}),
				'posts_read': sum(1 for event in events if event.event_type == 'post_read'),
				'read_minutes': sum(event.value for event in events if event.event_type == 'read_minute'),
				'likes_given': sum(1 for event in events if event.event_type == 'like_given'),
				'likes_received': 0,
				'replied_topics': len({
					event.topic_id
					for event in events
					if event.event_type in {'manual_reply', 'llm_reply'} and event.topic_id
				}),
				'days_visited': len({event.created_at.date() for event in events}),
				'flags_received': 0,
				'suspended_or_silenced': False,
			}

	def viewed_topics(self, account_id: int) -> set[str]:
		with self.session() as session:
			events = session.scalars(
				select(ActivityEvent).where(
					ActivityEvent.account_id == account_id,
					ActivityEvent.event_type == 'topic_view',
					ActivityEvent.topic_id.is_not(None),
				)
			)
			return {event.topic_id for event in events if event.topic_id}

	def record_connect_snapshot(self, account_id: int, payload: dict[str, Any]) -> ConnectSnapshot:
		with self.session() as session:
			snapshot = ConnectSnapshot(
				account_id=account_id,
				current_level=payload.get('current_level'),
				requirement_level=payload.get('requirement_level'),
				visible_level=payload.get('visible_level'),
				locked_message=payload.get('locked_message'),
				requirements_json=str(payload.get('requirements_json') or '[]'),
				raw_text=str(payload.get('raw_text') or ''),
			)
			session.add(snapshot)
			session.flush()
			return snapshot

	def latest_connect_snapshot(self, account_id: int) -> ConnectSnapshot | None:
		with self.session() as session:
			return session.scalar(
				select(ConnectSnapshot)
				.where(ConnectSnapshot.account_id == account_id)
				.order_by(ConnectSnapshot.created_at.desc(), ConnectSnapshot.id.desc())
			)

	def liked_posts(self, account_id: int) -> set[str]:
		with self.session() as session:
			events = session.scalars(
				select(ActivityEvent).where(
					ActivityEvent.account_id == account_id,
					ActivityEvent.event_type == 'like_given',
					ActivityEvent.post_id.is_not(None),
					)
				)
			return {event.post_id for event in events if event.post_id}

	def upsert_topic_snapshot(self, account_id: int, topic_id: str, title: str, url: str) -> None:
		now = datetime.now().astimezone()
		with self.session() as session:
			snapshot = session.scalar(
				select(TopicSnapshot).where(
					TopicSnapshot.account_id == account_id,
					TopicSnapshot.topic_id == topic_id,
				)
			)
			if snapshot is None:
				session.add(
					TopicSnapshot(
						account_id=account_id,
						topic_id=topic_id,
						title=title.strip(),
						url=url,
						created_at=now,
						updated_at=now,
					)
				)
				return
			snapshot.title = title.strip() or snapshot.title
			snapshot.url = url or snapshot.url
			snapshot.updated_at = now

	def upsert_post_snapshot(
		self,
		account_id: int,
		topic_id: str,
		post_id: str,
		text: str,
		author: str | None = None,
		url: str = '',
	) -> None:
		normalized_text = re.sub(r'\s+', ' ', text).strip()
		now = datetime.now().astimezone()
		with self.session() as session:
			snapshot = session.scalar(
				select(PostSnapshot).where(
					PostSnapshot.account_id == account_id,
					PostSnapshot.post_id == post_id,
				)
			)
			payload = {
				'topic_id': topic_id,
				'author': author,
				'text': normalized_text[:8000],
				'excerpt': normalized_text[:240],
				'text_length': len(re.sub(r'\s+', '', normalized_text)),
				'url': url,
				'updated_at': now,
			}
			if snapshot is None:
				session.add(
					PostSnapshot(
						account_id=account_id,
						post_id=post_id,
						created_at=now,
						**payload,
					)
				)
				return
			for key, value in payload.items():
				setattr(snapshot, key, value)

	def topic_snapshots_for_day(self, account_id: int, local_day=None) -> list[TopicSnapshot]:
		target_day = local_day or datetime.now().astimezone().date()
		with self.session() as session:
			events = list(
				session.scalars(
					select(ActivityEvent)
					.where(
						ActivityEvent.account_id == account_id,
						ActivityEvent.event_type == 'topic_view',
						ActivityEvent.topic_id.is_not(None),
					)
					.order_by(ActivityEvent.created_at, ActivityEvent.id)
				).all()
			)
			ordered_topic_ids: list[str] = []
			seen: set[str] = set()
			for event in events:
				event_date = event.created_at.date() if event.created_at.tzinfo is None else event.created_at.astimezone().date()
				if event_date != target_day or not event.topic_id or event.topic_id in seen:
					continue
				ordered_topic_ids.append(event.topic_id)
				seen.add(event.topic_id)
			if not ordered_topic_ids:
				return []
			snapshots = list(
				session.scalars(
					select(TopicSnapshot).where(
						TopicSnapshot.account_id == account_id,
						TopicSnapshot.topic_id.in_(ordered_topic_ids),
					)
				).all()
			)
			by_topic_id = {snapshot.topic_id: snapshot for snapshot in snapshots}
			return [by_topic_id[topic_id] for topic_id in ordered_topic_ids if topic_id in by_topic_id]

	def post_snapshots_for_topic(self, account_id: int, topic_id: str) -> list[PostSnapshot]:
		with self.session() as session:
			return list(
				session.scalars(
					select(PostSnapshot)
					.where(PostSnapshot.account_id == account_id, PostSnapshot.topic_id == topic_id)
					.order_by(PostSnapshot.text_length.desc(), PostSnapshot.id)
				).all()
			)

	def like_candidate_posts_for_day(
		self,
		account_id: int,
		local_day=None,
		limit_per_topic: int = 2,
		exclude_post_ids: set[str] | None = None,
	) -> list[dict[str, Any]]:
		excluded = set(exclude_post_ids or set()) | self.liked_posts(account_id)
		candidates: list[dict[str, Any]] = []
		for topic in self.topic_snapshots_for_day(account_id, local_day):
			topic_posts = [
				post
				for post in self.post_snapshots_for_topic(account_id, topic.topic_id)
				if post.post_id not in excluded and post.text_length > 0
			]
			for post in topic_posts[:limit_per_topic]:
				candidates.append(
					{
						'topic_id': topic.topic_id,
						'topic_title': topic.title or topic.topic_id,
						'topic_url': topic.url,
						'post_id': post.post_id,
						'author': post.author,
						'excerpt': post.excerpt,
						'text': post.text,
						'url': post.url,
						'text_length': post.text_length,
					}
				)
		return candidates

	def daily_event_counts(self, account_id: int, local_day=None) -> dict[str, int]:
		target_day = local_day or datetime.now().astimezone().date()
		with self.session() as session:
			events = list(session.scalars(select(ActivityEvent).where(ActivityEvent.account_id == account_id)).all())
		topic_ids: set[str] = set()
		likes = 0
		for event in events:
			event_day = event.created_at
			event_date = event_day.date() if event_day.tzinfo is None else event_day.astimezone().date()
			if event_date != target_day:
				continue
			if event.event_type == 'topic_view' and event.topic_id:
				topic_ids.add(event.topic_id)
			elif event.event_type == 'like_given':
				likes += 1
		return {'topic_view': len(topic_ids), 'like_given': likes}

	def start_run(self, account_id: int) -> int:
		with self.session() as session:
			run = RunSession(account_id=account_id)
			session.add(run)
			session.flush()
			return run.id

	def finish_run(
		self,
		run_id: int,
		status: str,
		error: str | None = None,
		topics_viewed: int = 0,
		posts_read: int = 0,
		likes_given: int = 0,
	) -> None:
		with self.session() as session:
			run = session.get(RunSession, run_id)
			if run is None:
				return
			run.ended_at = utc_now()
			run.status = status
			run.error = error
			run.topics_viewed = topics_viewed
			run.posts_read = posts_read
			run.likes_given = likes_given

	def clear_account_events(self, account_id: int) -> None:
		with self.session() as session:
			for model in (ActivityEvent, MetricSnapshot, RunSession, ConnectSnapshot, TopicSnapshot, PostSnapshot):
				for row in session.scalars(select(model).where(model.account_id == account_id)):
					session.delete(row)
