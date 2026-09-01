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
    MENU_CYCLE,
    DAY_NAMES,
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
# На Railway подключите Volume и укажите DATA_DIR=/data,
# иначе список подписчиков будет теряться при каждом передеплое.
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

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
    """Журнал дня.

    naps — список снов: [{"start": "ЧЧ:ММ" | None, "end": "ЧЧ:ММ" | None}, ...]
    asleep — сейчас спит (последний сон начат, но не закрыт)
    shift — на сколько минут сдвинут день относительно базового расписания
    """
    return {
        "day": None,
        "morning_wake": None,
        "naps": [],
        "asleep": False,
        "shift": 0,
    }


def get_state(chat_id: str, subs: dict) -> dict:
    prefs = subs.setdefault(chat_id, {"muted": False})
    state = prefs.setdefault("state", default_state())
    today = date.today().isoformat()
    if state.get("day") != today:
        prefs["state"] = default_state()
        prefs["state"]["day"] = today
        state = prefs["state"]
    # миграция со старого формата
    state.setdefault("morning_wake", None)
    state.setdefault("naps", [])
    state.setdefault("asleep", False)
    state.setdefault("shift", 0)
    return state


def has_personal_schedule(chat_id: str, subs: dict | None = None) -> bool:
    """У чата есть свой пересчитанный день на сегодня?"""
    subs = subs if subs is not None else load_subscribers()
    prefs = subs.get(str(chat_id), {})
    state = prefs.get("state") or {}
    if state.get("day") != date.today().isoformat():
        return False
    return bool(state.get("morning_wake") or state.get("naps"))


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------


def week_index(moment: datetime) -> int:
    """Номер рациона 0–3 по календарной неделе. Меняется автоматически."""
    return (moment.isocalendar().week - 1) % len(MENU_CYCLE)


def menu_for(moment: datetime) -> dict:
    return MENU_CYCLE[week_index(moment)]["days"][moment.weekday()]


def day_name(moment: datetime) -> str:
    return DAY_NAMES[moment.weekday()]


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
    cycle = MENU_CYCLE[week_index(moment)]
    menu = menu_for(moment)
    header = (
        f"*{day_name(moment)}, {moment.strftime('%d.%m')}*\n"
        f"_{cycle['name']} · неделя {week_index(moment) + 1} из {len(MENU_CYCLE)}_"
    )
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
        f"На сегодня всё. Завтра — *{day_name(tomorrow)}*, "
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


def fmt_duration(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    if hours and mins:
        return f"{hours} ч {mins:02d} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def apply_shift(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, state: dict, shift: int
) -> int:
    state["shift"] = shift
    return schedule_personal_day(context, chat_id, shift)


def shift_from(anchor_time: str, moment: datetime) -> int:
    """На сколько минут фактический момент отличается от базового."""
    return int(round((moment - at_today(anchor_time)).total_seconds() / 60))


def too_big(shift: int) -> bool:
    return abs(shift) > MAX_SHIFT_MINUTES


def plan_tail(state: dict, now: datetime, count: int = 3) -> str:
    """Короткий список ближайших событий с учётом сдвига."""
    shift = state["shift"]
    menu = menu_for(now)
    rows = []
    for event in SCHEDULE:
        when = at_today(event["time"], shift)
        if when <= now:
            continue
        icon = ICONS.get(event["kind"], "•")
        body = event["text"] or menu.get(event["key"], "")
        label = body.split(" + ")[0] if event["kind"] == "meal" else event["title"]
        rows.append(f"{icon} `{when.strftime('%H:%M')}`  {label}")
        if len(rows) >= count:
            break
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Отметка «уснул»
# ---------------------------------------------------------------------------


