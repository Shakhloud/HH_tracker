import json
from typing import Dict, Any, Optional
import aiohttp

from config import settings


class ResumeAnalyzer:
    def __init__(self):
        self.api_key = settings.OPENROUTER_API_KEY
        self.model = settings.OPENROUTER_MODEL
        self.base_url = "https://openrouter.ai/api/v1"
    
    async def analyze_resume(self, resume_text: str) -> Dict[str, Any]:
        """Analyze resume text and extract key information"""
        
        prompt = f"""Ты - эксперт по анализу резюме. Проанализируй следующее резюме и извлеки ключевую информацию.

ВАЖНО: Верни результат ТОЛЬКО в виде JSON объекта. Не добавляй пояснений, markdown форматирования или другого текста.

Резюме для анализа:
---
{resume_text}
---

Извлеки следующие поля:
1. "skills" - массив строк с ключевыми навыками и технологиями (минимум 5 навыков, если есть в резюме)
2. "experience_years" - число, общий стаж работы в годах (посчитай по датам в опыте работы)
3. "desired_position" - строка, должность которую ищет кандидат (из раздела "Желаемая должность" или по опыту работы)
4. "location" - строка, город/регион проживания кандидата
5. "salary_expectation" - строка, ожидаемая зарплата (если указана)
6. "experience_level" - одно из: "noExperience", "between1And3", "between3And6", "moreThan6"
7. "employment_types" - массив: ["full", "part", "project", "volunteer", "probation"] - выбери подходящие
8. "schedule_types" - массив: ["fullDay", "shift", "flexible", "remote", "flyInFlyOut"] - выбери подходящие

Пример правильного ответа:
{{"skills": ["Python", "Django", "PostgreSQL", "Docker", "Git"], "experience_years": 5, "desired_position": "Backend разработчик", "location": "Москва", "salary_expectation": "200000 руб", "experience_level": "between3And6", "employment_types": ["full"], "schedule_types": ["remote", "fullDay"]}}

Если какое-то поле невозможно определить, используй null для чисел и пустую строку "" для строк.

Твой ответ (только JSON):"""

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "Ты - эксперт по анализу резюме. Извлекай информацию точно и структурированно."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000
                }
                
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        print(f"API error: {response.status} - {error_text}")
                        if response.status == 401:
                            raise Exception("API error 401: Неверный API ключ OpenRouter. Проверьте OPENROUTER_API_KEY в .env файле.")
                        elif response.status == 404:
                            raise Exception(f"API error 404: Модель {self.model} не найдена. Попробуйте другую модель.")
                        else:
                            raise Exception(f"API error: {response.status}")
                    
                    data = await response.json()
                    content = data["choices"][0]["message"]["content"]
                    
                    # Log raw response for debugging
                    print(f"Raw AI response: {content[:500]}...")
            
            # Try to parse JSON from response
            content = content.strip()
            
            # Remove markdown code blocks
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            # Try to find JSON in the response
            try:
                result = json.loads(content)
                print(f"Parsed result: {result}")
                return result
            except json.JSONDecodeError as je:
                print(f"JSON parse error: {je}")
                print(f"Content that failed to parse: {content}")
                # Try to extract JSON from text
                import re
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group())
                        print(f"Extracted JSON: {result}")
                        return result
                    except:
                        pass
                raise
            
        except Exception as e:
            print(f"Error analyzing resume: {e}")
            import traceback
            traceback.print_exc()
            # Return default structure on error
            return {
                "skills": [],
                "experience_years": None,
                "desired_position": None,
                "location": None,
                "salary_expectation": None,
                "experience_level": None,
                "employment_types": [],
                "schedule_types": []
            }
    
    async def generate_search_query(self, resume_analysis: Dict[str, Any]) -> str:
        """Generate search query based on resume analysis"""
        
        skills = resume_analysis.get("skills", [])
        position = resume_analysis.get("desired_position", "")
        
        # Build search query from position and top skills
        query_parts = []
        
        if position:
            query_parts.append(position)
        
        if skills:
            # Add top 5 most relevant skills
            top_skills = skills[:5]
            query_parts.extend(top_skills)
        
        # Remove duplicates and join
        unique_parts = list(dict.fromkeys(query_parts))
        return " OR ".join(unique_parts) if unique_parts else "IT"
    
    async def calculate_vacancy_match(
        self,
        resume_analysis: Dict[str, Any],
        vacancy: Dict[str, Any]
    ) -> float:
        """Calculate match score between resume and vacancy (0-100)
        
        Returns -1 if vacancy experience requirements exceed resume experience
        """
        
        vacancy_name = (vacancy.get("name") or "").lower()
        snippet = vacancy.get("snippet") or {}
        vacancy_description = (snippet.get("requirement") or "").lower()
        vacancy_description += " " + (snippet.get("responsibility") or "").lower()
        
        resume_skills = [s.lower() for s in (resume_analysis.get("skills") or [])]
        desired_position = (resume_analysis.get("desired_position") or "").lower()
        resume_exp_years = resume_analysis.get("experience_years") or 0
        
        # Get vacancy experience requirements
        vacancy_exp = vacancy.get("experience", {}).get("id", "")
        
        # Strict experience filtering
        # If resume has 1 year exp, filter out vacancies requiring 3+ years
        exp_mapping = {
            "noExperience": 0,
            "between1And3": 1,
            "between3And6": 3,
            "moreThan6": 6
        }
        
        vac_min_exp = exp_mapping.get(vacancy_exp, 0)
        
        # If vacancy requires more experience than candidate has, reject it
        if vac_min_exp > resume_exp_years:
            return -1
        
        # If vacancy requires significantly more experience (e.g., 3+ years vs 1 year), reject
        if vac_min_exp - resume_exp_years >= 2:
            return -1
        
        score = 0
        max_score = 100
        
        # Check position match (40 points) - increased weight
        if desired_position:
            # Exact match in title
            if desired_position in vacancy_name:
                score += 40
            # Partial match (keywords)
            elif any(word in vacancy_name for word in desired_position.split() if len(word) > 3):
                score += 25
        
        # Check skills match (50 points)
        if resume_skills:
            matched_skills = 0
            for skill in resume_skills:
                skill_lower = skill.lower()
                if skill_lower in vacancy_name or skill_lower in vacancy_description:
                    matched_skills += 1
            
            skill_match_ratio = matched_skills / len(resume_skills)
            score += int(50 * skill_match_ratio)
        
        # Experience match (10 points) - only if experience is compatible
        if vacancy_exp:
            resume_exp_level = resume_analysis.get("experience_level", "")
            if resume_exp_level:
                res_mapping = {
                    "noExperience": 0,
                    "between1And3": 1,
                    "between3And6": 2,
                    "moreThan6": 3
                }
                vac_level = res_mapping.get(vacancy_exp, -1)
                res_level = res_mapping.get(resume_exp_level, -1)
                
                if vac_level == res_level:
                    score += 10
                elif vac_level < res_level:
                    # Vacancy requires less experience than candidate has - good match
                    score += 5
        
        return min(score, max_score)


    async def generate_cover_letter(self, resume_text: str, vacancy: Dict[str, Any]) -> str:
        """Generate short cover letter based on resume and vacancy"""
        
        vacancy_name = vacancy.get("name", "")
        employer = vacancy.get("employer", {}).get("name", "")
        snippet = vacancy.get("snippet", {})
        requirements = snippet.get("requirement", "")
        
        prompt = f"""Ты - эксперт по написанию сопроводительных писем. Напиши КОРОТКОЕ сопроводительное письмо на основе резюме кандидата и вакансии.

Резюме кандидата:
---
{resume_text[:2000]}
---

Информация о вакансии:
- Должность: {vacancy_name}
- Компания: {employer}
- Требования: {requirements}

Напиши сопроводительное письмо на русском языке ТОЛЬКО 2-3 предложениями:
1. Первое предложение - приветствие и интерес к позиции
2. Второе предложение - почему ты подходишь (1-2 ключевых навыка/опыта)
3. Третье предложение (опционально) - призыв к действию

Письмо должно звучать естественно, по-человечески, НЕ как шаблон. Максимум 50-80 слов. Без общих фраз типа "я увидел вашу вакансию на сайте"."""

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "Ты - эксперт по написанию сопроводительных писем. Пиши убедительные и профессиональные письма."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1500
                }
                
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    print(f"Cover letter API response status: {response.status}")
                    if response.status != 200:
                        error_text = await response.text()
                        print(f"Cover letter API error: {response.status} - {error_text}")
                        return None
                    
                    data = await response.json()
                    print(f"Cover letter API data received: {list(data.keys())}")
                    
                    if "choices" not in data or not data["choices"]:
                        print(f"Cover letter API no choices in response: {data}")
                        return "Не удалось сгенерировать письмо. Попробуйте позже."
                    
                    cover_letter = data["choices"][0]["message"]["content"]
                    print(f"Cover letter generated, length: {len(cover_letter)}")
                    return cover_letter.strip() if cover_letter else "Не удалось сгенерировать письмо."
                    
        except Exception as e:
            print(f"Error generating cover letter: {e}")
            import traceback
            traceback.print_exc()
            return None


resume_analyzer = ResumeAnalyzer()