#!/usr/bin/env python3
"""CareerBot — AI-powered Telegram career assistant."""

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

from config import BOT_TOKEN, is_user_allowed, OUTPUT_DIR
from db.database import init_db, get_session
from db.models import User, Profile, Document, VacancyAnalysis, GeneratedResume
from core.profile_builder import build_profile
from core.vacancy_analyzer import process_vacancy_url, process_vacancy_text
from core.resume_generator import generate_resume_data, generate_pdf, generate_docx
from parsers.pdf_parser import parse_pdf
from parsers.docx_parser import parse_docx
from parsers.vacancy_parser import extract_hh_vacancy_id

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- States ---
STATE_NEW = "new"
STATE_UPLOADING = "uploading"
STATE_ANSWERING = "answering"
STATE_READY = "ready"
STATE_EDITING = "editing"

# Editable profile fields
EDIT_FIELDS = {
    "edit_summary": {"label": "📝 О себе (summary)", "key": "summary", "hint": "Напиши краткое описание себя (2-3 предложения, продающее):"},
    "edit_positions": {"label": "🎯 Целевые позиции", "key": "target_positions", "hint": "Перечисли целевые позиции через запятую:"},
    "edit_salary": {"label": "💰 Зарплата", "key": "salary_range", "hint": "Укажи желаемый диапазон зарплаты (например: 200 000 — 350 000):"},
    "edit_format": {"label": "🏢 Формат работы", "key": "work_format", "hint": "Выбери формат:", "options": ["офис", "удалённо", "гибрид"]},
    "edit_contacts": {"label": "📱 Контакты", "key": "contacts", "hint": "Укажи контакты в формате:\nТелефон: ...\nEmail: ...\nTelegram: ...\nLinkedIn: ..."},
    "edit_skills": {"label": "🛠 Навыки", "key": "skills", "hint": "Перечисли ключевые навыки через запятую (я пересоберу с уровнями через AI):"},
    "edit_experience": {"label": "💼 Опыт работы", "key": "experience", "hint": "Опиши опыт работы. Можно одним сообщением или несколькими.\nФормат: Компания, должность, период, чем занимался, достижения."},
}

QUESTIONS_TEXT = (
    "Теперь расскажи о себе текстом. Можешь ответить одним или несколькими сообщениями:\n\n"
    "1️⃣ Чем нравится заниматься на работе?\n"
    "2️⃣ Чем НЕ нравится заниматься?\n"
    "3️⃣ Хобби и интересы?\n"
    "4️⃣ Семья, возраст? (по желанию)\n"
    "5️⃣ Какую зарплату хочешь? (вилка)\n"
    "6️⃣ Формат работы: офис / удалёнка / гибрид?\n"
    "7️⃣ География: город, готовность к переезду?\n\n"
    "Когда закончишь — нажми кнопку ниже."
)


def _get_or_create_user(tg_user) -> User:
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    if not user:
        user = User(
            tg_id=tg_user.id,
            username=tg_user.username or "",
            full_name=tg_user.full_name or "",
            state=STATE_NEW,
        )
        session.add(user)
        session.commit()
    session.close()
    return user


def _update_state(tg_id: int, state: str):
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_id).first()
    if user:
        user.state = state
        session.commit()
    session.close()


def _get_user(tg_id: int) -> User | None:
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_id).first()
    session.close()
    return user


def _get_profile(tg_id: int) -> Profile | None:
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_id).first()
    if user and user.profile:
        profile = user.profile
        # Detach from session
        session.expunge(profile)
        session.close()
        return profile
    session.close()
    return None


