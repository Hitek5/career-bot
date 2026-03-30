import json
import logging

from core.ai_engine import call_claude
from parsers.vacancy_parser import (
    extract_hh_vacancy_id, fetch_vacancy, fetch_employer, parse_vacancy_data,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — карьерный консультант. Проанализируй вакансию и сопоставь с профилем кандидата.

Верни JSON:
{
    "match_percent": 78,
    "matching_skills": ["навык1", "навык2"],
    "gaps": ["пробел — пояснение"],
    "recommendation": "Подавать / Не подавать / С оговорками",
    "recommendation_detail": "Подробное обоснование",
    "resume_focus": ["на чём акцентировать в резюме"],
    "company_research": "Информация о компании",
    "analysis_text": "Полный текст для чата:\\n📋 Вакансия: ...\\n🏢 Компания: ...\\n💰 Зарплата: ...\\n\\nСоответствие профилю: X% ✅/⚠️\\n\\n✅ Совпадает:\\n- ...\\n\\n⚠️ Пробелы:\\n- ...\\n\\n💡 Рекомендация: ..."
}

Ответ ТОЛЬКО JSON, без markdown-обёртки."""


def _clean_json(text: str) -> str:
    import re
    m = re.search(r'```(?:json)?\s*\n(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'(\{[\s\S]*\})', text)
    if m:
        return m.group(1).strip()
    return text.strip()


async def _analyze(vacancy_text: str, profile_json: str,
                   company_info: str = "") -> dict:
    user_msg = (
        f"ВАКАНСИЯ:\n{vacancy_text}\n\n"
        f"ИНФОРМАЦИЯ О КОМПАНИИ:\n{company_info or 'Не найдена'}\n\n"
        f"ПРОФИЛЬ КАНДИДАТА:\n{profile_json}"
    )
    response = await call_claude(SYSTEM_PROMPT, user_msg, max_tokens=4096)
    try:
        return json.loads(_clean_json(response))
    except json.JSONDecodeError:
        logger.error("Bad analysis JSON: %s", response[:300])
        raise ValueError("Ошибка анализа вакансии. Попробуйте ещё раз.")


async def process_vacancy_url(url: str, profile_json: str) -> dict:
    """Full pipeline: parse HH URL -> fetch -> research company -> analyze."""
    vacancy_id = extract_hh_vacancy_id(url)
    if not vacancy_id:
        raise ValueError("Не удалось извлечь ID вакансии из ссылки")

    raw = await fetch_vacancy(vacancy_id)
    vacancy = parse_vacancy_data(raw)

    # Research company
    company_info = ""
    if vacancy.get("employer_id"):
        try:
            emp = await fetch_employer(vacancy["employer_id"])
            industries = ", ".join(i.get("name", "") for i in emp.get("industries", []))
            company_info = (
                f"Компания: {emp.get('name', '')}\n"
                f"Описание: {(emp.get('description') or 'Нет')[:500]}\n"
                f"Отрасль: {industries}\n"
                f"Сайт: {emp.get('site_url', 'Н/Д')}"
            )
        except Exception as e:
            logger.warning("Employer fetch failed: %s", e)

    vacancy_text = (
        f"Позиция: {vacancy['name']}\n"
        f"Компания: {vacancy['company']}\n"
        f"Зарплата: {vacancy['salary']}\n"
        f"Опыт: {vacancy['experience']}\n"
        f"Занятость: {vacancy['employment']}\n"
        f"График: {vacancy['schedule']}\n"
        f"Город: {vacancy['area']}\n"
        f"Ключевые навыки: {', '.join(vacancy['key_skills'])}\n\n"
        f"Описание:\n{vacancy['description']}"
    )

    analysis = await _analyze(vacancy_text, profile_json, company_info)
    analysis["vacancy"] = vacancy
    return analysis


async def process_vacancy_text(text: str, profile_json: str) -> dict:
    """Analyze raw vacancy text (no HH URL)."""
    analysis = await _analyze(text, profile_json)
    analysis["vacancy"] = {"name": "Из текста", "company": "", "salary": "", "url": ""}
    return analysis
