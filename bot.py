#!/usr/bin/env python3
"""CareerBot — AI-powered Telegram career assistant."""

import json
import logging
import os
import re
import subprocess
import tempfile
import traceback
from pathlib import Path

import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, LabeledPrice
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters, ContextTypes,
)

from config import (
    BOT_TOKEN, OUTPUT_DIR, ADMIN_TG_ID,
    TRIAL_ANALYSES, TRIAL_RESUMES, get_user_role, is_unlimited,
    PAYMASTER_TOKEN, PAYMASTER_API_TOKEN, PAYMASTER_MERCHANT_ID, PACKAGES,
)
from db.database import init_db, get_session
from db.models import User, Profile, Document, VacancyAnalysis, GeneratedResume, Payment
from core.profile_builder import build_profile
from core.vacancy_analyzer import process_vacancy_url, process_vacancy_text
from core.resume_generator import generate_resume_data, generate_pdf, generate_docx
from parsers.pdf_parser import parse_pdf
from parsers.docx_parser import parse_docx
from parsers.universal_parser import parse_document, ALL_SUPPORTED, LIBREOFFICE_FORMATS
from parsers.vacancy_parser import extract_hh_vacancy_id

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# Suppress httpx polling spam
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- States ---
STATE_NEW = "new"
STATE_UPLOADING = "uploading"
STATE_ANSWERING = "answering"
STATE_READY = "ready"
STATE_EDITING = "editing"
STATE_RESUME_EDITING = "resume_editing"

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


def _get_or_create_user(tg_user) -> tuple[User, bool]:
    """Return (user, is_new)."""
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    is_new = False
    if not user:
        role = get_user_role(tg_user.id)
        user = User(
            tg_id=tg_user.id,
            username=tg_user.username or "",
            full_name=tg_user.full_name or "",
            state=STATE_NEW,
            role=role,
            analyses_left=-1 if is_unlimited(role) else TRIAL_ANALYSES,
            resumes_left=-1 if is_unlimited(role) else TRIAL_RESUMES,
        )
        session.add(user)
        session.commit()
        is_new = True
    session.expunge(user)
    session.close()
    return user, is_new


