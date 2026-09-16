"""Database connections and the stable model import surface."""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

from .models import Approval as Approval
from .models import Artifact as Artifact
from .models import Base as Base
from .models import BrowserRuntimeHeartbeat as BrowserRuntimeHeartbeat
from .models import Capability as Capability
from .models import CapabilityVersion as CapabilityVersion
from .models import Component as Component
from .models import Conversation as Conversation
from .models import ConversationRun as ConversationRun
from .models import ConversationRunEvent as ConversationRunEvent
from .models import ConversationTurn as ConversationTurn
from .models import Distillation as Distillation
from .models import ExecutionCheckpoint as ExecutionCheckpoint
from .models import FailureObservation as FailureObservation
from .models import HumanPatch as HumanPatch
from .models import MCPServer as MCPServer
from .models import MCPServerVersion as MCPServerVersion
from .models import MCPToolSnapshot as MCPToolSnapshot
from .models import Message as Message
from .models import ModelUsage as ModelUsage
from .models import NodeRun as NodeRun
from .models import OutputSubstitution as OutputSubstitution
from .models import PendingSelection as PendingSelection
from .models import PlanSnapshot as PlanSnapshot
from .models import RecoveryAttempt as RecoveryAttempt
from .models import ReplayEvidence as ReplayEvidence
from .models import ReplayFixture as ReplayFixture
from .models import RevisionProposal as RevisionProposal
from .models import Run as Run
from .models import RunEvent as RunEvent
from .models import RunPresentation as RunPresentation
from .models import RuntimeProfile as RuntimeProfile
from .models import SecretReference as SecretReference
from .models import TaskCheckpoint as TaskCheckpoint
from .models import TimestampMixin as TimestampMixin
from .models import Workflow as Workflow
from .models import WorkflowCapabilityManifest as WorkflowCapabilityManifest
from .models import WorkflowDisclosureCall as WorkflowDisclosureCall
from .models import WorkflowDisclosureSession as WorkflowDisclosureSession
from .models import WorkflowFragmentManifest as WorkflowFragmentManifest
from .models import WorkflowVersion as WorkflowVersion
from .models import rfc3339 as rfc3339
from .models import utcnow as utcnow

settings = get_settings()
engine: AsyncEngine = create_async_engine(settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def configure_sqlite(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def init_database() -> None:
    # API and Worker share the same versioned schema path as the CLI.
    import asyncio

    from alembic import command

    from app.reset_database import migration_config

    await asyncio.to_thread(command.upgrade, migration_config(), "head")


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
