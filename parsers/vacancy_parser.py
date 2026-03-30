import re
from typing import Optional

import httpx

HH_API = "https://api.hh.ru"


def extract_hh_vacancy_id(url: str) -> Optional[str]:
    """Extract vacancy ID from hh.ru URL."""
    m = re.search(r'hh\.ru/vacancy/(\d+)', url)
    return m.group(1) if m else None


async def fetch_vacancy(vacancy_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{HH_API}/vacancies/{vacancy_id}")
        r.raise_for_status()
        return r.json()


async def fetch_employer(employer_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{HH_API}/employers/{employer_id}")
        r.raise_for_status()
        return r.json()


async def search_similar(text: str, limit: int = 3) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{HH_API}/vacancies",
            params={"text": text, "per_page": limit, "area": 1},
        )
        r.raise_for_status()
        return r.json().get("items", [])


def parse_vacancy_data(data: dict) -> dict:
    """Convert raw HH API vacancy response to a flat dict."""
    salary = data.get("salary")
    salary_str = "Не указана"
    if salary:
        parts = []
        if salary.get("from"):
            parts.append(f"от {salary['from']:,}")
        if salary.get("to"):
            parts.append(f"до {salary['to']:,}")
        cur = salary.get("currency", "RUR")
        salary_str = " ".join(parts) + f" {cur}"
        if salary.get("gross"):
            salary_str += " gross"

    return {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "company": data.get("employer", {}).get("name", ""),
        "employer_id": data.get("employer", {}).get("id"),
        "salary": salary_str,
        "experience": data.get("experience", {}).get("name", ""),
        "employment": data.get("employment", {}).get("name", ""),
        "schedule": data.get("schedule", {}).get("name", ""),
        "description": re.sub(r"<[^>]+>", "", data.get("description", "")),
        "key_skills": [s.get("name", "") for s in data.get("key_skills", [])],
        "area": data.get("area", {}).get("name", ""),
        "url": data.get("alternate_url", ""),
    }
