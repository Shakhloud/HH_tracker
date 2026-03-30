import aiohttp
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import urllib.parse

from config import settings


class HHAPI:
    def __init__(self):
        self.base_url = settings.HH_API_URL
        self.headers = {
            "User-Agent": settings.HH_USER_AGENT
        }
    
    async def search_vacancies(
        self,
        text: Optional[str] = None,
        area: Optional[int] = None,
        experience: Optional[str] = None,
        employment: Optional[str] = None,
        schedule: Optional[str] = None,
        salary: Optional[int] = None,
        only_with_salary: bool = False,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 0,
        per_page: int = 100
    ) -> Dict[str, Any]:
        """Search vacancies on hh.ru"""
        url = f"{self.base_url}/vacancies"
        
        params = {
            "page": page,
            "per_page": min(per_page, 100),  # Max 100 per page
        }
        
        if text:
            params["text"] = text
        if area:
            params["area"] = area
        if experience:
            params["experience"] = experience
        if employment:
            params["employment"] = employment
        if schedule:
            params["schedule"] = schedule
        if salary:
            params["salary"] = salary
        if only_with_salary:
            params["only_with_salary"] = "true"
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"HH API error: {response.status} - {await response.text()}")
    
    async def get_vacancy(self, vacancy_id: str) -> Dict[str, Any]:
        """Get detailed vacancy info"""
        url = f"{self.base_url}/vacancies/{vacancy_id}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"HH API error: {response.status} - {await response.text()}")
    
    async def get_areas(self) -> List[Dict[str, Any]]:
        """Get list of areas (regions)"""
        url = f"{self.base_url}/areas"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"HH API error: {response.status} - {await response.text()}")
    
    async def get_dictionaries(self) -> Dict[str, Any]:
        """Get reference dictionaries (experience, employment, schedule)"""
        url = f"{self.base_url}/dictionaries"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"HH API error: {response.status} - {await response.text()}")
    
    async def search_vacancies_all(
        self,
        text: Optional[str] = None,
        area: Optional[int] = None,
        experience: Optional[str] = None,
        employment: Optional[str] = None,
        schedule: Optional[str] = None,
        salary: Optional[int] = None,
        only_with_salary: bool = False,
        date_from: Optional[datetime] = None,
        max_results: int = 150
    ) -> List[Dict[str, Any]]:
        """Search all vacancies with pagination"""
        all_vacancies = []
        page = 0
        
        while len(all_vacancies) < max_results:
            try:
                result = await self.search_vacancies(
                    text=text,
                    area=area,
                    experience=experience,
                    employment=employment,
                    schedule=schedule,
                    salary=salary,
                    only_with_salary=only_with_salary,
                    date_from=date_from,
                    page=page,
                    per_page=100
                )
                
                items = result.get("items", [])
                if not items:
                    break
                
                all_vacancies.extend(items)
                
                # Check if there are more pages
                pages = result.get("pages", 0)
                if page >= pages - 1:
                    break
                
                page += 1
                
            except Exception as e:
                print(f"Error fetching page {page}: {e}")
                break
        
        return all_vacancies[:max_results]


hh_api = HHAPI()
