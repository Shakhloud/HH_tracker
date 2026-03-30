import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import init_db, async_session, User, Vacancy, UserVacancy
from hh_api import hh_api
from resume_analyzer import resume_analyzer
from pdf_parser import pdf_parser
from scheduler import start_scheduler, stop_scheduler, user_new_vacancies

# Store searched vacancies for each user
user_searched_vacancies = {}

# Store pagination state for each user
user_vacancy_pages = {}

# Items per page
VACANCIES_PER_PAGE = 10

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Parse proxy URL
proxy_config = None
if settings.PROXY_URL:
    # Parse tg://proxy?server=...&port=...&secret=...
    proxy_url = settings.PROXY_URL
    if proxy_url.startswith("tg://proxy?"):
        params = proxy_url.replace("tg://proxy?", "").split("&")
        proxy_dict = {}
        for param in params:
            key, value = param.split("=")
            proxy_dict[key] = value
        
        proxy_config = {
            "server": proxy_dict.get("server"),
            "port": int(proxy_dict.get("port", 8443)),
            "secret": proxy_dict.get("secret")
        }

# Initialize bot with proxy support
if proxy_config:
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiohttp_socks import ProxyConnector
    
    # Create connector with SOCKS5 proxy
    connector = ProxyConnector.from_url(
        f"socks5://{proxy_config['server']}:{proxy_config['port']}"
    )
    
    # Create session with custom connector
    session = AiohttpSession()
    session._connector = connector
    
    bot = Bot(token=settings.BOT_TOKEN, session=session)
else:
    bot = Bot(token=settings.BOT_TOKEN)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# States
class ResumeStates(StatesGroup):
    waiting_for_resume = State()


# Keyboards
def get_main_keyboard(is_subscribed: bool = False):
    buttons = [
        [InlineKeyboardButton(text="🔍 Найти вакансии", callback_data="search_vacancies")],
    ]
    
    if is_subscribed:
        buttons.append([InlineKeyboardButton(text="🔕 Отписаться от рассылки", callback_data="unsubscribe")])
    else:
        buttons.append([InlineKeyboardButton(text="🔔 Подписаться на рассылку", callback_data="subscribe")])
    
    buttons.extend([
        [InlineKeyboardButton(text="📄 Обновить резюме", callback_data="update_resume")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_vacancies_keyboard(vacancies: list):
    """Create keyboard with vacancy names
    
    vacancies: list of tuples (vacancy_dict, vac_id, match_score)
    """
    buttons = []
    for vac, vac_id, score in vacancies[:10]:  # Show max 10 buttons
        vac_name = vac.get("name", "Без названия")
        # Truncate name if too long (max 40 chars for button)
        if len(vac_name) > 35:
            vac_name = vac_name[:32] + "..."
        button_text = f"{vac_name} ({score:.0f}%)"
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"vacancy_{vac_id}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_vacancies_page(message, user_id: int, page: int, sort_by: str = 'date'):
    """Show vacancies page with pagination"""
    data = user_vacancy_pages.get(user_id)
    if not data:
        return
    
    vacancies = data['vacancies']
    total = len(vacancies)
    total_pages = (total + VACANCIES_PER_PAGE - 1) // VACANCIES_PER_PAGE
    
    # Sort vacancies
    if sort_by == 'date':
        def get_published_date(item):
            vac = item[0]
            published = vac.get("published_at", "")
            try:
                return datetime.fromisoformat(published.replace("Z", "+00:00"))
            except:
                return datetime.min
        sorted_vacancies = sorted(vacancies, key=get_published_date, reverse=True)
        title = "📅 Самые свежие вакансии"
    else:  # score
        sorted_vacancies = sorted(vacancies, key=lambda x: x[2], reverse=True)
        title = "🎯 По соответствию"
    
    # Get page slice
    start = page * VACANCIES_PER_PAGE
    end = start + VACANCIES_PER_PAGE
    page_vacancies = sorted_vacancies[start:end]
    
    # Create keyboard
    keyboard_buttons = []
    for vac, vac_id, score in page_vacancies:
        vac_name = vac.get("name", "Без названия")
        if len(vac_name) > 28:
            vac_name = vac_name[:25] + "..."
        button_text = f"{vac_name} ({score:.0f}%)"
        keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"vacancy_{vac_id}")])
    
    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"vac_page_{page-1}_{sort_by}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"vac_page_{page+1}_{sort_by}"))
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)
    
    # Sort toggle button
    if sort_by == 'date':
        keyboard_buttons.append([InlineKeyboardButton(text="🎯 Сортировать по соответствию", callback_data="vac_sort_score")])
    else:
        keyboard_buttons.append([InlineKeyboardButton(text="📅 Сортировать по дате", callback_data="vac_sort_date")])
    
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await message.edit_text(
        f"✅ Найдено {total} подходящих вакансий\n\n"
        f"{title} (страница {page+1} из {total_pages}):\n\n"
        f"Нажми на название для деталей:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# Helper functions
async def get_or_create_user(session: AsyncSession, telegram_user: types.User) -> User:
    """Get existing user or create new one"""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_user.id)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name
        )
        session.add(user)
        await session.commit()
    
    return user