async def _notify_admin(bot, text: str):
    """Send notification to admin."""
    try:
        await bot.send_message(chat_id=ADMIN_TG_ID, text=text)
    except Exception as e:
        logger.warning("Admin notify failed: %s", e)


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
    if user:
        session.expunge(user)
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
    user, is_new = _get_or_create_user(tg_user)

    if is_new:
        await _notify_admin(
            context.bot,
            f"🆕 Новый пользователь CareerBot:\n"
            f"👤 {tg_user.full_name} (@{tg_user.username or '—'})\n"
            f"🆔 {tg_user.id}\n"
            f"📊 Роль: {user.role}",
        )

    # Check if already has a profile
    profile = _get_profile(tg_user.id)
    if profile and profile.profile_json:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Аналитика вакансии", callback_data="mode_vacancy")],
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
        "Я помогу с поиском работы: сделаю аналитику вакансий, "
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
        await update.message.reply_text("Пока нет аналитики вакансий. Отправь ссылку hh.ru!")
        return

    lines = ["📋 **Последняя аналитика:**\n"]
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
        "/history — история аналитики\n"
        "/update — обновить профиль\n"
        "/help — эта справка\n\n"
        "**Как пользоваться:**\n"
        "1. Загрузи документы и ответь на вопросы → создаётся профиль\n"
        "2. Отправь ссылку hh.ru или текст вакансии → аналитика + рекомендации\n"
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

    if ext not in ALL_SUPPORTED:
        supported = ", ".join(sorted(ALL_SUPPORTED))
        await update.message.reply_text(
            f"⚠️ Формат {ext} не поддерживается.\n"
            f"Поддерживаемые: {supported}"
        )
        return

    if ext in LIBREOFFICE_FORMATS:
        fmt_names = {".doc": "Word 97-2003", ".odt": "OpenDocument", ".rtf": "Rich Text"}
        await update.message.reply_text(
            f"📥 Обрабатываю {fname}...\n"
            f"⚠️ Формат {fmt_names.get(ext, ext)} — конвертирую, это может занять несколько секунд."
        )
    else:
        await update.message.reply_text(f"📥 Обрабатываю {fname}...")

    # Download file
    file = await doc.get_file()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        try:
            content = parse_document(tmp_path)
        except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired, ValueError) as conv_err:
            logger.error("Document parsing failed for %s: %s", fname, conv_err)
            await update.message.reply_text(
                f"⚠️ Не удалось обработать {fname}.\n\n"
                "**Что делать:**\n"
                "1. Открой файл в Word / Google Docs\n"
                "2. Сохрани как .docx или .pdf\n"
                "3. Загрузи сюда\n",
                parse_mode="Markdown",
            )
            return

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
            "Теперь отправляй ссылки на вакансии hh.ru — я сделаю аналитику "
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
        await _generate_and_send_resume(query.message, tg_user, analysis_id, context)
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
            [InlineKeyboardButton("📄 Аналитика вакансии", callback_data="mode_vacancy")],
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
    elif data == "edit_resume_text":
        resume_data = context.user_data.get("last_resume_data")
        if not resume_data:
            await query.message.reply_text("❌ Нет данных резюме для редактирования. Сгенерируй резюме заново.")
            return
        # Build editable text representation
        text = _resume_data_to_text(resume_data)
        _update_state(tg_user.id, STATE_RESUME_EDITING)
        await query.message.reply_text(
            "✏️ **Редактирование резюме**\n\n"
            "Ниже — текст резюме. Скопируй, отредактируй нужные блоки и отправь обратно.\n"
            "Сохраняй структуру заголовков (`## СЕКЦИЯ`).\n\n"
            "Для отмены нажми кнопку:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data="cancel_resume_edit")],
            ]),
        )
        # Send resume text as code block (easy to copy)
        # Split if too long for one message
        chunks = _split_code_block(text, max_len=3900)
        for chunk in chunks:
            await query.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")
    elif data == "cancel_resume_edit":
        _update_state(tg_user.id, STATE_READY)
        await query.message.reply_text(
            "Отменено. Отправь ссылку на вакансию или используй меню.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 Аналитика вакансии", callback_data="mode_vacancy")],
                [InlineKeyboardButton("✏️ Редактировать профиль", callback_data="edit_menu")],
            ]),
        )
    elif data == "cancel_edit":
        _update_state(tg_user.id, STATE_READY)
        context.user_data.pop("editing_field", None)
        await _show_edit_menu(query.message, "Отменено.")
    elif data == "buy_menu":
        await _show_payment_options(query.message, "buy")
    elif data.startswith("buy_stars_"):
        pack_id = data.replace("buy_stars_", "")
        await _send_stars_invoice(query.message, tg_user, pack_id)
    elif data == "buy_card_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 199 ₽ — Старт (5+5)", callback_data="buy_card_pack_5")],
            [InlineKeyboardButton("💳 499 ₽ — Стандарт (20+15)", callback_data="buy_card_pack_20")],
            [InlineKeyboardButton("💳 999 ₽ — Про (50+30)", callback_data="buy_card_pack_50")],
            [InlineKeyboardButton("💳 1 999 ₽ — Макс (100+100)", callback_data="buy_card_pack_100")],
        ])
        await query.message.reply_text("Выбери пакет для оплаты картой:", reply_markup=kb)
    elif data.startswith("buy_card_"):
        pack_id = data.replace("buy_card_", "")
        await _send_paymaster_invoice(query.message, tg_user, pack_id)
    elif data == "buy_sber":
        await query.message.reply_text(
            "💰 **Перевод на Сбер:**\n\n"
            "Номер: `+79035117700`\n"
            "Получатель: Александр М.\n\n"
            "**Суммы:**\n"
            "• Старт (5+5) — 199 ₽\n"
            "• Стандарт (20+15) — 499 ₽\n"
            "• Про (50+30) — 999 ₽\n"
            "• Макс (100+100) — 1 999 ₽\n\n"
            "После перевода напиши @Amoskv с чеком — активирую в течение часа.",
            parse_mode="Markdown",
        )


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


