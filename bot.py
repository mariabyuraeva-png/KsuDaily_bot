"""Telegram-бот: рацион на день, режим и подготовка ко сну.

Запуск:
    export BOT_TOKEN="123456:ABC..."
    python bot.py

Опционально:
    export TZ_NAME="Europe/Amsterdam"   # часовой пояс расписания
"""

import json
import logging
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from menu_data import (
    DAILY_DIGEST_TIME,
    ICONS,
    MAX_SHIFT_MINUTES,
    NAP_DURATIONS,
    NAPS,
    PREP_LEAD_MINUTES,
    RECIPES,
    SCHEDULE,
    SLEEP_PREP,
    WAKE_ANCHORS,
    WEEK_MENU,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("baby-bot")

TOKEN = os.environ.get("BOT_TOKEN")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Amsterdam"))
SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.json"

BTN_WAKE = "🌅 Проснулся"
BTN_ASLEEP = "😴 Уснул"
BTN_TODAY = "📋 Сегодня"
BTN_NEXT = "⏭ Дальше"

KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_WAKE), KeyboardButton(BTN_ASLEEP)],
        [KeyboardButton(BTN_TODAY), KeyboardButton(BTN_NEXT)],
    ],
    resize_keyboard=True,
)


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------


def load_subscribers() -> dict:
    if not SUBSCRIBERS_FILE.exists():
        return {}
    try:
        return json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Не удалось прочитать %s: %s", SUBSCRIBERS_FILE, exc)
        return {}


def save_subscribers(data: dict) -> None:
    tmp = SUBSCRIBERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SUBSCRIBERS_FILE)


def default_state() -> dict:
    """stage — сколько снов уже позади (0, 1, 2), 3 — ушли на ночь."""
    return {
        "day": None,
        "stage": 0,
        "shift": 0,
        "last_wake": None,
        "asleep_since": None,
    }


def get_state(chat_id: str, subs: dict) -> dict:
    prefs = subs.setdefault(chat_id, {"muted": False})
    state = prefs.setdefault("state", default_state())
    today = date.today().isoformat()
    if state.get("day") != today:
        prefs["state"] = default_state()
        prefs["state"]["day"] = today
        state = prefs["state"]
    return state


def has_personal_schedule(chat_id: str, subs: dict | None = None) -> bool:
    """У чата есть свой пересчитанный день на сегодня?"""
    subs = subs if subs is not None else load_subscribers()
    prefs = subs.get(str(chat_id), {})
    state = prefs.get("state") or {}
    return state.get("day") == date.today().isoformat() and state.get("last_wake")


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------


def menu_for(moment: datetime) -> dict:
    return WEEK_MENU[moment.weekday()]


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(hour=int(hour), minute=int(minute), tzinfo=TZ)


def at_today(value: str, shift: int = 0) -> datetime:
    """Строку 'ЧЧ:ММ' превращает в сегодняшний момент со сдвигом в минутах."""
    now = datetime.now(TZ)
    base = datetime.combine(now.date(), parse_hhmm(value))
    return base + timedelta(minutes=shift)


def human_delta(minutes: int) -> str:
    if minutes == 0:
        return "минута в минуту"
    sign = "позже" if minutes > 0 else "раньше"
    minutes = abs(minutes)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин {sign}"
    if hours:
        return f"{hours} ч {sign}"
    return f"{mins} мин {sign}"


def event_text(event: dict, menu: dict, shift: int = 0) -> str:
    body = event["text"] or menu.get(event["key"], "")
    icon = ICONS.get(event["kind"], "•")
    when = at_today(event["time"], shift).strftime("%H:%M")
    lines = [f"{icon} *{when} — {event['title']}*"]
    if body:
        lines += ["", body]
    return "\n".join(lines)