def format_vacancy(vacancy: dict, match_score: Optional[float] = None) -> str:
    """Format vacancy for display"""
    name = vacancy.get("name", "Без названия")
    employer = vacancy.get("employer", {}).get("name", "Не указан")
    url = vacancy.get("alternate_url", "")
    
    # Salary
    salary = vacancy.get("salary")
    if salary:
        salary_from = salary.get("from")
        salary_to = salary.get("to")
        currency = salary.get("currency", "")
        
        if salary_from and salary_to:
            salary_str = f"{salary_from:,} - {salary_to:,} {currency}"
        elif salary_from:
            salary_str = f"от {salary_from:,} {currency}"
        elif salary_to:
            salary_str = f"до {salary_to:,} {currency}"
        else:
            salary_str = "Не указана"
    else:
        salary_str = "Не указана"
    
    # Experience
    experience = vacancy.get("experience", {}).get("name", "Не указан")
    
    # Location
    area = vacancy.get("area", {}).get("name", "Не указан")
    
    # Description snippets
    snippet = vacancy.get("snippet", {})
    requirement = snippet.get("requirement", "")
    responsibility = snippet.get("responsibility", "")
    
    text = f"📌 <b>{name}</b>\n\n"
    text += f"🏢 <b>Компания:</b> {employer}\n"
    text += f"💰 <b>Зарплата:</b> {salary_str}\n"
    text += f"📍 <b>Локация:</b> {area}\n"
    text += f"💼 <b>Опыт:</b> {experience}\n\n"
    
    if match_score:
        text += f"🎯 <b>Соответствие:</b> {match_score:.0f}%\n\n"
    
    if requirement:
        # Clean HTML tags
        requirement = requirement.replace("<highlighttext>", "").replace("</highlighttext>", "")
        text += f"📝 <b>Требования:</b> {requirement[:200]}...\n\n" if len(requirement) > 200 else f"📝 <b>Требования:</b> {requirement}\n\n"
    
    if responsibility:
        responsibility = responsibility.replace("<highlighttext>", "").replace("</highlighttext>", "")
        text += f"🎯 <b>Обязанности:</b> {responsibility[:200]}...\n\n" if len(responsibility) > 200 else f"🎯 <b>Обязанности:</b> {responsibility}\n\n"
    
    text += f"🔗 <a href='{url}'>Открыть на hh.ru</a>"
    
    return text