# =============================================================================
# Handlers
# =============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not is_user_allowed(tg_user.id):
        await update.message.reply_text(
            "⛔ Бот доступен по приглашению. Обратитесь к администратору."
        )
        return

    user = _get_or_create_user(tg_user)

    # Check if already has a profile
    profile = _get_profile(tg_user.id)
    if profile and profile.profile_json:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Анализировать вакансию", callback_data="mode_vacancy")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="show_profile")],
            [InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_menu")],
        ])
        await update.message.reply_text(
            f"С возвращением, {tg_user.first_name}! 👋\n\n"
            "Твой профиль уже создан. Что хочешь сделать?",
            reply_markup=kb,
        )
        return

    _update_state(tg_user.id, STATE_UPLOADING)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Документы загружены", callback_data="docs_done")],
    ])
    await update.message.reply_text(
        f"Привет, {tg_user.first_name}! 🚀\n\n"
        "Я помогу с поиском работы: проанализирую вакансии, "
        "подготовлю адаптированное резюме.\n\n"
        "📎 **Для начала загрузи документы** (PDF, DOCX, TXT):\n"
        "— Резюме (любое, даже старое)\n"
        "— Рабочие тетради / кейсы\n"
        "— Должностные инструкции\n"
        "— Сертификаты\n\n"
        "Можешь загрузить несколько файлов. "
        "Когда закончишь — нажми кнопку.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = _get_profile(update.effective_user.id)
    if not profile or not profile.profile_json:
        await update.message.reply_text("Профиль ещё не создан. Используй /start")
        return

    data = json.loads(profile.profile_json)
    text = data.get("profile_summary_for_user", profile.summary or "Профиль создан.")
    await _send_long(update.message, f"👤 **Твой профиль:**\n\n{text}")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session()
    user = session.query(User).filter_by(tg_id=update.effective_user.id).first()
    if not user:
        await update.message.reply_text("Сначала /start")
        session.close()
        return

    analyses = (
        session.query(VacancyAnalysis)
        .filter_by(user_id=user.id)
        .order_by(VacancyAnalysis.created_at.desc())
        .limit(10)
        .all()
    )
    session.close()

    if not analyses:
        await update.message.reply_text("Пока нет анализов вакансий. Отправь ссылку hh.ru!")
        return

    lines = ["📋 **Последние анализы:**\n"]
    for i, a in enumerate(analyses, 1):
        match_emoji = "✅" if (a.match_percent or 0) >= 70 else "⚠️"
        lines.append(
            f"{i}. {match_emoji} {a.position or 'Вакансия'} — {a.company_name or ''} "
            f"({a.match_percent or 0:.0f}%)"
        )

    await _send_long(update.message, "\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **CareerBot — команды:**\n\n"
        "/start — начать / главное меню\n"
        "/profile — показать профиль\n"
        "/history — история анализов\n"
        "/update — обновить профиль\n"
        "/help — эта справка\n\n"
        "**Как пользоваться:**\n"
        "1. Загрузи документы и ответь на вопросы → создаётся профиль\n"
        "2. Отправь ссылку hh.ru или текст вакансии → анализ + рекомендации\n"
        "3. Нажми «Готовить резюме» → получишь PDF и DOCX",
        parse_mode="Markdown",
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = _get_profile(update.effective_user.id)
    if not profile or not profile.profile_json:
        await update.message.reply_text("Профиль ещё не создан. Используй /start")
        return

    await _show_edit_menu(update.message)


async def _show_edit_menu(message, prefix_text=""):
    """Show profile edit menu with field buttons."""
    buttons = []
    for field_id, field in EDIT_FIELDS.items():
        buttons.append([InlineKeyboardButton(field["label"], callback_data=field_id)])
    buttons.append([InlineKeyboardButton("📎 Добавить документы", callback_data="add_docs")])
    buttons.append([InlineKeyboardButton("🔄 Пересоздать с нуля", callback_data="restart_onboarding")])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])

    text = prefix_text or "✏️ **Редактирование профиля**\n\nВыбери что хочешь изменить:"
    kb = InlineKeyboardMarkup(buttons)
    try:
        await message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await message.reply_text(text, reply_markup=kb)


