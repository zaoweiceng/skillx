from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ManagedSkill(Base, TimestampMixin):
    __tablename__ = "managed_skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    package_path: Mapped[str] = mapped_column(Text)
    source_digest: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    skillx_capability_id: Mapped[str | None] = mapped_column(
        ForeignKey("capabilities.id", ondelete="SET NULL"), nullable=True
    )
    skillx_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("capability_versions.id", ondelete="SET NULL"), nullable=True
    )
    converted_source_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