# Handlers
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command"""
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user)
        
        if not user.resume_text:
            await message.answer(
                "👋 Привет! Я бот для поиска вакансий на hh.ru.\n\n"
                "Чтобы начать, мне нужно проанализировать твое резюме. "
                "Пожалуйста, отправь мне PDF-файл с твоим резюме.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Отправить резюме", callback_data="update_resume")]
                ])
            )
        else:
            await message.answer(
                f"👋 С возвращением, {message.from_user.first_name}!\n\n"
                f"📊 Твое резюме проанализировано.\n"
                f"💼 Желаемая позиция: {user.desired_position or 'Не указана'}\n\n"
                f"Выбери действие:",
                reply_markup=get_main_keyboard(user.is_subscribed)
            )


@dp.callback_query(F.data == "update_resume")
async def process_update_resume(callback: CallbackQuery, state: FSMContext):
    """Start resume update process"""
    await state.set_state(ResumeStates.waiting_for_resume)
    await callback.message.edit_text(
        "📄 Пожалуйста, отправь мне PDF-файл с твоим резюме.\n\n"
        "Я проанализирую его и подберу подходящие вакансии."
    )
    await callback.answer()


@dp.message(ResumeStates.waiting_for_resume, F.document)
async def process_resume_pdf(message: Message, state: FSMContext):
    """Process uploaded PDF resume"""
    document = message.document
    
    # Check if it's a PDF
    if not document.file_name.lower().endswith('.pdf'):
        await message.answer("❌ Пожалуйста, отправь файл в формате PDF.")
        return
    
    # Download file
    processing_msg = await message.answer("⏳ Загружаю и обрабатываю резюме...")
    
    try:
        file = await bot.get_file(document.file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes = file_bytes.read()
        
        # Validate PDF
        if not pdf_parser.validate_pdf(file_bytes):
            await processing_msg.edit_text("❌ Не удалось прочитать PDF файл. Попробуй другой файл.")
            return
        
        # Extract text
        await processing_msg.edit_text("⏳ Извлекаю текст из PDF...")
        resume_text = await pdf_parser.extract_text(file_bytes)
        
        if not resume_text:
            await processing_msg.edit_text("❌ Не удалось извлечь текст из PDF. Попробуй другой файл.")
            return
        
        # Analyze with AI
        await processing_msg.edit_text("🤖 Анализирую резюме с помощью AI...")
        analysis = await resume_analyzer.analyze_resume(resume_text)
        
        # Save to database
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user)
            
            user.resume_text = resume_text
            user.skills = str(analysis.get("skills", []))
            user.experience_years = analysis.get("experience_years")
            user.desired_position = analysis.get("desired_position")
            user.location = analysis.get("location")
            user.salary_expectation = analysis.get("salary_expectation")
            
            await session.commit()
        
        # Show results
        skills_str = ", ".join(analysis.get("skills", [])[:10])
        
        await processing_msg.edit_text(
            f"✅ Резюме успешно проанализировано!\n\n"
            f"💼 <b>Желаемая позиция:</b> {analysis.get('desired_position') or 'Не указана'}\n"
            f"📍 <b>Локация:</b> {analysis.get('location') or 'Не указана'}\n"
            f"💰 <b>Ожидаемая зарплата:</b> {analysis.get('salary_expectation') or 'Не указана'}\n"
            f"📊 <b>Опыт:</b> {analysis.get('experience_years') or 'Не указан'} лет\n"
            f"🛠 <b>Ключевые навыки:</b> {skills_str or 'Не указаны'}\n\n"
            f"Теперь ты можешь искать вакансии!",
            reply_markup=get_main_keyboard(user.is_subscribed),
            parse_mode="HTML"
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error processing resume: {e}")
        await processing_msg.edit_text(
            "❌ Произошла ошибка при обработке резюме. Попробуй еще раз."
        )


@dp.callback_query(F.data == "search_vacancies")
async def process_search_vacancies(callback: CallbackQuery):
    """Search and show vacancies"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if not user.resume_text:
            await callback.message.edit_text(
                "❌ Сначала нужно загрузить резюме!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Загрузить резюме", callback_data="update_resume")]
                ])
            )
            await callback.answer()
            return
        
        # Show loading message
        loading_msg = await callback.message.edit_text("🔍 Ищу подходящие вакансии...")
        
        try:
            # Parse user analysis
            import json
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
            
            # Search vacancies
            vacancies = await hh_api.search_vacancies_all(
                text=search_query,
                max_results=settings.MAX_VACANCIES
            )
            
            if not vacancies:
                await loading_msg.edit_text(
                    "😔 Не найдено подходящих вакансий. Попробуй обновить резюме или изменить критерии поиска.",
                    reply_markup=get_main_keyboard(user.is_subscribed)
                )
                await callback.answer()
                return
            
            # Calculate match scores and filter by experience
            scored_vacancies = []
            for vac in vacancies:
                score = await resume_analyzer.calculate_vacancy_match(analysis, vac)
                # Only include vacancies with valid score (not -1)
                if score >= 0:
                    scored_vacancies.append((vac, str(vac.get("id")), score))
            
            if not scored_vacancies:
                await loading_msg.edit_text(
                    "😔 Не найдено подходящих вакансий по твоему опыту. Попробуй обновить резюме.",
                    reply_markup=get_main_keyboard(user.is_subscribed)
                )
                await callback.answer()
                return
            
            # Store vacancies for this user
            user_searched_vacancies[user.id] = scored_vacancies
            
            # Sort by publication date (newest first)
            def get_published_date(item):
                vac = item[0]
                published = vac.get("published_at", "")
                try:
                    return datetime.fromisoformat(published.replace("Z", "+00:00"))
                except:
                    return datetime.min
            
            scored_vacancies.sort(key=get_published_date, reverse=True)
            
            # Take first 10 for display (newest)
            display_vacancies = scored_vacancies[:10]
            
            # Save vacancies to database
            for vac, vac_id, _ in scored_vacancies:
                # Check if exists
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
            
            await session.commit()
            
            # Store current page for pagination
            user_vacancy_pages[user.id] = {
                'vacancies': scored_vacancies,
                'page': 0,
                'sort_by': 'date'  # 'date' or 'score'
            }
            
            # Show first page
            await show_vacancies_page(loading_msg, user.id, 0, 'date')
            
        except Exception as e:
            logger.error(f"Error searching vacancies: {e}")
            await loading_msg.edit_text(
                "❌ Произошла ошибка при поиске вакансий. Попробуй позже.",
                reply_markup=get_main_keyboard(user.is_subscribed)
            )
    
    await callback.answer()