# =============================================================================
# Document handling
# =============================================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not is_user_allowed(tg_user.id):
        return

    user = _get_user(tg_user.id)
    if not user or user.state not in (STATE_UPLOADING, STATE_NEW):
        # If in ready state and they send a doc, accept it too
        if user and user.state == STATE_READY:
            pass  # Allow documents anytime when profile exists
        else:
            await update.message.reply_text(
                "Сейчас не время для документов. Используй /start или /update"
            )
            return

    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or "unknown"
    ext = Path(fname).suffix.lower()

    if ext not in (".pdf", ".docx", ".txt", ".doc"):
        await update.message.reply_text(f"⚠️ Формат {ext} не поддерживается. Жду PDF, DOCX или TXT.")
        return

    await update.message.reply_text(f"📥 Обрабатываю {fname}...")

    # Download file
    file = await doc.get_file()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        if ext == ".pdf":
            content = parse_pdf(tmp_path)
        elif ext in (".docx", ".doc"):
            content = parse_docx(tmp_path)
        else:  # .txt
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

        if not content.strip():
            await update.message.reply_text(f"⚠️ Не удалось извлечь текст из {fname}")
            return

        # Save to DB
        session = get_session()
        db_user = session.query(User).filter_by(tg_id=tg_user.id).first()
        doc_record = Document(
            user_id=db_user.id,
            doc_type=ext.lstrip("."),
            filename=fname,
            content=content[:50000],  # Limit
        )
        session.add(doc_record)
        session.commit()
        doc_count = session.query(Document).filter_by(user_id=db_user.id).count()
        session.close()

        await update.message.reply_text(
            f"✅ {fname} обработан ({len(content):,} символов)\n"
            f"📂 Всего документов: {doc_count}"
        )

    except Exception as e:
        logger.error("Document parsing failed: %s", e)
        await update.message.reply_text(f"❌ Ошибка обработки {fname}: {e}")
    finally:
        os.unlink(tmp_path)


# =============================================================================
# Callback queries
# =============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_user = query.from_user

    if data == "docs_done":
        await _on_docs_done(query, tg_user)
    elif data == "answers_done":
        await _on_answers_done(query, tg_user, context)
    elif data == "show_profile":
        profile = _get_profile(tg_user.id)
        if profile and profile.profile_json:
            pdata = json.loads(profile.profile_json)
            text = pdata.get("profile_summary_for_user", "Профиль создан.")
            await _send_long(query.message, f"👤 **Твой профиль:**\n\n{text}")
        else:
            await query.message.reply_text("Профиль не найден.")
    elif data == "restart_onboarding":
        _update_state(tg_user.id, STATE_UPLOADING)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Документы загружены", callback_data="docs_done")],
        ])
        await query.message.reply_text(
            "📎 Загружай документы. Когда закончишь — нажми кнопку.",
            reply_markup=kb,
        )
    elif data == "mode_vacancy":
        await query.message.reply_text(
            "📎 Отправь ссылку на вакансию hh.ru или текст вакансии."
        )
    elif data == "profile_ok":
        _update_state(tg_user.id, STATE_READY)
        await query.message.reply_text(
            "✅ Профиль сохранён!\n\n"
            "Теперь отправляй ссылки на вакансии hh.ru — я проанализирую "
            "и подготовлю адаптированное резюме."
        )
    elif data == "profile_redo":
        _update_state(tg_user.id, STATE_UPLOADING)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Документы загружены", callback_data="docs_done")],
        ])
        await query.message.reply_text("Хорошо, начнём сначала. Загружай документы.", reply_markup=kb)
    elif data.startswith("gen_resume_"):
        analysis_id = int(data.split("_")[-1])
        await _generate_and_send_resume(query.message, tg_user, analysis_id)
    elif data == "skip_resume":
        await query.message.reply_text("👌 Окей. Отправь ссылку на другую вакансию когда будет нужно.")
    elif data == "add_docs":
        _update_state(tg_user.id, STATE_UPLOADING)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Документы загружены", callback_data="docs_added")],
        ])
        await query.message.reply_text(
            "📎 Загружай новые документы. Они дополнят существующий профиль.\n"
            "Когда закончишь — нажми кнопку.",
            reply_markup=kb,
        )
    elif data == "docs_added":
        _update_state(tg_user.id, STATE_READY)
        await _show_edit_menu(query.message, "✅ Документы добавлены!\n\nЧто ещё изменить?")
    elif data == "back_to_main":
        _update_state(tg_user.id, STATE_READY)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Анализировать вакансию", callback_data="mode_vacancy")],
            [InlineKeyboardButton("👤 Мой профиль", callback_data="show_profile")],
            [InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_menu")],
        ])
        await query.message.reply_text("Главное меню:", reply_markup=kb)
    elif data == "edit_menu":
        await _show_edit_menu(query.message)
    elif data in EDIT_FIELDS:
        field = EDIT_FIELDS[data]
        context.user_data["editing_field"] = data

        if "options" in field:
            # Show options as buttons
            buttons = [
                [InlineKeyboardButton(opt, callback_data=f"setval_{data}_{opt}")]
                for opt in field["options"]
            ]
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="edit_menu")])
            await query.message.reply_text(
                field["hint"], reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            _update_state(tg_user.id, STATE_EDITING)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data="cancel_edit")],
            ])
            # Show current value
            profile = _get_profile(tg_user.id)
            current = ""
            if profile and profile.profile_json:
                pdata = json.loads(profile.profile_json)
                val = pdata.get(field["key"], "")
                if isinstance(val, list):
                    current = ", ".join(str(v) for v in val)
                elif isinstance(val, dict):
                    current = "\n".join(f"{k}: {v}" for k, v in val.items() if v)
                else:
                    current = str(val)
            hint = field["hint"]
            if current:
                hint += f"\n\n📋 Текущее значение:\n{current}"
            await query.message.reply_text(hint, reply_markup=kb)
    elif data.startswith("setval_"):
        parts = data.split("_", 2)
        field_id = parts[1] + "_" + parts[1].split("edit_")[-1]  # reconstruct
        # Actually parse properly
        _, field_id_part1, value = data.split("_", 2)
        field_id = f"edit_{field_id_part1}"
        if field_id not in EDIT_FIELDS:
            # Try finding the right field
            for fid in EDIT_FIELDS:
                if data.startswith(f"setval_{fid}_"):
                    field_id = fid
                    value = data[len(f"setval_{fid}_"):]
                    break
        await _apply_edit(query.message, tg_user, field_id, value)
    elif data == "cancel_edit":
        _update_state(tg_user.id, STATE_READY)
        context.user_data.pop("editing_field", None)
        await _show_edit_menu(query.message, "Отменено.")


