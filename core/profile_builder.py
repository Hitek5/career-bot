import json
import logging

from core.ai_engine import call_claude

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — карьерный аналитик. Тебе даны документы пользователя (резюме, сертификаты, рабочие тетради) и ответы на вопросы.

Создай структурированный цифровой профиль в формате JSON:
{
    "full_name": "Имя Фамилия",
    "contacts": {"phone": "", "email": "", "telegram": "", "linkedin": ""},
    "summary": "Краткое описание 2-3 предложения — продающее",
    "target_positions": ["позиция1", "позиция2"],
    "salary_range": "от X до Y",
    "work_format": "удалённо/офис/гибрид",
    "skills": {
        "hard": [{"name": "навык", "level": "уровень"}],
        "soft": [{"name": "навык", "level": "уровень"}]
    },
    "experience": [
        {
            "company": "компания",
            "position": "должность",
            "period": "2020 — н.в.",
            "description": "краткое описание",
            "achievements": ["достижение с конкретной цифрой"]
        }
    ],
    "education": [
        {"institution": "ВУЗ", "degree": "степень", "field": "направление", "year": "2008"}
    ],
    "certifications": ["сертификат"],
    "languages": [{"language": "Русский", "level": "родной"}],
    "strengths": ["сильная сторона"],
    "growth_areas": ["зона роста"],
    "values": ["ценность"],
    "profile_summary_for_user": "Текст сводки для показа в чате: ключевые компетенции, целевые позиции, сильные стороны, рекомендации по зарплатному диапазону"
}

Правила:
- Извлеки ВСЕ достижения с конкретными цифрами
- Определи уровень каждого навыка
- Summary должно продавать кандидата
- profile_summary_for_user — развёрнутая сводка на русском
- Ответ ТОЛЬКО JSON, без markdown-обёртки"""


def _clean_json(text: str) -> str:
    """Extract JSON from response that may have preamble text and code fences."""
    # Find the JSON block - look for ```json ... ``` first
    import re
    m = re.search(r'```(?:json)?\s*\n(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try finding raw JSON object
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return m.group(1).strip()
    return text.strip()


async def build_profile(documents_text: str, answers_text: str) -> dict:
    """Build a digital career profile from documents and user answers."""
    user_msg = f"ДОКУМЕНТЫ ПОЛЬЗОВАТЕЛЯ:\n{documents_text}\n\nОТВЕТЫ НА ВОПРОСЫ:\n{answers_text}"
    response = await call_claude(SYSTEM_PROMPT, user_msg, max_tokens=8000)

    try:
        return json.loads(_clean_json(response))
    except json.JSONDecodeError:
        logger.error("Bad profile JSON: %s", response[:300])
        raise ValueError("AI вернул некорректный профиль. Попробуйте ещё раз.")