@dp.callback_query(F.data.startswith("vacancy_"))
async def show_vacancy_details(callback: CallbackQuery):
    """Show vacancy details"""
    vacancy_id = callback.data.replace("vacancy_", "")
    
    try:
        # Get vacancy from HH API for fresh data
        vacancy = await hh_api.get_vacancy(vacancy_id)
        
        text = format_vacancy(vacancy)
        
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Сгенерировать сопроводительное письмо", callback_data=f"cover_letter_{vacancy_id}")],
                [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="show_by_date")],
                [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")]
            ])
        )
        
    except Exception as e:
        logger.error(f"Error showing vacancy: {e}")
        await callback.answer("❌ Ошибка загрузки вакансии")
    
    await callback.answer()


@dp.callback_query(F.data.startswith("cover_letter_"))
async def generate_cover_letter_handler(callback: CallbackQuery):
    """Generate cover letter for vacancy"""
    vacancy_id = callback.data.replace("cover_letter_", "")
    
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if not user.resume_text:
            await callback.answer("❌ Сначала нужно загрузить резюме!")
            return
        
        # Show loading message
        loading_msg = await callback.message.edit_text(
            "⏳ Генерирую сопроводительное письмо...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data=f"vacancy_{vacancy_id}")]
            ])
        )
        
        try:
            # Get vacancy details
            vacancy = await hh_api.get_vacancy(vacancy_id)
            
            # Generate cover letter
            cover_letter = await resume_analyzer.generate_cover_letter(user.resume_text, vacancy)
            
            if not cover_letter:
                await loading_msg.edit_text(
                    "❌ Не удалось сгенерировать письмо. Попробуйте позже.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔙 Назад к вакансии", callback_data=f"vacancy_{vacancy_id}")]
                    ])
                )
                return
            
            # Get vacancy URL
            vacancy_url = vacancy.get("alternate_url", "")
            
            # Show cover letter with vacancy link
            await loading_msg.edit_text(
                f"📝 <b>Сопроводительное письмо</b>\n\n"
                f"<pre>{cover_letter[:3500]}</pre>\n\n"
                f"🔗 <a href='{vacancy_url}'>Ссылка на вакансию</a>\n\n"
                f"✅ Скопируйте письмо и отправьте работодателю.",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Сгенерировать заново", callback_data=f"cover_letter_{vacancy_id}")],
                    [InlineKeyboardButton(text="🔙 Назад к вакансии", callback_data=f"vacancy_{vacancy_id}")],
                    [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")]
                ])
            )
            
        except Exception as e:
            logger.error(f"Error generating cover letter: {e}")
            await loading_msg.edit_text(
                "❌ Ошибка при генерации письма. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Назад к вакансии", callback_data=f"vacancy_{vacancy_id}")]
                ])
            )
    
    await callback.answer()