async def _apply_edit(message, tg_user, field_id: str, value: str):
    """Apply a profile field edit."""
    if field_id not in EDIT_FIELDS:
        await message.reply_text("❌ Неизвестное поле.")
        return

    field = EDIT_FIELDS[field_id]
    key = field["key"]

    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    profile = session.query(Profile).filter_by(user_id=user.id).first()

    if not profile or not profile.profile_json:
        session.close()
        await message.reply_text("Профиль не найден.")
        return

    pdata = json.loads(profile.profile_json)

    # Parse value based on field type
    if key == "target_positions":
        pdata[key] = [p.strip() for p in value.split(",") if p.strip()]
    elif key == "contacts":
        # Parse "Phone: ...\nEmail: ..." format
        contacts = pdata.get("contacts", {})
        for line in value.split("\n"):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                v = v.strip()
                if "телеф" in k or "phone" in k:
                    contacts["phone"] = v
                elif "email" in k or "почт" in k:
                    contacts["email"] = v
                elif "telegram" in k or "tg" in k:
                    contacts["telegram"] = v
                elif "linkedin" in k:
                    contacts["linkedin"] = v
        pdata[key] = contacts
    elif key == "skills":
        # Rebuild skills with AI
        from core.ai_engine import call_claude
        import asyncio
        try:
            resp = await call_claude(
                "Ты карьерный аналитик. Пользователь обновил список навыков. "
                "Верни JSON: {\"hard\": [{\"name\": \"...\", \"level\": \"...\"}], \"soft\": [{\"name\": \"...\", \"level\": \"...\"}]}. "
                "Определи уровень каждого навыка. Только JSON.",
                f"Навыки: {value}",
                max_tokens=2000,
            )
            import re as _re
            m = _re.search(r'(\{[\s\S]*\})', resp)
            if m:
                pdata[key] = json.loads(m.group(1))
        except Exception as e:
            logger.error("Skills AI parse failed: %s", e)
            pdata[key] = {"hard": [{"name": s.strip(), "level": "—"} for s in value.split(",")], "soft": []}
    elif key == "experience":
        # Rebuild experience with AI
        from core.ai_engine import call_claude
        try:
            resp = await call_claude(
                "Ты карьерный аналитик. Пользователь обновил опыт работы. "
                "Верни JSON массив: [{\"company\": \"\", \"position\": \"\", \"period\": \"\", "
                "\"description\": \"\", \"achievements\": [\"...\"]}]. Только JSON массив.",
                f"Опыт: {value}",
                max_tokens=3000,
            )
            import re as _re
            m = _re.search(r'(\[[\s\S]*\])', resp)
            if m:
                pdata[key] = json.loads(m.group(1))
        except Exception as e:
            logger.error("Experience AI parse failed: %s", e)
    else:
        # Simple string fields: summary, salary_range, work_format
        pdata[key] = value

    # Regenerate user-facing summary
    pdata["profile_summary_for_user"] = _build_summary_text(pdata)

    profile.profile_json = json.dumps(pdata, ensure_ascii=False)
    profile.summary = pdata.get("summary", "")
    profile.skills = json.dumps(pdata.get("skills", {}), ensure_ascii=False)
    profile.target_positions = json.dumps(pdata.get("target_positions", []), ensure_ascii=False)
    profile.contacts = json.dumps(pdata.get("contacts", {}), ensure_ascii=False)
    profile.salary_range = pdata.get("salary_range", "")
    if key == "work_format":
        profile.work_format = value

    user.state = STATE_READY
    session.commit()
    session.close()

    await _show_edit_menu(message, f"✅ {field['label']} обновлено!\n\nЧто ещё изменить?")


