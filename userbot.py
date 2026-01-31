import argparse
import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from telethon import TelegramClient
from telethon.tl.custom.message import Message

TARGET_PATTERN = re.compile(
    r"(https?://t\.me/[A-Za-z0-9_]+|@[A-Za-z0-9_]+|-?\d{5,})"
)


@dataclass
class State:
    last_forwarded_id: int = 0


def normalize_chat(chat: str) -> str:
    if chat.startswith("https://t.me/"):
        return chat.replace("https://t.me/", "", 1)
    if chat.startswith("@"):
        return chat[1:]
    return chat


def parse_targets(text: str) -> List[str]:
    targets: List[str] = []
    for match in TARGET_PATTERN.findall(text or ""):
        normalized = normalize_chat(match.strip())
        if normalized and normalized not in targets:
            targets.append(normalized)
    return targets


def load_state(state_file: Path) -> State:
    if not state_file.exists():
        return State()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return State()
    return State(last_forwarded_id=int(data.get("last_forwarded_id", 0)))


def save_state(state_file: Path, state: State) -> None:
    state_file.write_text(
        json.dumps({"last_forwarded_id": state.last_forwarded_id}, ensure_ascii=False),
        encoding="utf-8",
    )


async def resolve_targets(
    client: TelegramClient,
    group: str,
    message_id: Optional[int],
) -> List[str]:
    if message_id:
        message = await client.get_messages(group, ids=message_id)
        return parse_targets(message.message)

    async for message in client.iter_messages(group, limit=50):
        targets = parse_targets(message.message)
        if targets:
            return targets

    return []


def collect_album(messages: List[Message], main_message: Message) -> List[Message]:
    if main_message.grouped_id is None:
        return [main_message]
    grouped = [
        msg for msg in messages if msg.grouped_id == main_message.grouped_id
    ]
    return sorted(grouped, key=lambda item: item.id)


async def fetch_latest_post(
    client: TelegramClient,
    source_channel: str,
    fixed_message_id: Optional[int],
) -> List[Message]:
    if fixed_message_id:
        message = await client.get_messages(source_channel, ids=fixed_message_id)
        return [message] if message else []

    recent_messages = await client.get_messages(source_channel, limit=10)
    if not recent_messages:
        return []
    return collect_album(list(recent_messages), recent_messages[0])


async def forward_to_targets(
    client: TelegramClient,
    targets: Iterable[str],
    messages: List[Message],
    send_delay: float,
) -> None:
    for target in targets:
        try:
            await client.forward_messages(target, messages)
            logging.info("Отправлено в %s", target)
        except Exception as exc:  # noqa: BLE001 - логируем и продолжаем
            logging.error("Ошибка отправки в %s: %s", target, exc)
        await asyncio.sleep(send_delay)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Userbot на Telethon, который пересылает посты из канала и "
            "рассылает их по чатам, список которых хранится в группе."
        )
    )
    parser.add_argument(
        "--source-channel",
        help="Канал-источник (username, id или ссылка)",
    )
    parser.add_argument(
        "--source-message-id",
        type=int,
        help="ID сообщения в канале, если нужно отправлять фиксированный пост",
    )
    parser.add_argument(
        "--targets-group",
        help="Группа, где хранится список чатов для рассылки",
    )
    parser.add_argument(
        "--targets-message-id",
        type=int,
        help="ID сообщения в группе, где перечислены чаты",
    )
    parser.add_argument(
        "--send-delay",
        type=float,
        default=None,
        help="Пауза между пересылками по чатам (сек)",
    )
    parser.add_argument(
        "--interval-min",
        type=int,
        default=None,
        help="Минимальная пауза между циклами рассылки (сек)",
    )
    parser.add_argument(
        "--interval-max",
        type=int,
        default=None,
        help="Максимальная пауза между циклами рассылки (сек)",
    )
    parser.add_argument(
        "--state-file",
        help="Путь к файлу состояния с последним отправленным ID",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = build_parser()
    args = parser.parse_args()

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Нужно указать API_ID и API_HASH в переменных окружения")

    source_channel = args.source_channel or os.getenv("SOURCE_CHANNEL")
    if not source_channel:
        raise SystemExit("Нужно указать SOURCE_CHANNEL или --source-channel")

    targets_group = args.targets_group or os.getenv("TARGETS_GROUP")
    if not targets_group:
        raise SystemExit("Нужно указать TARGETS_GROUP или --targets-group")

    source_message_id = args.source_message_id or os.getenv("SOURCE_MESSAGE_ID")
    if source_message_id is not None:
        source_message_id = int(source_message_id)

    targets_message_id = args.targets_message_id or os.getenv("TARGETS_MESSAGE_ID")
    if targets_message_id is not None:
        targets_message_id = int(targets_message_id)

    session_name = os.getenv("SESSION_NAME", "userbot")
    send_delay = args.send_delay
    if send_delay is None:
        send_delay = float(os.getenv("SEND_DELAY", "1.0"))

    interval_min = args.interval_min
    if interval_min is None:
        interval_min = int(os.getenv("INTERVAL_MIN", "600"))

    interval_max = args.interval_max
    if interval_max is None:
        interval_max = int(os.getenv("INTERVAL_MAX", "1200"))

    if interval_min > interval_max:
        raise SystemExit("INTERVAL_MIN не может быть больше INTERVAL_MAX")

    state_file = Path(args.state_file or os.getenv("STATE_FILE", ".state.json"))
    state = load_state(state_file)

    async def runner() -> None:
        async with TelegramClient(session_name, int(api_id), api_hash) as client:
            while True:
                targets = await resolve_targets(client, targets_group, targets_message_id)
                if not targets:
                    logging.warning("Не удалось найти список чатов в группе %s", targets_group)
                else:
                    messages = await fetch_latest_post(
                        client, source_channel, source_message_id
                    )
                    if not messages:
                        logging.warning("Не удалось получить пост из %s", source_channel)
                    else:
                        latest_id = max(message.id for message in messages)
                        if latest_id > state.last_forwarded_id:
                            await forward_to_targets(
                                client, targets, messages, send_delay
                            )
                            state.last_forwarded_id = latest_id
                            save_state(state_file, state)
                        else:
                            logging.info("Новых постов нет, последний ID %s", latest_id)

                sleep_for = random.randint(interval_min, interval_max)
                logging.info("Следующая проверка через %s сек", sleep_for)
                await asyncio.sleep(sleep_for)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