@dp.callback_query(F.data == "subscribe")
async def process_subscribe(callback: CallbackQuery):
    """Subscribe user to notifications - update menu"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if not user.resume_text:
            await callback.message.edit_text(
                "❌ Сначала нужно загрузить резюме!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Загрузить резюме", callback_data="update_resume")]
                ])
            )
            await callback.answer()
            return
        
        user.is_subscribed = True
        await session.commit()
        
        # Parse skills from JSON string
        import json
        try:
            skills = json.loads(user.skills.replace("'", '"')) if user.skills else []
            skills_str = ", ".join(skills[:5]) if skills else "Не указаны"
        except:
            skills_str = "Не указаны"
        
        # Update message text and keyboard with new subscription status
        await callback.message.edit_text(
            f"🏠 Главное меню\n\n"
            f"📊 <b>Данные из резюме:</b>\n"
            f"💼 Желаемая позиция: {user.desired_position or 'Не указана'}\n"
            f"📍 Локация: {user.location or 'Не указана'}\n"
            f"💰 Ожидаемая зарплата: {user.salary_expectation or 'Не указана'}\n"
            f"📈 Опыт работы: {user.experience_years or 'Не указан'} лет\n"
            f"🛠 Ключевые навыки: {skills_str}\n\n"
            f"🔔 Рассылка: {'✅ Активна' if user.is_subscribed else '❌ Не активна'}\n\n"
            f"Выбери действие:",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(True)
        )
    
    await callback.answer("✅ Подписка активирована")


@dp.callback_query(F.data == "unsubscribe")
async def process_unsubscribe(callback: CallbackQuery):
    """Unsubscribe user from notifications - update menu"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        user.is_subscribed = False
        await session.commit()
        
        # Parse skills from JSON string
        import json
        try:
            skills = json.loads(user.skills.replace("'", '"')) if user.skills else []
            skills_str = ", ".join(skills[:5]) if skills else "Не указаны"
        except:
            skills_str = "Не указаны"
        
        # Update message text and keyboard with new subscription status
        await callback.message.edit_text(
            f"🏠 Главное меню\n\n"
            f"📊 <b>Данные из резюме:</b>\n"
            f"💼 Желаемая позиция: {user.desired_position or 'Не указана'}\n"
            f"📍 Локация: {user.location or 'Не указана'}\n"
            f"💰 Ожидаемая зарплата: {user.salary_expectation or 'Не указана'}\n"
            f"📈 Опыт работы: {user.experience_years or 'Не указан'} лет\n"
            f"🛠 Ключевые навыки: {skills_str}\n\n"
            f"🔔 Рассылка: {'✅ Активна' if user.is_subscribed else '❌ Не активна'}\n\n"
            f"Выбери действие:",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(False)
        )
    
    await callback.answer("🔕 Подписка отключена")


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    """Return to main menu"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        # Parse skills from JSON string
        import json
        try:
            skills = json.loads(user.skills.replace("'", '"')) if user.skills else []
            skills_str = ", ".join(skills[:5]) if skills else "Не указаны"
        except:
            skills_str = "Не указаны"
        
        await callback.message.edit_text(
            f"🏠 Главное меню\n\n"
            f"📊 <b>Данные из резюме:</b>\n"
            f"💼 Желаемая позиция: {user.desired_position or 'Не указана'}\n"
            f"📍 Локация: {user.location or 'Не указана'}\n"
            f"💰 Ожидаемая зарплата: {user.salary_expectation or 'Не указана'}\n"
            f"📈 Опыт работы: {user.experience_years or 'Не указан'} лет\n"
            f"🛠 Ключевые навыки: {skills_str}\n\n"
            f"🔔 Рассылка: {'✅ Активна' if user.is_subscribed else '❌ Не активна'}\n\n"
            f"Выбери действие:",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(user.is_subscribed)
        )
    
    await callback.answer()


@dp.callback_query(F.data == "help")
async def show_help(callback: CallbackQuery):
    """Show help message"""
    help_text = (
        "ℹ️ <b>Помощь по боту</b>\n\n"
        "<b>🔍 Найти вакансии</b> - Поиск 150 последних вакансий по твоему резюме\n\n"
        "<b>🔔 Подписаться на рассылку</b> - Каждые 20 минут проверка новых вакансий\n\n"
        "<b>🔕 Отписаться от рассылки</b> - Отключить уведомления\n\n"
        "<b>📄 Обновить резюме</b> - Загрузить новое резюме для анализа\n\n"
        "<b>Как это работает?</b>\n"
        "1. Загрузи PDF с резюме\n"
        "2. AI проанализирует твои навыки и опыт\n"
        "3. Бот будет искать подходящие вакансии\n"
        "4. Получай уведомления о новых вакансиях"
    )
    
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        await callback.message.edit_text(
            help_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
            ])
        )
    
    await callback.answer()


@dp.callback_query(F.data.startswith("show_new_vacancies_"))
async def show_new_vacancies_from_notification(callback: CallbackQuery):
    """Show all new vacancies from notification"""
    # Extract user id from callback data
    user_id = int(callback.data.replace("show_new_vacancies_", ""))
    
    # Get stored vacancies for this user
    vacancies = user_new_vacancies.get(user_id, [])
    
    if not vacancies:
        await callback.message.edit_text(
            "❌ Список вакансий устарел. Попробуй найти вакансии вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти вакансии", callback_data="search_vacancies")],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")]
            ])
        )
        await callback.answer()
        return
    
    # Prepare vacancies list for keyboard
    vac_list = [(vac, str(vac.get("id")), score) for vac, score in vacancies]
    
    await callback.message.edit_text(
        f"📋 <b>Новые вакансии ({len(vacancies)}):</b>\n\n"
        f"Нажми на название для деталей:",
        parse_mode="HTML",
        reply_markup=get_vacancies_keyboard(vac_list)
    )
    
    await callback.answer()


@dp.callback_query(F.data.startswith("vac_page_"))
async def handle_pagination(callback: CallbackQuery):
    """Handle pagination buttons"""
    # Parse callback data: vac_page_{page}_{sort_by}
    parts = callback.data.split("_")
    page = int(parts[2])
    sort_by = parts[3] if len(parts) > 3 else 'date'
    
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        # Update page in storage
        if user.id in user_vacancy_pages:
            user_vacancy_pages[user.id]['page'] = page
            user_vacancy_pages[user.id]['sort_by'] = sort_by
        
        await show_vacancies_page(callback.message, user.id, page, sort_by)
    
    await callback.answer()


@dp.callback_query(F.data == "vac_sort_score")
async def sort_by_score(callback: CallbackQuery):
    """Sort vacancies by score"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if user.id in user_vacancy_pages:
            user_vacancy_pages[user.id]['sort_by'] = 'score'
            user_vacancy_pages[user.id]['page'] = 0
            await show_vacancies_page(callback.message, user.id, 0, 'score')
        else:
            await callback.answer("❌ Список вакансий устарел")
            return
    
    await callback.answer()


