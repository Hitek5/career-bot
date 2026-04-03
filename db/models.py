from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    username = Column(String(100))
    full_name = Column(String(200))
    # new | uploading | answering | ready | confirming_resume
    state = Column(String(50), default="new")
    pending_analysis_id = Column(Integer, nullable=True)
    # Access control: trial (default) / paid / admin
    role = Column(String(20), default="trial")
    analyses_left = Column(Integer, default=3)
    resumes_left = Column(Integer, default=2)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    profile = relationship("Profile", back_populates="user", uselist=False)
    documents = relationship("Document", back_populates="user")
    vacancy_analyses = relationship("VacancyAnalysis", back_populates="user")
    generated_resumes = relationship("GeneratedResume", back_populates="user")


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    profile_json = Column(Text)
    summary = Column(Text)
    skills = Column(Text)
    experience = Column(Text)
    education = Column(Text)
    contacts = Column(Text)
    target_positions = Column(Text)
    salary_range = Column(String(100))
    work_format = Column(String(50))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="profile")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    doc_type = Column(String(20))   # pdf, docx, text
    filename = Column(String(500))
    content = Column(Text)
    parsed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="documents")


class VacancyAnalysis(Base):
    __tablename__ = "vacancy_analyses"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    url = Column(String(1000))
    company_name = Column(String(300))
    position = Column(String(300))
    salary = Column(String(200))
    analysis_json = Column(Text)
    match_percent = Column(Float)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="vacancy_analyses")
    generated_resumes = relationship("GeneratedResume", back_populates="vacancy_analysis")


class GeneratedResume(Base):
    __tablename__ = "generated_resumes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    vacancy_analysis_id = Column(Integer, ForeignKey("vacancy_analyses.id"), nullable=True)
    format = Column(String(10))   # pdf, docx
    file_path = Column(String(1000))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="generated_resumes")
    vacancy_analysis = relationship("VacancyAnalysis", back_populates="generated_resumes")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    provider = Column(String(20))  # stars, paymaster, manual
    currency = Column(String(10))  # XTR, RUB
    amount = Column(Integer)  # Stars or kopecks
    package = Column(String(50))  # pack_10, pack_30, unlimited
    telegram_charge_id = Column(String(200))
    provider_charge_id = Column(String(200))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User")
