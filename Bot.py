import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
MAX_INPUT_MB = 19
MAX_OUTPUT_MB = 48

if not TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

bot = Application.builder().token(TOKEN).build()
app = FastAPI()

QUALITIES = {
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "2K": 1440,
    "4K": 2160
}


def buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("360p", callback_data="q:360p"),
            InlineKeyboardButton("480p", callback_data="q:480p")
        ],
        [
            InlineKeyboardButton("720p", callback_data="q:720p"),
            InlineKeyboardButton("1080p", callback_data="q:1080p")
        ],
        [
            InlineKeyboardButton("2K", callback_data="q:2K"),
            InlineKeyboardButton("4K", callback_data="q:4K")
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Welcome to KD Video Compressor Bot!\n\n"
        "Send your video to compress.\n"
        "Choose your preferred quality."
    )


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    media = message.video

    if media is None and message.document:
        if (message.document.mime_type or "").startswith("video/"):
            media = message.document

    if media is None:
        return

    size = media.file_size or 0

    if size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text("❌ Maximum upload size is 19 MB.")
        return

    filename = getattr(media, "file_name", None)
    if not filename:
        filename = f"video_{media.file_unique_id}.mp4"

    context.user_data["video"] = {
        "file_id": media.file_id,
        "filename": Path(filename).name
    }

    await message.reply_text(
        f"📹 Video received!\n"
        f"📦 Original size: {size / 1048576:.2f} MB\n\n"
        "Select video quality:",
        reply_markup=buttons()
    )


def get_dimensions(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json", str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )

    stream = json.loads(result.stdout)["streams"][0]

    return int(stream["width"]), int(stream["height"])


async def compress(source, output, target):
    width, height = await asyncio.to_thread(
        get_dimensions, source
    )

    output_height = min(height, target)

    output_width = round(
        width * output_height / height / 2
    ) * 2

    output_height = round(output_height / 2) * 2

    command = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-vf", f"scale={output_width}:{output_height}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output)
    ]

    await asyncio.to_thread(
        subprocess.run,
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=1200
    )


async def choose(update, context):
    query = update.callback_query
    await query.answer()

    video = context.user_data.get("video")

    if not video:
        await query.edit_message_text("Please send a video first.")
        return

    quality = query.data.split(":")[1]

    await query.edit_message_text(
        f"⏳ Compressing video in {quality}..."
    )

    folder = Path(tempfile.mkdtemp())

    try:
        source = folder / video["filename"]

        if source.suffix.lower() not in {
            ".mp4", ".mov", ".mkv", ".webm", ".avi"
        }:
            source = source.with_suffix(".mp4")

        output = folder / f"compressed_{quality}.mp4"

        telegram_file = await context.bot.get_file(
            video["file_id"]
        )

        await telegram_file.download_to_drive(
            custom_path=str(source)
        )

        await compress(
            source,
            output,
            QUALITIES[quality]
        )

        input_size = source.stat().st_size
        output_size = output.stat().st_size

        if output_size > MAX_OUTPUT_MB * 1048576:
            await query.message.reply_text(
                "❌ Output file is too large."
            )
            return

        saved = max(0, input_size - output_size)

        caption = (
            f"✅ Compression Complete!\n"
            f"🎬 Quality: {quality}\n"
            f"📦 Original: {input_size / 1048576:.2f} MB\n"
            f"📦 Compressed: {output_size / 1048576:.2f} MB\n"
            f"📉 Saved: {saved / 1048576:.2f} MB"
        )

        with output.open("rb") as file:
            await query.message.reply_video(
                video=file,
                caption=caption,
                supports_streaming=True
            )

    except Exception as error:
        print(error)

        await query.message.reply_text(
            "❌ Compression failed. Try another video."
        )

    finally:
        context.user_data.pop("video", None)
        shutil.rmtree(folder, ignore_errors=True)


bot.add_handler(CommandHandler("start", start))

bot.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.VIDEO,
        receive
    )
)

bot.add_handler(
    CallbackQueryHandler(
        choose,
        pattern=r"^q:"
    )
)


@app.get("/")
async def home():
    return {"status": "KD Video Compressor Bot Running"}


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.post("/telegram/{secret}")
async def webhook(secret: str, request: Request):

    if secret != TOKEN:
        raise HTTPException(status_code=404)

    data = await request.json()

    update = Update.de_json(
        data,
        bot.bot
    )

    await bot.process_update(update)

    return {"ok": True}


@app.on_event("startup")
async def startup():

    await bot.initialize()
    await bot.start()

    if PUBLIC_URL:
        await bot.bot.set_webhook(
            f"{PUBLIC_URL}/telegram/{TOKEN}"
        )


@app.on_event("shutdown")
async def shutdown():

    if bot.running:
        await bot.stop()

    await bot.shutdown()