def _build_summary_text(pdata: dict) -> str:
    """Build a readable profile summary from profile data."""
    parts = []
    if pdata.get("full_name"):
        parts.append(f"**{pdata['full_name']}**")
    if pdata.get("summary"):
        parts.append(pdata["summary"])
    if pdata.get("target_positions"):
        positions = pdata["target_positions"]
        if isinstance(positions, list):
            parts.append(f"🎯 Целевые позиции: {', '.join(positions)}")
    if pdata.get("salary_range"):
        parts.append(f"💰 Зарплата: {pdata['salary_range']}")
    if pdata.get("work_format"):
        parts.append(f"🏢 Формат: {pdata['work_format']}")
    if pdata.get("skills"):
        skills = pdata["skills"]
        if isinstance(skills, dict):
            hard = skills.get("hard", [])
            if hard:
                names = [s["name"] if isinstance(s, dict) else str(s) for s in hard[:10]]
                parts.append(f"🛠 Навыки: {', '.join(names)}")
    if pdata.get("experience"):
        exp = pdata["experience"]
        if isinstance(exp, list) and exp:
            parts.append(f"💼 Опыт: {len(exp)} мест работы")
            for e in exp[:3]:
                if isinstance(e, dict):
                    parts.append(f"  • {e.get('position', '')} @ {e.get('company', '')} ({e.get('period', '')})")
    return "\n".join(parts)


async def _on_docs_done(query, tg_user):
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    doc_count = session.query(Document).filter_by(user_id=user.id).count() if user else 0
    session.close()

    if doc_count == 0:
        await query.message.reply_text(
            "⚠️ Ты ещё не загрузил ни одного документа. "
            "Отправь PDF или DOCX файл."
        )
        return

    _update_state(tg_user.id, STATE_ANSWERING)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово — создать профиль", callback_data="answers_done")],
    ])
    await query.message.reply_text(QUESTIONS_TEXT, reply_markup=kb)