def prep_meal_text(event: dict, menu: dict, shift: int = 0) -> str:
    body = event["text"] or menu.get(event["key"], "")
    when = at_today(event["time"], shift).strftime("%H:%M")
    return (
        f"⏳ *Через {PREP_LEAD_MINUTES} мин — {event['title'].lower()} ({when})*"
        f"\n\n{body}\n\n_Пора начинать готовить._"
    )


def prep_sleep_text(event: dict, prep: dict, shift: int = 0) -> str:
    when = at_today(event["time"], shift).strftime("%H:%M")
    return (
        f"🌙 *{prep['title']}*\n\n{prep['text']}\n\n"
        f"_Укладывание в {when}._"
    )


def day_digest(moment: datetime, shift: int = 0) -> str:
    menu = menu_for(moment)
    header = f"*{menu['name']}, {moment.strftime('%d.%m')}*"
    if shift:
        header += f"\n_Сдвиг дня: {human_delta(shift)}_"
    lines = [header, ""]
    for event in SCHEDULE:
        icon = ICONS.get(event["kind"], "•")
        when = at_today(event["time"], shift).strftime("%H:%M")
        body = event["text"] or menu.get(event["key"], "")
        if event["kind"] == "meal" and body:
            lines.append(f"{icon} `{when}`  *{event['title']}*")
            lines.append(f"       {body}")
        else:
            lines.append(f"{icon} `{when}`  {event['title']}")
            if body:
                lines.append(f"       _{body}_")
        lines.append("")
    lines.append("_Грудь по требованию ночью. Порции ориентировочные._")
    return "\n".join(lines)


def schedule_text(shift: int = 0) -> str:
    lines = ["*Режим дня*", ""]
    for event in SCHEDULE:
        icon = ICONS.get(event["kind"], "•")
        when = at_today(event["time"], shift).strftime("%H:%M")
        lines.append(f"{icon} `{when}`  {event['title']}")
    lines.append("")
    for nap in NAPS:
        start = at_today(nap["start"], shift).strftime("%H:%M")
        end = at_today(nap["end"], shift).strftime("%H:%M")
        lines.append(f"😴 {nap['name']}: {start} – {end}")
    return "\n".join(lines)


