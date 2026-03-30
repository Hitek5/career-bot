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
            [InlineKeyboardButton("🔄 Пересоздать профиль", callback_data="restart_onboarding")],
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
    _update_state(update.effective_user.id, STATE_UPLOADING)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Документы загружены", callback_data="docs_done")],
    ])
    await update.message.reply_text(
        "📎 Загружай новые документы. Они дополнят существующий профиль.\n"
        "Когда закончишь — нажми кнопку.",
        reply_markup=kb,
    )


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