async def _on_answers_done(query, tg_user, context):
    await query.message.reply_text("⏳ Создаю цифровой профиль... Это займёт 30-60 секунд.")

    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()

    # Collect documents
    docs = session.query(Document).filter_by(user_id=user.id).all()
    docs_text = "\n\n---\n\n".join(
        f"[{d.filename}]\n{d.content}" for d in docs
    )

    # Collect answers from context
    answers = context.user_data.get("answers", [])
    answers_text = "\n".join(answers) if answers else "Не предоставлены"

    try:
        profile_data = await build_profile(docs_text, answers_text)

        # Save profile
        existing = session.query(Profile).filter_by(user_id=user.id).first()
        if existing:
            existing.profile_json = json.dumps(profile_data, ensure_ascii=False)
            existing.summary = profile_data.get("summary", "")
            existing.skills = json.dumps(profile_data.get("skills", {}), ensure_ascii=False)
            existing.target_positions = json.dumps(
                profile_data.get("target_positions", []), ensure_ascii=False
            )
            existing.contacts = json.dumps(profile_data.get("contacts", {}), ensure_ascii=False)
            existing.salary_range = profile_data.get("salary_range", "")
        else:
            profile = Profile(
                user_id=user.id,
                profile_json=json.dumps(profile_data, ensure_ascii=False),
                summary=profile_data.get("summary", ""),
                skills=json.dumps(profile_data.get("skills", {}), ensure_ascii=False),
                target_positions=json.dumps(
                    profile_data.get("target_positions", []), ensure_ascii=False
                ),
                contacts=json.dumps(profile_data.get("contacts", {}), ensure_ascii=False),
                salary_range=profile_data.get("salary_range", ""),
            )
            session.add(profile)

        user.state = STATE_READY
        session.commit()

        summary_text = profile_data.get("profile_summary_for_user", "Профиль создан.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👍 Всё верно", callback_data="profile_ok")],
            [InlineKeyboardButton("🔄 Пересоздать", callback_data="profile_redo")],
        ])

        await _send_long(
            query.message,
            f"📊 **Твой цифровой профиль:**\n\n{summary_text}",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error("Profile build failed: %s", e)
        await query.message.reply_text(f"❌ Ошибка создания профиля: {e}\nПопробуй /start заново.")
    finally:
        session.close()


# =============================================================================
# Text messages — answers or vacancies
# =============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if not is_user_allowed(tg_user.id):
        return

    text = update.message.text.strip()
    if not text:
        return

    user = _get_user(tg_user.id)
    if not user:
        await update.message.reply_text("Используй /start для начала.")
        return

    # If editing profile field — apply edit
    if user.state == STATE_EDITING:
        field_id = context.user_data.get("editing_field")
        if field_id:
            context.user_data.pop("editing_field", None)
            await _apply_edit(update.message, tg_user, field_id, text)
        else:
            _update_state(tg_user.id, STATE_READY)
            await update.message.reply_text("Не понял, что редактируем. Используй /update")
        return

    # If answering questions — save answers
    if user.state == STATE_ANSWERING:
        answers = context.user_data.get("answers", [])
        answers.append(text)
        context.user_data["answers"] = answers
        await update.message.reply_text(
            f"✏️ Записано. Продолжай или нажми «Готово»."
        )
        return

    # If uploading — remind
    if user.state == STATE_UPLOADING:
        await update.message.reply_text(
            "Сейчас жду документы (PDF/DOCX). "
            "Загрузи файлы или нажми «Документы загружены»."
        )
        return

    # If ready — check for vacancy URL or text
    if user.state == STATE_READY:
        await _handle_vacancy_input(update, context, user, text)
        return

    await update.message.reply_text("Используй /start или /help")


async def _handle_vacancy_input(update, context, user, text):
    profile = _get_profile(user.tg_id)
    if not profile or not profile.profile_json:
        await update.message.reply_text("Профиль не найден. Используй /start")
        return

    # Check if it's an HH URL
    is_url = bool(extract_hh_vacancy_id(text))

    await update.message.reply_text("🔍 Анализирую вакансию... ⏳")

    try:
        if is_url:
            analysis = await process_vacancy_url(text, profile.profile_json)
        else:
            analysis = await process_vacancy_text(text, profile.profile_json)

        # Save to DB
        session = get_session()
        db_user = session.query(User).filter_by(tg_id=user.tg_id).first()
        vacancy = analysis.get("vacancy", {})

        va = VacancyAnalysis(
            user_id=db_user.id,
            url=vacancy.get("url", text[:500]),
            company_name=vacancy.get("company", ""),
            position=vacancy.get("name", ""),
            salary=vacancy.get("salary", ""),
            analysis_json=json.dumps(analysis, ensure_ascii=False),
            match_percent=analysis.get("match_percent", 0),
        )
        session.add(va)
        session.commit()
        va_id = va.id
        session.close()

        # Send analysis
        analysis_text = analysis.get("analysis_text", "Анализ завершён.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Готовить резюме", callback_data=f"gen_resume_{va_id}")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_resume")],
        ])

        await _send_long(update.message, analysis_text, reply_markup=kb)

    except Exception as e:
        logger.error("Vacancy analysis failed: %s", e)
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