def handle_sleep(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, moment: datetime
) -> str:
    subs = load_subscribers()
    state = get_state(str(chat_id), subs)

    if state["asleep"]:
        # Повторное нажатие — считаем уточнением времени засыпания.
        index = len(state["naps"]) - 1
        state["naps"][index]["start"] = moment.strftime("%H:%M")
        shift = shift_from(NAPS[index]["start"], moment)
        if not too_big(shift):
            apply_shift(context, chat_id, state, shift)
        save_subscribers(subs)
        return (
            f"Поправил время засыпания на {moment.strftime('%H:%M')}.\n"
            f"Если малыш уже проснулся — нажмите «{BTN_WAKE}»."
        )

    index = len(state["naps"])
    if index >= len(NAPS):
        save_subscribers(subs)
        return "Все сны на сегодня уже отмечены. Спокойной ночи."

    nap = NAPS[index]
    shift = shift_from(nap["start"], moment)

    if too_big(shift):
        save_subscribers(subs)
        return (
            f"Засыпание в {moment.strftime('%H:%M')} отличается от обычного "
            f"на {fmt_duration(abs(shift))} — это слишком много, чтобы "
            "автоматически двигать день. Оставляю прежнее расписание.\n\n"
            "Если время верное, начните день заново командой /reset."
        )

    state["naps"].append({"start": moment.strftime("%H:%M"), "end": None})
    state["asleep"] = True
    count = apply_shift(context, chat_id, state, shift)
    save_subscribers(subs)

    if index == len(NAPS) - 1:
        return (
            f"🌙 Отбой в {moment.strftime('%H:%M')}. Ночь пошла.\n\n"
            f"Утром нажмите «{BTN_WAKE}» — пересчитаю завтрашний день "
            "от фактического подъёма."
        )

    duration = NAP_DURATIONS[index]
    expected = moment + timedelta(minutes=duration)
    note = (
        "Ровно по расписанию."
        if shift == 0
        else f"Это {human_delta(shift)} обычного — сдвинул остаток дня."
    )
    return (
        f"😴 *{nap['name']}*: уснул в {moment.strftime('%H:%M')}.\n"
        f"{note}\n\n"
        f"Ожидаемое пробуждение около *{expected.strftime('%H:%M')}* "
        f"({duration} мин).\n\n"
        f"Проснётся — нажмите «{BTN_WAKE}», даже если раньше или позже.\n"
        f"_Уведомлений на остаток дня: {count}_"
    )


# ---------------------------------------------------------------------------
# Отметка «проснулся»
# ---------------------------------------------------------------------------


def handle_wake(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, moment: datetime
) -> str:
    subs = load_subscribers()
    state = get_state(str(chat_id), subs)

    # --- 1. Спал: закрываем текущий сон -----------------------------------
    if state["asleep"]:
        index = len(state["naps"]) - 1
        nap = NAPS[index]

        if index == len(NAPS) - 1:
            # Просыпание после ночи — это начало нового дня.
            state.update(default_state())
            state["day"] = date.today().isoformat()
            return _morning(context, chat_id, state, subs, moment)

        shift = shift_from(nap["end"], moment)
        if too_big(shift):
            save_subscribers(subs)
            return (
                f"Пробуждение в {moment.strftime('%H:%M')} расходится "
                f"с обычным на {fmt_duration(abs(shift))}. Слишком много, "
                "чтобы двигать день автоматически — оставляю как было."
            )

        state["naps"][index]["end"] = moment.strftime("%H:%M")
        state["asleep"] = False
        count = apply_shift(context, chat_id, state, shift)
        save_subscribers(subs)

        started = state["naps"][index]["start"]
        if started:
            slept = int(
                (moment - at_today(started)).total_seconds() // 60
            )
            slept_line = f"Проспал {fmt_duration(slept)}."
        else:
            slept_line = "Время засыпания не отмечалось."

        return _after_wake(state, moment, f"☀️ *{nap['name']}* закончен.", slept_line, count)

    # --- 2. Не спал и утро ещё не отмечено: это утренний подъём -----------
    if not state["morning_wake"]:
        return _morning(context, chat_id, state, subs, moment)

    # --- 3. Не спал, но утро уже было: кнопку «Уснул» пропустили ----------
    index = len(state["naps"])
    if index >= len(NAPS):
        save_subscribers(subs)
        return (
            "Все сны на сегодня уже отмечены. Если это ночное пробуждение — "
            "покормите и укладывайте обратно, пересчитывать день не нужно."
        )

    nap = NAPS[index]
    shift = shift_from(nap["end"], moment)
    if too_big(shift):
        save_subscribers(subs)
        return (
            f"Похоже, кнопку «{BTN_ASLEEP}» пропустили, а {nap['name'].lower()} "
            f"должен был закончиться около {nap['end']}. Разница с "
            f"{moment.strftime('%H:%M')} слишком велика, чтобы гадать.\n\n"
            "Укажите время вручную: `/wake 13:20` или начните заново /reset."
        )

    state["naps"].append({"start": None, "end": moment.strftime("%H:%M")})
    count = apply_shift(context, chat_id, state, shift)
    save_subscribers(subs)
    return _after_wake(
        state,
        moment,
        f"☀️ Записал пробуждение — *{nap['name']}* завершён.",
        f"Засыпание не отмечалось — взял базовую длительность.",
        count,
    )


