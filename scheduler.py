import asyncio
import logging
from datetime import datetime, timedelta
from typing import List
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session, User, Vacancy, UserVacancy
from hh_api import hh_api
from resume_analyzer import resume_analyzer
from config import settings

logger = logging.getLogger(__name__)

# Global scheduler task
scheduler_task = None


async def check_new_vacancies(bot):
    """Check for new vacancies and notify subscribed users"""
    logger.info("Running scheduled vacancy check...")
    
    async with async_session() as session:
        # Get all subscribed users
        result = await session.execute(
            select(User).where(User.is_subscribed == True)
        )
        users = result.scalars().all()
        
        if not users:
            logger.info("No subscribed users found")
            return
        
        for user in users:
            try:
                await process_user_vacancies(session, user, bot)
            except Exception as e:
                logger.error(f"Error processing user {user.telegram_id}: {e}")
                continue
        
        await session.commit()
    
    logger.info("Scheduled check completed")


async def process_user_vacancies(session: AsyncSession, user: User, bot):
    """Process vacancies for a single user"""
    import json
    
    if not user.resume_text:
        return
    
    # Parse user analysis
    try:
        skills = json.loads(user.skills.replace("'", '"')) if user.skills else []
    except:
        skills = []
    
    analysis = {
        "skills": skills,
        "experience_years": user.experience_years,
        "desired_position": user.desired_position,
        "experience_level": None,
    }
    
    # Generate search query
    search_query = await resume_analyzer.generate_search_query(analysis)
    
    # Calculate date from (last check or 24 hours ago)
    if user.last_check_at:
        date_from = user.last_check_at
    else:
        date_from = datetime.utcnow() - timedelta(hours=24)
    
    # Search for new vacancies
    try:
        vacancies = await hh_api.search_vacancies_all(
            text=search_query,
            date_from=date_from,
            max_results=100
        )
    except Exception as e:
        logger.error(f"Error searching vacancies for user {user.telegram_id}: {e}")
        return
    
    if not vacancies:
        # No new vacancies - send notification anyway
        await send_no_new_vacancies_notification(bot, user)
        user.last_check_at = datetime.utcnow()
        return
    
    # Filter and score vacancies
    new_vacancies = []
    for vac in vacancies:
        vac_id = str(vac.get("id"))
        
        # Check if already processed for this user
        # First get vacancy db id
        result = await session.execute(
            select(Vacancy).where(Vacancy.hh_id == vac_id)
        )
        existing_vac = result.scalar_one_or_none()
        
        if existing_vac:
            result = await session.execute(
                select(UserVacancy).where(
                    and_(
                        UserVacancy.user_id == user.id,
                        UserVacancy.vacancy_id == existing_vac.id
                    )
                )
            )
            if result.scalar_one_or_none():
                continue
        
        # Calculate match score
        score = await resume_analyzer.calculate_vacancy_match(analysis, vac)
        
        # Only include vacancies with decent match (>= 40%)
        if score >= 40:
            new_vacancies.append((vac, score))
    
    # Save vacancies to database
    for vac, _ in new_vacancies:
        vac_id = str(vac.get("id"))
        
        result = await session.execute(
            select(Vacancy).where(Vacancy.hh_id == vac_id)
        )
        existing = result.scalar_one_or_none()
        
        if not existing:
            salary = vac.get("salary", {}) or {}
            new_vacancy = Vacancy(
                hh_id=vac_id,
                name=vac.get("name", ""),
                employer_name=vac.get("employer", {}).get("name"),
                url=vac.get("alternate_url", ""),
                salary_from=salary.get("from"),
                salary_to=salary.get("to"),
                salary_currency=salary.get("currency"),
                location=vac.get("area", {}).get("name"),
                experience=vac.get("experience", {}).get("name"),
                employment_type=vac.get("employment", {}).get("name"),
                schedule=vac.get("schedule", {}).get("name"),
                published_at=datetime.fromisoformat(vac.get("published_at", "").replace("Z", "+00:00")).replace(tzinfo=None)
            )
            session.add(new_vacancy)
            await session.flush()
            vacancy_db_id = new_vacancy.id
        else:
            vacancy_db_id = existing.id
        
        # Create user-vacancy link
        user_vacancy = UserVacancy(
            user_id=user.id,
            vacancy_id=vacancy_db_id,
            is_sent=False
        )
        session.add(user_vacancy)
    
    await session.commit()
    
    # Send notification
    if new_vacancies:
        await send_new_vacancies_notification(bot, user, new_vacancies)
    else:
        await send_no_new_vacancies_notification(bot, user)
    
    # Update last check time
    user.last_check_at = datetime.utcnow()


# Store new vacancies for each user (in-memory storage)
user_new_vacancies = {}


async def send_new_vacancies_notification(bot, user: User, vacancies: List):
    """Send notification about new vacancies - compact version"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    count = len(vacancies)
    
    # Sort by match score
    vacancies.sort(key=lambda x: x[1], reverse=True)
    
    # Store vacancies for this user
    user_new_vacancies[user.id] = vacancies
    
    message = f"🔔 <b>Новые вакансии!</b>\n\n"
    message += f"Найдено {count} подходящих вакансий по твоему резюме.\n\n"
    message += f"Нажми кнопку ниже, чтобы посмотреть список:"
    
    # Create buttons for viewing all
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📋 Показать все ({count})", callback_data=f"show_new_vacancies_{user.id}")]
    ])
    
    try:
        await bot.send_message(
            user.telegram_id,
            message,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error sending notification to {user.telegram_id}: {e}")


async def send_no_new_vacancies_notification(bot, user: User):
    """Send notification when no new vacancies found"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    current_time = datetime.utcnow().strftime("%H:%M")
    
    message = (
        f"🔔 <b>Проверка вакансий ({current_time})</b>\n\n"
        f"😔 Новых подходящих вакансий не найдено.\n\n"
        f"Я продолжу следить за обновлениями каждые {settings.CHECK_INTERVAL_MINUTES} минут."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти вакансии вручную", callback_data="search_vacancies")]
    ])
    
    try:
        await bot.send_message(
            user.telegram_id,
            message,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error sending notification to {user.telegram_id}: {e}")


async def scheduler_loop(bot):
    """Main scheduler loop"""
    while True:
        try:
            await check_new_vacancies(bot)
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}")
        
        # Wait for next check
        await asyncio.sleep(settings.CHECK_INTERVAL_MINUTES * 60)


async def start_scheduler(bot):
    """Start the scheduler"""
    global scheduler_task
    
    if scheduler_task is None or scheduler_task.done():
        scheduler_task = asyncio.create_task(scheduler_loop(bot))
        logger.info(f"Scheduler started (interval: {settings.CHECK_INTERVAL_MINUTES} minutes)")


async def stop_scheduler():
    """Stop the scheduler"""
    global scheduler_task
    
    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        logger.info("Scheduler stopped")