def _resume_data_to_text(rd: dict) -> str:
    """Convert resume_data dict to editable plain text."""
    lines = []
    lines.append(f"## ИМЯ\n{rd.get('full_name', '')}")
    lines.append(f"\n## ПОЗИЦИЯ\n{rd.get('target_position', '')}")

    contacts = rd.get("contacts", {})
    c_parts = []
    for k in ("phone", "email", "telegram", "linkedin"):
        if contacts.get(k):
            c_parts.append(f"{k}: {contacts[k]}")
    if c_parts:
        lines.append(f"\n## КОНТАКТЫ\n" + "\n".join(c_parts))

    lines.append(f"\n## О СЕБЕ\n{rd.get('summary', '')}")

    exp = rd.get("experience", [])
    if exp:
        lines.append("\n## ОПЫТ РАБОТЫ")
        for job in exp:
            lines.append(f"\n### {job.get('company', '')} | {job.get('period', '')}")
            lines.append(f"{job.get('position', '')}")
            for ach in job.get("achievements", []):
                lines.append(f"- {ach}")

    edu = rd.get("education", [])
    if edu:
        lines.append("\n## ОБРАЗОВАНИЕ")
        for e in edu:
            lines.append(f"- {e.get('institution', '')} — {e.get('degree', '')} {e.get('field', '')}, {e.get('year', '')}")

    skills = rd.get("skills", [])
    if skills:
        lines.append(f"\n## НАВЫКИ\n{', '.join(skills)}")

    certs = rd.get("certifications", [])
    if certs:
        lines.append(f"\n## СЕРТИФИКАТЫ\n{', '.join(certs)}")

    langs = rd.get("languages", [])
    if langs:
        lines.append(f"\n## ЯЗЫКИ\n{', '.join(langs)}")

    return "\n".join(lines)


def _text_to_resume_data(text: str) -> dict:
    """Parse edited plain text back to resume_data dict."""
    rd = {
        "full_name": "", "target_position": "", "contacts": {},
        "summary": "", "experience": [], "education": [],
        "skills": [], "certifications": [], "languages": [],
    }
    current_section = None
    current_job = None
    buffer = []

    def flush_buffer():
        return "\n".join(buffer).strip()

    for raw_line in text.split("\n"):
        line = raw_line.strip()

        # Section headers
        if line.startswith("## ИМЯ"):
            current_section = "name"; buffer = []; continue
        elif line.startswith("## ПОЗИЦИЯ"):
            if current_section == "name": rd["full_name"] = flush_buffer()
            current_section = "position"; buffer = []; continue
        elif line.startswith("## КОНТАКТЫ"):
            if current_section == "position": rd["target_position"] = flush_buffer()
            current_section = "contacts"; buffer = []; continue
        elif line.startswith("## О СЕБЕ"):
            if current_section == "position": rd["target_position"] = flush_buffer()
            if current_section == "contacts": _parse_contacts(rd, flush_buffer())
            current_section = "summary"; buffer = []; continue
        elif line.startswith("## ОПЫТ РАБОТЫ"):
            if current_section == "summary": rd["summary"] = flush_buffer()
            current_section = "experience"; buffer = []; current_job = None; continue
        elif line.startswith("## ОБРАЗОВАНИЕ"):
            if current_section == "experience" and current_job:
                rd["experience"].append(current_job)
            if current_section == "summary": rd["summary"] = flush_buffer()
            current_section = "education"; buffer = []; current_job = None; continue
        elif line.startswith("## НАВЫКИ"):
            if current_section == "experience" and current_job:
                rd["experience"].append(current_job)
            if current_section == "education": _parse_education(rd, flush_buffer())
            current_section = "skills"; buffer = []; continue
        elif line.startswith("## СЕРТИФИКАТЫ"):
            if current_section == "skills": rd["skills"] = [s.strip() for s in flush_buffer().split(",") if s.strip()]
            current_section = "certs"; buffer = []; continue
        elif line.startswith("## ЯЗЫКИ"):
            if current_section == "certs": rd["certifications"] = [s.strip() for s in flush_buffer().split(",") if s.strip()]
            if current_section == "skills": rd["skills"] = [s.strip() for s in flush_buffer().split(",") if s.strip()]
            current_section = "langs"; buffer = []; continue

        # Sub-section: experience job
        if current_section == "experience" and line.startswith("### "):
            if current_job:
                rd["experience"].append(current_job)
            # Parse "### Company | Period"
            header = line[4:]
            parts = header.split("|", 1)
            current_job = {
                "company": parts[0].strip(),
                "period": parts[1].strip() if len(parts) > 1 else "",
                "position": "",
                "achievements": [],
            }
            continue

        if current_section == "experience" and current_job:
            if line.startswith("- "):
                current_job["achievements"].append(line[2:])
            elif line and not current_job["position"]:
                current_job["position"] = line
            elif line:
                current_job["achievements"].append(line)
            continue

        buffer.append(raw_line)

    # Flush last section
    last = flush_buffer()
    if current_section == "name": rd["full_name"] = last
    elif current_section == "position": rd["target_position"] = last
    elif current_section == "contacts": _parse_contacts(rd, last)
    elif current_section == "summary": rd["summary"] = last
    elif current_section == "experience" and current_job: rd["experience"].append(current_job)
    elif current_section == "education": _parse_education(rd, last)
    elif current_section == "skills": rd["skills"] = [s.strip() for s in last.split(",") if s.strip()]
    elif current_section == "certs": rd["certifications"] = [s.strip() for s in last.split(",") if s.strip()]
    elif current_section == "langs": rd["languages"] = [s.strip() for s in last.split(",") if s.strip()]

    return rd