def _morning(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    state: dict,
    subs: dict,
    moment: datetime,
) -> str:
    shift = shift_from(WAKE_ANCHORS[0], moment)
    if too_big(shift):
        save_subscribers(subs)
        return (
            f"Подъём в {moment.strftime('%H:%M')} отличается от обычного "
            f"на {fmt_duration(abs(shift))}. Это слишком много для "
            "автоматического пересчёта — оставляю базовое расписание."
        )

    state["morning_wake"] = moment.strftime("%H:%M")
    state["naps"] = []
    state["asleep"] = False
    count = apply_shift(context, chat_id, state, shift)
    save_subscribers(subs)

    note = (
        "Ровно по расписанию."
        if shift == 0
        else f"Это {human_delta(shift)} обычного — пересчитал весь день."
    )
    return _after_wake(
        state, moment, f"🌅 Доброе утро. Подъём в {moment.strftime('%H:%M')}.", note, count
    )


def _after_wake(
    state: dict, moment: datetime, head: str, note: str, count: int
) -> str:
    shift = state["shift"]
    index = len(state["naps"])

    lines = [head, note, ""]

    if index < len(NAPS):
        nap = NAPS[index]
        sleep_at = at_today(nap["start"], shift)
        feed_key = {0: "nap1_prep", 1: "nap2_prep", 2: "night_prep"}[index]
        feed_event = next(e for e in SCHEDULE if e["key"] == feed_key)
        feed_at = at_today(feed_event["time"], shift)
        window = int((sleep_at - moment).total_seconds() // 60)
        lines += [
            f"😴 Следующий сон: *{nap['name']}* около "
            f"*{sleep_at.strftime('%H:%M')}*",
            f"🤱 Грудь и укладывание в {feed_at.strftime('%H:%M')}",
            f"⏱ Окно бодрствования: {fmt_duration(window)}",
            "",
        ]

    tail = plan_tail(state, moment)
    if tail:
        lines += ["Ближайшее:", tail, ""]
    lines.append(f"_Уведомлений на остаток дня: {count}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Обработчики кнопок и ручного ввода времени
# ---------------------------------------------------------------------------


async def on_wake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = handle_wake(context, update.effective_chat.id, datetime.now(TZ))
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def on_asleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = handle_sleep(context, update.effective_chat.id, datetime.now(TZ))
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


def parse_manual_time(args: list[str]) -> datetime | None:
    if not args:
        return None
    raw = args[0].replace(".", ":").replace("-", ":")
    try:
        hour, minute = raw.split(":")
        return at_today(f"{int(hour):02d}:{int(minute):02d}")
    except (ValueError, IndexError):
        return None


async def cmd_wake_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    moment = parse_manual_time(context.args)
    if not moment:
        await update.message.reply_text(
            "Укажите время: `/wake 09:05`", parse_mode=ParseMode.MARKDOWN,
            reply_markup=KEYBOARD,
        )
        return
    text = handle_wake(context, update.effective_chat.id, moment)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def cmd_sleep_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    moment = parse_manual_time(context.args)
    if not moment:
        await update.message.reply_text(
            "Укажите время: `/sleep 12:10`", parse_mode=ParseMode.MARKDOWN,
            reply_markup=KEYBOARD,
        )
        return
    text = handle_sleep(context, update.effective_chat.id, moment)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = load_subscribers()
    state = get_state(str(update.effective_chat.id), subs)
    save_subscribers(subs)

    lines = ["*Как прошёл день*", ""]
    lines.append(
        f"🌅 Подъём: {state['morning_wake'] or 'не отмечен'}"
    )
    for index, nap in enumerate(state["naps"]):
        name = NAPS[index]["name"]
        start = nap["start"] or "—"
        end = nap["end"] or "ещё спит"
        if nap["start"] and nap["end"]:
            slept = int(
                (at_today(nap["end"]) - at_today(nap["start"])).total_seconds() // 60
            )
            lines.append(f"😴 {name}: {start} – {end} ({fmt_duration(slept)})")
        else:
            lines.append(f"😴 {name}: {start} – {end}")

    remaining = len(NAPS) - len(state["naps"])
    if remaining > 0 and not state["asleep"]:
        lines.append(f"Впереди снов: {remaining}")

    shift = state["shift"]
    lines += [
        "",
        f"Сдвиг дня: {human_delta(shift) if shift else 'нет, идём по расписанию'}",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
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
    f"«{BTN_WAKE}» — отметить пробуждение: утреннее или после любого сна.\n"
    f"«{BTN_ASLEEP}» — отметить засыпание перед любым сном.\n"
    "Бот сам понимает, какой это по счёту сон, и после каждого нажатия "
    "пересчитывает остаток дня.\n\n"
    "/status — как идёт день: подъём, сны, текущий сдвиг\n"
    "/wake 09:05 — отметить подъём задним числом\n"
    "/sleep 12:10 — отметить засыпание задним числом\n\n"
    "/today — меню и режим на сегодня\n"
    "/tomorrow — на завтра\n"
    "/next — ближайшее событие\n"
    "/schedule — режим дня\n"
    "/week — меню на текущую неделю\n"
    "/week 2 — меню второго рациона\n"
    "/cycle — какой рацион идёт сейчас\n"
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
    """/week — текущий рацион, /week 3 — третий рацион цикла."""
    now = datetime.now(TZ)
    index = week_index(now)
    if context.args:
        try:
            requested = int(context.args[0]) - 1
        except ValueError:
            requested = -1
        if 0 <= requested < len(MENU_CYCLE):
            index = requested
        else:
            await update.message.reply_text(
                f"Рационов всего {len(MENU_CYCLE)}. Напишите, например, `/week 2`.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=KEYBOARD,
            )
            return

    cycle = MENU_CYCLE[index]
    mark = " (сейчас идёт)" if index == week_index(now) else ""
    chunks = [f"*{cycle['name']}* — неделя {index + 1} из {len(MENU_CYCLE)}{mark}"]
    for day in range(7):
        menu = cycle["days"][day]
        chunks.append(
            f"*{DAY_NAMES[day]}*\n"
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


async def cmd_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает все рационы цикла и какой идёт сейчас."""
    now = datetime.now(TZ)
    current = week_index(now)
    lines = ["*Цикл рационов*", ""]
    for index, cycle in MENU_CYCLE.items():
        mark = "▶️" if index == current else "  "
        lines.append(f"{mark} *{index + 1}.* {cycle['name']}")
    lines += [
        "",
        f"Сейчас идёт рацион {current + 1}. "
        "Меню переключается само в понедельник.",
        "",
        "Посмотреть любой: `/week 2`",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=KEYBOARD
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
            BotCommand("cycle", "Какой рацион идёт"),
            BotCommand("recipe", "Рецепты"),
            BotCommand("status", "Как идёт день"),
            BotCommand("wake", "Подъём задним числом"),
            BotCommand("sleep", "Засыпание задним числом"),
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
    app.add_handler(CommandHandler("cycle", cmd_cycle))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("wake", cmd_wake_at))
    app.add_handler(CommandHandler("sleep", cmd_sleep_at))
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