@dp.callback_query(F.data == "vac_sort_date")
async def sort_by_date(callback: CallbackQuery):
    """Sort vacancies by date"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if user.id in user_vacancy_pages:
            user_vacancy_pages[user.id]['sort_by'] = 'date'
            user_vacancy_pages[user.id]['page'] = 0
            await show_vacancies_page(callback.message, user.id, 0, 'date')
        else:
            await callback.answer("❌ Список вакансий устарел")
            return
    
    await callback.answer()


@dp.callback_query(F.data == "show_top_matches_menu")
async def show_top_matches_from_menu(callback: CallbackQuery):
    """Show top matches when clicked from main menu"""
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user)
        
        if not user.resume_text:
            await callback.message.edit_text(
                "❌ Сначала нужно загрузить резюме!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Загрузить резюме", callback_data="update_resume")],
                    [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")]
                ])
            )
            await callback.answer()
            return
        
        # Check if we have stored vacancies
        if user.id not in user_vacancy_pages or not user_vacancy_pages[user.id].get('vacancies'):
            await callback.message.edit_text(
                "❌ Сначала нужно выполнить поиск вакансий!\n\n"
                "Нажми «🔍 Найти вакансии» чтобы получить список.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Найти вакансии", callback_data="search_vacancies")],
                    [InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")]
                ])
            )
            await callback.answer()
            return
        
        # Show first page sorted by score
        user_vacancy_pages[user.id]['sort_by'] = 'score'
        user_vacancy_pages[user.id]['page'] = 0
        await show_vacancies_page(callback.message, user.id, 0, 'score')
    
    await callback.answer()


# Main entry point
async def main():
    """Start the bot"""
    # Initialize database
    await init_db()
    
    # Start scheduler
    await start_scheduler(bot)
    
    logger.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped!")