def _parse_contacts(rd: dict, text: str):
    contacts = {}
    for line in text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            contacts[k] = v.strip()
    rd["contacts"] = contacts


def _parse_education(rd: dict, text: str):
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            line = line[2:]
        if not line:
            continue
        # "Institution — Degree Field, Year"
        parts = line.split("—", 1)
        inst = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        year = ""
        if "," in rest:
            rest_parts = rest.rsplit(",", 1)
            rest = rest_parts[0].strip()
            year = rest_parts[1].strip()
        degree = ""
        field = rest
        for d in ("Бакалавр", "Магистр", "Специалист", "Кандидат", "Доктор", "MBA", "PhD", "Bachelor", "Master"):
            if d.lower() in rest.lower():
                idx = rest.lower().index(d.lower())
                degree = rest[idx:idx+len(d)]
                field = (rest[:idx] + rest[idx+len(d):]).strip()
                break
        rd["education"].append({"institution": inst, "degree": degree, "field": field, "year": year})


def _split_code_block(text: str, max_len: int = 3900) -> list[str]:
    """Split text into chunks that fit in Telegram code blocks."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text[:max_len].rfind("\n")
        if cut < max_len // 3:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def _apply_resume_text_edit(message, tg_user, text: str, context):
    """Parse user-edited resume text, regenerate files, update profile."""
    from core.resume_generator import generate_pdf, generate_docx

    # Parse the edited text back to resume_data
    resume_data = _text_to_resume_data(text)

    # Validate minimally
    if not resume_data.get("full_name") and not resume_data.get("summary"):
        await message.reply_text(
            "❌ Не удалось распознать структуру. Сохраняй заголовки `## СЕКЦИЯ`.\n"
            "Попробуй ещё раз или нажми «Отмена».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data="cancel_resume_edit")],
            ]),
        )
        return

    await message.reply_text("⏳ Пересобираю файлы с правками...")

    try:
        safe_name = context.user_data.get("last_resume_safe_name", "resume_edited")
        analysis_id = context.user_data.get("last_resume_analysis_id")

        # Regenerate PDF and DOCX
        pdf_path = generate_pdf(resume_data, safe_name + "_v2")
        docx_path = generate_docx(resume_data, safe_name + "_HH_v2")

        # Save to DB
        session = get_session()
        user = session.query(User).filter_by(tg_id=tg_user.id).first()
        if user and analysis_id:
            pdf_record = GeneratedResume(
                user_id=user.id,
                vacancy_analysis_id=analysis_id,
                format="pdf",
                file_path=str(pdf_path),
            )
            docx_record = GeneratedResume(
                user_id=user.id,
                vacancy_analysis_id=analysis_id,
                format="docx",
                file_path=str(docx_path),
            )
            session.add(pdf_record)
            session.add(docx_record)
            session.commit()

        # Update profile with user's edits (learn preferences)
        if user:
            profile = session.query(Profile).filter_by(user_id=user.id).first()
            if profile and profile.profile_json:
                pdata = json.loads(profile.profile_json)
                # Merge user edits into profile preferences
                if resume_data.get("summary"):
                    pdata["preferred_summary"] = resume_data["summary"]
                if resume_data.get("target_position"):
                    # Add to preferred positions if not already there
                    tp = pdata.get("target_positions", [])
                    if isinstance(tp, list) and resume_data["target_position"] not in tp:
                        tp.append(resume_data["target_position"])
                        pdata["target_positions"] = tp
                if resume_data.get("contacts"):
                    existing = pdata.get("contacts", {})
                    existing.update({k: v for k, v in resume_data["contacts"].items() if v})
                    pdata["contacts"] = existing
                # Save resume edit preferences
                edits_log = pdata.get("resume_edit_preferences", [])
                edits_log.append({
                    "position": resume_data.get("target_position", ""),
                    "summary_used": resume_data.get("summary", ""),
                    "skills_used": resume_data.get("skills", []),
                })
                # Keep last 10 edits
                pdata["resume_edit_preferences"] = edits_log[-10:]
                pdata["profile_summary_for_user"] = _build_summary_text(pdata)
                profile.profile_json = json.dumps(pdata, ensure_ascii=False)
                session.commit()

        session.close()

        # Send files
        await message.reply_document(
            document=open(pdf_path, "rb"),
            filename=f"{safe_name}_v2.pdf",
            caption="📄 Обновлённое резюме (PDF)",
        )
        await message.reply_document(
            document=open(docx_path, "rb"),
            filename=f"{safe_name}_HH_v2.docx",
            caption="📋 Обновлённое резюме (DOCX)",
        )

        # Update context with new resume data for further edits
        context.user_data["last_resume_data"] = resume_data

        _update_state(tg_user.id, STATE_READY)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Ещё правки", callback_data="edit_resume_text")],
            [InlineKeyboardButton("📄 Новая вакансия", callback_data="mode_vacancy")],
        ])
        await message.reply_text(
            "✅ Резюме обновлено! Правки сохранены в профиль — "
            "в следующий раз учту твои предпочтения.",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error("Resume text edit failed: %s", e)
        _update_state(tg_user.id, STATE_READY)
        await message.reply_text(f"❌ Ошибка пересборки: {e}")


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

    # If editing resume text — parse and regenerate
    if user.state == STATE_RESUME_EDITING:
        await _apply_resume_text_edit(update.message, tg_user, text, context)
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

    # Check analysis limit for trial users
    if not is_unlimited(user.role) and user.analyses_left is not None and user.analyses_left <= 0:
        await _show_payment_options(update.message, "analyses")
        return

    # Check if it's an HH URL
    is_url = bool(extract_hh_vacancy_id(text))

    await update.message.reply_text("🔍 Делаю аналитику вакансии... ⏳")

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
        # Decrement analysis counter for trial users
        if not is_unlimited(db_user.role) and db_user.analyses_left is not None and db_user.analyses_left > 0:
            db_user.analyses_left -= 1
        session.commit()
        va_id = va.id
        analyses_left = db_user.analyses_left
        user_role = db_user.role
        session.close()

        # Send analysis
        analysis_text = analysis.get("analysis_text", "Аналитика готова.")

        # Show balance for trial users
        if not is_unlimited(user_role) and analyses_left is not None:
            analysis_text += f"\n\n📊 Осталось аналитик: {analyses_left}/{TRIAL_ANALYSES}"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Готовить резюме", callback_data=f"gen_resume_{va_id}")],
            [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_resume")],
        ])

        await _send_long(update.message, analysis_text, reply_markup=kb)

    except Exception as e:
        logger.error("Vacancy analysis failed: %s", e)
        await update.message.reply_text(f"❌ Ошибка аналитики: {e}")


# =============================================================================
# Resume generation
# =============================================================================

async def _generate_and_send_resume(message, tg_user, analysis_id: int, context=None):
    # Check resume limit for trial users
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()

    if not is_unlimited(user.role) and user.resumes_left is not None and user.resumes_left <= 0:
        session.close()
        await _show_payment_options(message, "resumes")
        return

    await message.reply_text("⏳ Генерирую адаптированное резюме... 30-60 сек.")

    profile = session.query(Profile).filter_by(user_id=user.id).first()
    va = session.query(VacancyAnalysis).filter_by(id=analysis_id).first()

    if not profile or not va:
        await message.reply_text("❌ Профиль или аналитика не найдены.")
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

        # Decrement resume counter for trial users
        if not is_unlimited(user.role) and user.resumes_left is not None and user.resumes_left > 0:
            user.resumes_left -= 1
            session.commit()

        balance_text = ""
        if not is_unlimited(user.role):
            balance_text = (
                f"\n\n📊 Баланс: аналитик — {user.analyses_left}, "
                f"резюме — {user.resumes_left}"
            )

        # Store resume data for potential editing
        if context:
            context.user_data["last_resume_data"] = resume_data
            context.user_data["last_resume_analysis_id"] = analysis_id
            context.user_data["last_resume_safe_name"] = safe_name

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Редактировать текст резюме", callback_data="edit_resume_text")],
        ])
        await message.reply_text(
            "✅ Готово! Два файла:\n"
            "• PDF — красивый, для email/ЛС\n"
            "• DOCX — для загрузки на hh.ru\n\n"
            "Нажми «Редактировать» чтобы поправить текст, "
            "или отправь ссылку на другую вакансию."
            + balance_text,
            reply_markup=kb,
        )

    except Exception as e:
        logger.error("Resume generation failed: %s", e)
        await message.reply_text(f"❌ Ошибка генерации резюме: {e}")
    finally:
        session.close()


# =============================================================================
# Balance & payments
# =============================================================================

async def _show_payment_options(message, resource: str):
    prefix = (
        "⚠️ Бесплатные аналитики закончились.\n\n"
        if resource == "analyses"
        else "⚠️ Бесплатные генерации резюме закончились.\n\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ 25 Stars — Старт (5+5)", callback_data="buy_stars_pack_5")],
        [InlineKeyboardButton("⭐ 60 Stars — Стандарт (20+15)", callback_data="buy_stars_pack_20")],
        [InlineKeyboardButton("⭐ 120 Stars — Про (50+30)", callback_data="buy_stars_pack_50")],
        [InlineKeyboardButton("⭐ 200 Stars — Макс (100+100)", callback_data="buy_stars_pack_100")],
        [InlineKeyboardButton("💳 Картой (Paymaster)", callback_data="buy_card_menu")],
        [InlineKeyboardButton("💰 Перевод на Сбер", callback_data="buy_sber")],
    ])
    await message.reply_text(
        prefix +
        "📦 **Пакеты:**\n"
        "• Старт: 5 аналитик + 5 резюме — 25 ⭐ / 199 ₽\n"
        "• Стандарт: 20 аналитик + 15 резюме — 60 ⭐ / 499 ₽\n"
        "• Про: 50 аналитик + 30 резюме — 120 ⭐ / 999 ₽\n"
        "• Макс: 100 аналитик + 100 резюме — 200 ⭐ / 1 999 ₽",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = _get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Используй /start для начала.")
        return

    if is_unlimited(user.role):
        await update.message.reply_text("♾ Безлимитный доступ")
        return

    text = (
        f"📊 **Твой баланс:**\n\n"
        f"🔍 Аналитик вакансий: {user.analyses_left}\n"
        f"📄 Генераций резюме: {user.resumes_left}\n\n"
    )
    if user.analyses_left <= 0 or user.resumes_left <= 0:
        text += "Пополнить 👇"
    else:
        text += "Для пополнения — /buy"

    kb = None
    if user.analyses_left <= 0 or user.resumes_left <= 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Купить пакет", callback_data="buy_menu")],
        ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_payment_options(update.message, "buy")


async def _send_stars_invoice(message, tg_user, pack_id: str):
    """Send Telegram Stars invoice."""
    pack = PACKAGES.get(pack_id)
    if not pack:
        return

    await message.reply_invoice(
        title=f"CareerBot — {pack['label']}",
        description=_pack_description(pack),
        payload=f"stars_{pack_id}_{tg_user.id}",
        provider_token="",  # Empty for Stars
        currency="XTR",
        prices=[LabeledPrice(label=pack["label"], amount=pack["stars"])],
    )


async def _send_paymaster_invoice(message, tg_user, pack_id: str):
    """Create Paymaster payment link via REST API and send as button."""
    pack = PACKAGES.get(pack_id)
    if not pack:
        return

    # Try native Telegram Payments first (if BotFather token exists)
    if PAYMASTER_TOKEN:
        await message.reply_invoice(
            title=f"CareerBot — {pack['label']}",
            description=_pack_description(pack),
            payload=f"paymaster_{pack_id}_{tg_user.id}",
            provider_token=PAYMASTER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=pack["label"], amount=pack["rub"])],
        )
        return

    # Fallback: Paymaster REST API → payment link
    if not PAYMASTER_API_TOKEN or not PAYMASTER_MERCHANT_ID:
        await message.reply_text("💳 Оплата картой временно недоступна. Используй Stars.")
        return

    amount_rub = pack["rub"] / 100  # kopecks → rubles
    payload = f"career_{pack_id}_{tg_user.id}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://paymaster.ru/api/v2/invoices",
                headers={
                    "Authorization": f"Bearer {PAYMASTER_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "merchantId": PAYMASTER_MERCHANT_ID,
                    "amount": {"value": amount_rub, "currency": "RUB"},
                    "description": f"CareerBot — {pack['label']}",
                    "paymentData": {"paymentId": payload},
                },
            )
            data = resp.json()

        pay_url = data.get("url")
        if not pay_url:
            logger.error("Paymaster no URL: %s", data)
            await message.reply_text("❌ Ошибка создания платежа. Попробуй Stars.")
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Оплатить {amount_rub:.0f} ₽", url=pay_url)],
        ])
        await message.reply_text(
            f"📦 Пакет: **{pack['label']}**\n"
            f"💰 Сумма: {amount_rub:.0f} ₽\n\n"
            f"Нажми кнопку для оплаты картой.\n"
            f"После оплаты напиши @Amoskv для активации.",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    except Exception as e:
        logger.error("Paymaster API error: %s", e)
        await message.reply_text(f"❌ Ошибка платёжной системы: {e}\nПопробуй Stars.")


def _pack_description(pack: dict) -> str:
    return f"{pack['analyses']} аналитик вакансий + {pack['resumes']} генераций резюме"


async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve payment within 10 seconds."""
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process successful payment — add balance."""
    payment = update.message.successful_payment
    tg_user = update.effective_user

    # Parse payload: "stars_pack_10_123456" or "paymaster_pack_30_123456"
    parts = payment.invoice_payload.split("_", 2)
    provider = parts[0] if parts else "unknown"
    pack_id = "_".join(parts[1:-1]) if len(parts) >= 3 else ""
    # Reconstruct pack_id properly
    payload = payment.invoice_payload
    for pid in PACKAGES:
        if pid in payload:
            pack_id = pid
            break

    pack = PACKAGES.get(pack_id)
    if not pack:
        logger.error("Unknown package in payment: %s", payment.invoice_payload)
        await update.message.reply_text("❌ Ошибка: неизвестный пакет. Обратись к @Amoskv")
        return

    # Save payment to DB
    session = get_session()
    user = session.query(User).filter_by(tg_id=tg_user.id).first()
    if not user:
        session.close()
        return

    pay_record = Payment(
        user_id=user.id,
        provider=provider,
        currency=payment.currency,
        amount=payment.total_amount,
        package=pack_id,
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id or "",
    )
    session.add(pay_record)

    # Apply package — add to balance
    if user.analyses_left is None or user.analyses_left < 0:
        user.analyses_left = pack["analyses"]
    else:
        user.analyses_left += pack["analyses"]
    if user.resumes_left is None or user.resumes_left < 0:
        user.resumes_left = pack["resumes"]
    else:
        user.resumes_left += pack["resumes"]

    session.commit()
    new_analyses = user.analyses_left
    new_resumes = user.resumes_left
    user_role = user.role
    session.close()

    # Confirm to user
    balance_text = f"🔍 Аналитик: {new_analyses}\n📄 Резюме: {new_resumes}"

    await update.message.reply_text(
        f"✅ Оплата прошла!\n\n"
        f"📦 Пакет: {pack['label']}\n"
        f"📊 Баланс:\n{balance_text}\n\n"
        f"Отправляй ссылку на вакансию — поехали! 🚀"
    )

    # Notify admin
    await _notify_admin(
        context.bot,
        f"💰 Оплата CareerBot!\n"
        f"👤 {tg_user.full_name} (@{tg_user.username or '—'})\n"
        f"📦 {pack['label']} ({provider})\n"
        f"💵 {payment.total_amount} {payment.currency}\n"
        f"🆔 {payment.telegram_payment_charge_id}",
    )


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
# Global error handler
# =============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — logs errors and notifies user."""
    err = context.error

    # Silently ignore polling network errors (TimedOut, Bad Gateway)
    # These are transient Telegram API hiccups, not bugs
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Transient Telegram error (ignored): %s", err)
        return

    if isinstance(err, httpx.TimeoutException):
        logger.warning("httpx timeout (ignored): %s", err)
        return

    logger.error("Exception while handling an update:", exc_info=context.error)

    # Build short error description
    if isinstance(err, httpx.HTTPStatusError):
        err_text = f"🌐 Ошибка HTTP {err.response.status_code}. Попробуй позже."
    elif isinstance(err, (ConnectionError, httpx.ConnectError)):
        err_text = "🔌 Ошибка соединения. Проверю и попробуй позже."
    else:
        err_text = "❌ Произошла ошибка. Попробуй ещё раз или напиши /start"

    # Try to notify the user
    if update and hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text(err_text)
        except Exception:
            pass
    elif update and hasattr(update, "callback_query") and update.callback_query:
        try:
            await update.callback_query.message.reply_text(err_text)
        except Exception:
            pass

    # Notify admin about non-trivial errors only
    try:
        tb = traceback.format_exception(type(err), err, err.__traceback__)
        short_tb = "".join(tb[-3:])[:1000]
        await context.bot.send_message(
            chat_id=ADMIN_TG_ID,
            text=f"🚨 CareerBot error:\n<code>{short_tb}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# =============================================================================
# Main
# =============================================================================

def main():
    init_db()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(HTTPXRequest(
            connect_timeout=10.0,
            read_timeout=45.0,  # long-polling needs longer read timeout
            write_timeout=10.0,
            pool_timeout=10.0,
        ))
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("buy", cmd_buy))

    # Payments (must be before other message handlers)
    app.add_handler(PreCheckoutQueryHandler(handle_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Documents
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("CareerBot starting...")
    app.run_polling(
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,  # long-polling timeout for getUpdates
    )


if __name__ == "__main__":
    main()