def next_event_text(now: datetime, shift: int = 0) -> str:
    for event in SCHEDULE:
        planned = at_today(event["time"], shift)
        if planned > now:
            minutes = int((planned - now).total_seconds() // 60)
            hours, mins = divmod(minutes, 60)
            left = f"{hours} ч {mins} мин" if hours else f"{mins} мин"
            return event_text(event, menu_for(now), shift) + f"\n\n_Через {left}_"
    tomorrow = now + timedelta(days=1)
    return (
        f"На сегодня всё. Завтра — *{menu_for(tomorrow)['name']}*, "
        f"подъём около {SCHEDULE[0]['time']}."
    )


# ---------------------------------------------------------------------------
# Персональное расписание
# ---------------------------------------------------------------------------


def build_timeline(shift: int) -> list[dict]:
    """Все события дня со сдвигом: сами события, готовка и подготовка ко сну."""
    items: list[dict] = []
    for event in SCHEDULE:
        when = at_today(event["time"], shift)
        items.append({"when": when, "event": event, "type": "main"})

        if event["kind"] == "meal" and PREP_LEAD_MINUTES > 0:
            items.append(
                {
                    "when": when - timedelta(minutes=PREP_LEAD_MINUTES),
                    "event": event,
                    "type": "prep_meal",
                }
            )
        if event["kind"] == "sleep":
            for prep in SLEEP_PREP:
                items.append(
                    {
                        "when": when - timedelta(minutes=prep["lead"]),
                        "event": event,
                        "type": "prep_sleep",
                        "prep": prep,
                    }
                )
    items.sort(key=lambda item: item["when"])
    return items


def clear_personal_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    prefix = f"p{chat_id}-"
    for job in context.job_queue.jobs():
        if job.name and job.name.startswith(prefix):
            job.schedule_removal()


def schedule_personal_day(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, shift: int
) -> int:
    """Ставит персональные уведомления на остаток дня. Возвращает их число."""
    clear_personal_jobs(context, chat_id)
    now = datetime.now(TZ)
    planned = 0
    for index, item in enumerate(build_timeline(shift)):
        if item["when"] <= now:
            continue
        context.job_queue.run_once(
            job_personal,
            when=item["when"],
            data={"chat_id": chat_id, "item": item, "shift": shift},
            name=f"p{chat_id}-{index}",
        )
        planned += 1
    return planned


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------


async def send_to_default_chats(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Рассылка тем, кто сегодня не пересчитывал день кнопкой."""
    subs = load_subscribers()
    dead = []
    for chat_id, prefs in subs.items():
        if prefs.get("muted") or has_personal_schedule(chat_id, subs):
            continue
        if not await safe_send(context, int(chat_id), text):
            dead.append(chat_id)
    if dead:
        for chat_id in dead:
            subs.pop(chat_id, None)
        save_subscribers(subs)


async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> bool:
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN
        )
        return True
    except Exception as exc:
        log.warning("Не отправлено в %s: %s", chat_id, exc)
        return "bot was blocked" not in str(exc) and "chat not found" not in str(exc)


def render_item(item: dict, shift: int, moment: datetime) -> str:
    menu = menu_for(moment)
    if item["type"] == "main":
        return event_text(item["event"], menu, shift)
    if item["type"] == "prep_meal":
        return prep_meal_text(item["event"], menu, shift)
    return prep_sleep_text(item["event"], item["prep"], shift)


# ---------------------------------------------------------------------------
# Задания
# ---------------------------------------------------------------------------


async def job_personal(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    subs = load_subscribers()
    if subs.get(str(chat_id), {}).get("muted"):
        return
    text = render_item(data["item"], data["shift"], datetime.now(TZ))
    await safe_send(context, chat_id, text)


async def job_default(context: ContextTypes.DEFAULT_TYPE) -> None:
    item = context.job.data
    await send_to_default_chats(context, render_item(item, 0, datetime.now(TZ)))


async def job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Доброе утро. План на день:\n\n"
        + day_digest(datetime.now(TZ))
        + f"\n\nКогда малыш проснётся — нажмите «{BTN_WAKE}», "
        "и я пересчитаю день под реальное время."
    )
    await send_to_default_chats(context, text)


def register_jobs(app: Application) -> None:
    queue = app.job_queue
    queue.run_daily(job_digest, time=parse_hhmm(DAILY_DIGEST_TIME), name="digest")

    for index, item in enumerate(build_timeline(0)):
        queue.run_daily(
            job_default,
            time=item["when"].timetz(),
            data=item,
            name=f"default-{index}",
        )
    log.info("Базовых заданий: %d", len(queue.jobs()))


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------


async def on_wake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = datetime.now(TZ)
    subs = load_subscribers()
    state = get_state(str(chat_id), subs)

    stage = state.get("stage", 0)
    if stage >= 3:
        await update.message.reply_text(
            "Все три сна на сегодня уже отмечены. Если это ночное пробуждение — "
            "просто покормите и укладывайте обратно, пересчитывать день не нужно.",
            reply_markup=KEYBOARD,
        )
        return

    anchor = at_today(WAKE_ANCHORS[stage])
    shift = int(round((now - anchor).total_seconds() / 60))

    if abs(shift) > MAX_SHIFT_MINUTES:
        await update.message.reply_text(
            f"Сдвиг получается {human_delta(shift)} — это слишком много, "
            "похоже на ошибку или совсем нестандартный день. "
            "Оставляю базовое расписание, пользуйтесь /today и /next.",
            reply_markup=KEYBOARD,
        )
        return

    state["shift"] = shift
    state["last_wake"] = now.strftime("%H:%M")
    state["asleep_since"] = None
    save_subscribers(subs)

    count = schedule_personal_day(context, chat_id, shift)

    if stage == 0:
        head = f"Доброе утро. Подъём в {now.strftime('%H:%M')}."
    else:
        head = f"Проснулся после сна {stage} в {now.strftime('%H:%M')}."

    if shift == 0:
        note = "Ровно по расписанию."
    else:
        note = f"Это {human_delta(shift)} обычного — сдвигаю весь остаток дня."

    nap = NAPS[stage]
    sleep_at = at_today(nap["start"], shift)
    feed_key = {0: "nap1_prep", 1: "nap2_prep", 2: "night_prep"}[stage]
    feed_event = next(e for e in SCHEDULE if e["key"] == feed_key)
    feed_at = at_today(feed_event["time"], shift)
    window = int((sleep_at - now).total_seconds() // 60)
    wh, wm = divmod(max(window, 0), 60)

    lines = [
        head,
        note,
        "",
        f"😴 {nap['name']}: засыпание около *{sleep_at.strftime('%H:%M')}*",
        f"🤱 Грудь и укладывание в {feed_at.strftime('%H:%M')}",
        f"Окно бодрствования: {wh} ч {wm:02d} мин",
        "",
        next_event_text(now, shift),
        "",
        f"_Запланировано уведомлений на остаток дня: {count}_",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def on_asleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    now = datetime.now(TZ)
    subs = load_subscribers()
    state = get_state(str(chat_id), subs)
    stage = state.get("stage", 0)

    if stage >= 3:
        await update.message.reply_text(
            "Ночь уже отмечена. Спокойной ночи.", reply_markup=KEYBOARD
        )
        return

    state["asleep_since"] = now.strftime("%H:%M")
    state["stage"] = stage + 1
    save_subscribers(subs)

    if stage == 2:
        await update.message.reply_text(
            f"Отбой в {now.strftime('%H:%M')}. Ночь пошла.\n\n"
            f"Утром нажмите «{BTN_WAKE}» — пересчитаю завтрашний день.",
            reply_markup=KEYBOARD,
        )
        return

    duration = NAP_DURATIONS[stage]
    expected = now + timedelta(minutes=duration)
    await update.message.reply_text(
        f"😴 Уснул в {now.strftime('%H:%M')}.\n\n"
        f"Ожидаемое пробуждение около *{expected.strftime('%H:%M')}* "
        f"({duration} мин).\n\n"
        f"Когда проснётся — нажмите «{BTN_WAKE}», даже если это будет "
        "раньше или позже: я подстрою остаток дня.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=KEYBOARD,
    )


def current_shift(chat_id: int) -> int:
    subs = load_subscribers()
    prefs = subs.get(str(chat_id), {})
    state = prefs.get("state") or {}
    if state.get("day") == date.today().isoformat():
        return state.get("shift", 0)
    return 0


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

HELP = (
    "*Бот-помощник по режиму и рациону*\n\n"
    f"Кнопка «{BTN_WAKE}» — отметить реальное пробуждение, "
    "утром или после сна. Весь остаток дня сдвинется на ту же разницу.\n"
    f"Кнопка «{BTN_ASLEEP}» — отметить, что уснул. "
    "Подскажу ожидаемое время пробуждения.\n\n"
    "/today — меню и режим на сегодня\n"
    "/tomorrow — на завтра\n"
    "/next — ближайшее событие\n"
    "/schedule — режим дня\n"
    "/week — меню на неделю\n"
    "/recipe — список рецептов\n"
    "/recipe каша — конкретный рецепт\n"
    "/reset — вернуть базовое расписание\n\n"
    "/mute — выключить уведомления\n"
    "/unmute — включить обратно\n"
    "/stop — отписаться"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    subs.setdefault(chat_id, {"muted": False})
    get_state(chat_id, subs)
    save_subscribers(subs)
    await update.message.reply_text(
        "Подписка оформлена. Уведомления придут по расписанию, "
        f"меню на день — в {DAILY_DIGEST_TIME}.\n\n" + HELP,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=KEYBOARD,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shift = current_shift(update.effective_chat.id)
    await update.message.reply_text(
        day_digest(datetime.now(TZ), shift),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=KEYBOARD,
    )


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        day_digest(datetime.now(TZ) + timedelta(days=1)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=KEYBOARD,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shift = current_shift(update.effective_chat.id)
    await update.message.reply_text(
        next_event_text(datetime.now(TZ), shift),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=KEYBOARD,
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shift = current_shift(update.effective_chat.id)
    await update.message.reply_text(
        schedule_text(shift), parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chunks = []
    for index in range(7):
        menu = WEEK_MENU[index]
        chunks.append(
            f"*{menu['name']}*\n"
            f"Завтрак: {menu['breakfast']}\n"
            f"Перекус: {menu['snack']}\n"
            f"Обед: {menu['lunch']}\n"
            f"Полдник: {menu['afternoon']}\n"
            f"Ужин: {menu['dinner']}\n"
            f"2-й ужин: {menu['dinner2']}"
        )
    await update.message.reply_text(
        "\n\n".join(chunks), parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        names = ", ".join(sorted(RECIPES))
        await update.message.reply_text(
            f"Доступные рецепты: {names}\n\nНапример: `/recipe тефтели`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=KEYBOARD,
        )
        return
    query = " ".join(context.args).lower().strip()
    for key, text in RECIPES.items():
        if key.startswith(query) or query in key:
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
            )
            return
    await update.message.reply_text(
        "Такого рецепта нет. Список — просто /recipe без аргументов.",
        reply_markup=KEYBOARD,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    clear_personal_jobs(context, chat_id)
    subs = load_subscribers()
    prefs = subs.setdefault(str(chat_id), {"muted": False})
    prefs["state"] = default_state()
    prefs["state"]["day"] = date.today().isoformat()
    save_subscribers(subs)
    await update.message.reply_text(
        "Вернул базовое расписание на сегодня.", reply_markup=KEYBOARD
    )


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    if chat_id not in subs:
        await update.message.reply_text("Вы не подписаны. Нажмите /start.")
        return
    subs[chat_id]["muted"] = True
    save_subscribers(subs)
    await update.message.reply_text(
        "Уведомления выключены. Команды и кнопки работают. Вернуть — /unmute.",
        reply_markup=KEYBOARD,
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    if chat_id not in subs:
        await update.message.reply_text("Вы не подписаны. Нажмите /start.")
        return
    subs[chat_id]["muted"] = False
    save_subscribers(subs)
    await update.message.reply_text("Уведомления включены.", reply_markup=KEYBOARD)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    clear_personal_jobs(context, chat_id)
    subs = load_subscribers()
    subs.pop(str(chat_id), None)
    save_subscribers(subs)
    await update.message.reply_text("Отписал. Вернуться — /start.")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("today", "Меню на сегодня"),
            BotCommand("tomorrow", "Меню на завтра"),
            BotCommand("next", "Ближайшее событие"),
            BotCommand("schedule", "Режим дня"),
            BotCommand("week", "Меню на неделю"),
            BotCommand("recipe", "Рецепты"),
            BotCommand("reset", "Базовое расписание"),
            BotCommand("mute", "Выключить уведомления"),
            BotCommand("unmute", "Включить уведомления"),
            BotCommand("stop", "Отписаться"),
        ]
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получите токен у @BotFather и выполните:\n"
            '    export BOT_TOKEN="ваш_токен"'
        )

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("stop", cmd_stop))

    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_WAKE}$"), on_wake))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_ASLEEP}$"), on_asleep))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_TODAY}$"), cmd_today))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_NEXT}$"), cmd_next))

    register_jobs(app)

    log.info("Бот запущен. Часовой пояс: %s", TZ)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