# =============================================================================
# Resume generation
# =============================================================================

async def _generate_and_send_resume(message, tg_user, analysis_id: int):
    await message.reply_text("⏳ Генерирую адаптированное резюме... 30-60 сек.")

    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    profile = session.query(Profile).filter_by(user_id=user.id).first()
    va = session.query(VacancyAnalysis).filter_by(id=analysis_id).first()

    if not profile or not va:
        await message.reply_text("❌ Профиль или анализ не найден.")
        session.close()
        return

    try:
        resume_data = await generate_resume_data(profile.profile_json, va.analysis_json)

        # Generate safe filename
        safe_name = re.sub(r'[^\w\-]', '_', f"{user.full_name}_{va.position or 'resume'}")[:60]

        # PDF
        pdf_path = generate_pdf(resume_data, safe_name)
        pdf_record = GeneratedResume(
            user_id=user.id,
            vacancy_analysis_id=va.id,
            format="pdf",
            file_path=str(pdf_path),
        )
        session.add(pdf_record)

        # DOCX
        docx_path = generate_docx(resume_data, safe_name + "_HH")
        docx_record = GeneratedResume(
            user_id=user.id,
            vacancy_analysis_id=va.id,
            format="docx",
            file_path=str(docx_path),
        )
        session.add(docx_record)
        session.commit()

        # Send files
        await message.reply_document(
            document=open(pdf_path, "rb"),
            filename=f"{safe_name}.pdf",
            caption="📄 1-страничное резюме (PDF) — для прямой отправки",
        )
        await message.reply_document(
            document=open(docx_path, "rb"),
            filename=f"{safe_name}_HH.docx",
            caption="📋 Резюме формат HH (DOCX) — для загрузки на hh.ru",
        )

        await message.reply_text(
            "✅ Готово! Два файла:\n"
            "• PDF — красивый, для email/ЛС\n"
            "• DOCX — для загрузки на hh.ru\n\n"
            "Отправь ссылку на другую вакансию для нового анализа."
        )

    except Exception as e:
        logger.error("Resume generation failed: %s", e)
        await message.reply_text(f"❌ Ошибка генерации резюме: {e}")
    finally:
        session.close()


# =============================================================================
# Helpers
# =============================================================================

async def _send_long(message, text, reply_markup=None, parse_mode="Markdown"):
    """Send long text split into chunks."""
    MAX = 4000
    if len(text) <= MAX:
        try:
            await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception:
            await message.reply_text(text, reply_markup=reply_markup)
        return

    chunks = []
    while text:
        if len(text) <= MAX:
            chunks.append(text)
            break
        cut = text[:MAX].rfind("\n")
        if cut < MAX // 2:
            cut = MAX
        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    for i, chunk in enumerate(chunks):
        rm = reply_markup if i == len(chunks) - 1 else None
        try:
            await message.reply_text(chunk, parse_mode=parse_mode, reply_markup=rm)
        except Exception:
            await message.reply_text(chunk, reply_markup=rm)


# =============================================================================
# Main
# =============================================================================

def main():
    init_db()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("update", cmd_update))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Documents
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("CareerBot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
