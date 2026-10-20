import asyncio
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from PIL import Image, ImageEnhance, ImageFilter, ImageFont, ImageDraw, ImageOps

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Limit concurrent image jobs to keep the bot responsive
image_semaphore = asyncio.Semaphore(3)

# Ensure Pillow can open large images safely (optional, prevents decompression bombs)
Image.MAX_IMAGE_PIXELS = 50_000_000


# ---------- Helpers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🖼️ **Image Tools Bot**\n\n"
        "Send me a photo or an image file and choose an action:\n"
        "• **Resize** – change width/height\n"
        "• **Compress** – reduce file size (quality)\n"
        "• **Convert** – PNG / JPEG / WebP\n"
        "• **Rotate** – rotate by 90/180/270°\n"
        "• **Grayscale** – black & white\n"
        "• **Blur / Sharpen** – simple filters\n"
        "• **Brightness / Contrast** – enhance\n"
        "• **Watermark** – add custom text\n\n"
        "Commands:\n"
        "/start – show this message\n"
        "/cancel – clear current operation\n\n"
        "⚠️ Telegram compresses photos; for full quality, send as a **file**."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("✅ Session cleared. Send a new image to begin.")


async def _download_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str, str] | None:
    """
    Download an image whether it arrived as a compressed photo or as a document.
    Returns (input_path, tmpdir, original_name) or None on failure.
    """
    message = update.message
    if not message:
        return None

    tmpdir = tempfile.mkdtemp(prefix="imgbot_")
    tmp_path = Path(tmpdir)

    try:
        if message.photo:
            # Compressed photo: take the largest available size
            photo = message.photo[-1]
            file = await photo.get_file()
            input_path = tmp_path / "input.jpg"
            await file.download_to_drive(custom_path=str(input_path))
            original_name = "photo.jpg"
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            doc = message.document
            file = await doc.get_file()
            suffix = Path(doc.file_name or "image").suffix or ".jpg"
            input_path = tmp_path / f"input{suffix}"
            await file.download_to_drive(custom_path=str(input_path))
            original_name = doc.file_name or f"image{suffix}"
        else:
            return None

        return str(input_path), tmpdir, original_name

    except Exception:
        logger.exception("Image download failed")
        await message.reply_text("❌ Could not download that image. Please try again.")
        return None


def _open_image(path: str) -> Image.Image:
    """Open an image and apply EXIF orientation so phone photos are upright."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # critical for phone photos[reference:2]
    # Convert to RGB for filters/JPEG output; keep RGBA for PNG transparency if needed
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def _make_watermark(img: Image.Image, text: str) -> Image.Image:
    """Draw text watermark near the bottom-right corner."""
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Try a common Linux font; fall back to Pillow’s default bitmap font
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", size=max(20, base.width // 30))
    except Exception:
        font = ImageFont.load_default()

    # Calculate text size
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    margin = max(10, base.width // 50)
    x = base.width - tw - margin
    y = base.height - th - margin

    # Semi-transparent dark background for readability
    draw.rectangle([x - 6, y - 6, x + tw + 6, y + th + 6], fill=(0, 0, 0, 120))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 220))

    return Image.alpha_composite(base, overlay).convert("RGB")


# ---------- Initial image handler ----------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with image_semaphore:
        # If we are collecting parameters for an action, route there instead
        if context.user_data.get("awaiting_param"):
            await handle_param_input(update, context)
            return

        result = await _download_image(update, context)
        if not result:
            await update.message.reply_text("Please send a photo or an image file.")
            return

        input_path, tmpdir, original_name = result
        context.user_data.update(
            input_path=input_path,
            tmpdir=tmpdir,
            original_name=original_name,
        )

        # Open once to read dimensions and format for the UI message
        try:
            img = _open_image(input_path)
            w, h = img.size
            fmt = img.format or "unknown"
            info = f"📐 {w}×{h}  ·  {fmt}"
        except Exception:
            logger.exception("Could not open image")
            await update.message.reply_text("❌ That file is not a valid image.")
            return

        keyboard = [
            [
                InlineKeyboardButton("Resize", callback_data="resize"),
                InlineKeyboardButton("Compress", callback_data="compress"),
                InlineKeyboardButton("Convert", callback_data="convert"),
            ],
            [
                InlineKeyboardButton("Rotate 90°", callback_data="rotate_90"),
                InlineKeyboardButton("Grayscale", callback_data="grayscale"),
            ],
            [
                InlineKeyboardButton("Blur", callback_data="blur"),
                InlineKeyboardButton("Sharpen", callback_data="sharpen"),
            ],
            [
                InlineKeyboardButton("Brightness +", callback_data="bright"),
                InlineKeyboardButton("Contrast +", callback_data="contrast"),
            ],
            [InlineKeyboardButton("💧 Watermark", callback_data="watermark")],
        ]
        await update.message.reply_text(
            f"**Image loaded**\n{info}\n\nWhat would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


# ---------- Inline button handler ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    input_path = context.user_data.get("input_path")
    tmpdir = context.user_data.get("tmpdir")
    original_name = context.user_data.get("original_name", "image.jpg")

    if not input_path or not os.path.exists(input_path):
        await query.edit_message_text("❌ No image found. Please send an image first.")
        return

    action = query.data
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix or ".jpg"

    # Actions that need user text input
    if action == "resize":
        context.user_data["awaiting_param"] = "resize"
        await query.edit_message_text(
            "📏 Send the new size as `WIDTHxHEIGHT` (e.g., `800x600`).\n"
            "Use `/cancel` to abort."
        )
        return

    if action == "watermark":
        context.user_data["awaiting_param"] = "watermark"
        await query.edit_message_text(
            "💧 Send the watermark text you want to appear on the image."
        )
        return

    # Immediate actions — process and reply with the resulting image
    try:
        img = _open_image(input_path)
        output_ext = suffix

        if action == "compress":
            output_ext = ".jpg"
            out_path = os.path.join(tmpdir, f"compressed_{stem}.jpg")
            img.save(out_path, "JPEG", quality=60, optimize=True)

        elif action == "convert":
            # Offer format choice via a second keyboard
            keyboard = [
                [InlineKeyboardButton("PNG", callback_data="convert_png"),
                 InlineKeyboardButton("JPEG", callback_data="convert_jpg"),
                 InlineKeyboardButton("WebP", callback_data="convert_webp")],
            ]
            await query.edit_message_text(
                "Choose the output format:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        elif action == "rotate_90":
            out_path = os.path.join(tmpdir, f"rotated_{stem}{suffix}")
            img.rotate(-90, expand=True).save(out_path)  # clockwise 90°[reference:3]

        elif action == "grayscale":
            out_path = os.path.join(tmpdir, f"gray_{stem}.jpg")
            img.convert("L").save(out_path, "JPEG")

        elif action == "blur":
            out_path = os.path.join(tmpdir, f"blur_{stem}.jpg")
            img.filter(ImageFilter.GaussianBlur(radius=2)).save(out_path, "JPEG")

        elif action == "sharpen":
            out_path = os.path.join(tmpdir, f"sharp_{stem}.jpg")
            img.filter(ImageFilter.SHARPEN).save(out_path, "JPEG")

        elif action == "bright":
            out_path = os.path.join(tmpdir, f"bright_{stem}.jpg")
            enhanced = ImageEnhance.Brightness(img).enhance(1.4)  # 1.0 = original[reference:4]
            enhanced.save(out_path, "JPEG")

        elif action == "contrast":
            out_path = os.path.join(tmpdir, f"contrast_{stem}.jpg")
            enhanced = ImageEnhance.Contrast(img).enhance(1.4)
            enhanced.save(out_path, "JPEG")

        elif action.startswith("convert_"):
            fmt = action.split("_", 1)[1]
            ext_map = {"png": ".png", "jpg": ".jpg", "webp": ".webp"}
            output_ext = ext_map[fmt]
            out_path = os.path.join(tmpdir, f"converted_{stem}{output_ext}")
            save_fmt = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[fmt]
            img.save(out_path, save_fmt)

        else:
            await query.edit_message_text("Unknown action.")
            return

        await query.edit_message_text("✅ Done. Sending the result…")
        await query.message.reply_document(
            document=open(out_path, "rb"),
            filename=os.path.basename(out_path),
        )

    except Exception:
        logger.exception(f"Action '{action}' failed")
        await query.edit_message_text("❌ That operation failed. Please try another image.")
    finally:
        # Clean up after immediate actions
        if action not in ("resize", "watermark", "convert"):
            context.user_data.clear()


# ---------- Text parameter handler (resize / watermark) ----------
async def handle_param_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting_param")
    if not awaiting:
        return

    text = update.message.text.strip()
    input_path = context.user_data["input_path"]
    tmpdir = context.user_data["tmpdir"]
    stem = Path(context.user_data.get("original_name", "image")).stem

    try:
        img = _open_image(input_path)

        if awaiting == "resize":
            try:
                w_str, h_str = text.lower().replace(" ", "").split("x")
                w, h = int(w_str), int(h_str)
                if w <= 0 or h <= 0:
                    raise ValueError
            except Exception:
                await update.message.reply_text(
                    "❌ Invalid size. Use `WIDTHxHEIGHT`, e.g. `800x600`."
                )
                return

            out_path = os.path.join(tmpdir, f"resized_{stem}.jpg")
            img.resize((w, h), Image.Resampling.LANCZOS).save(out_path, "JPEG", quality=90)
            caption = f"Resized to {w}×{h}"

        elif awaiting == "watermark":
            if not text:
                await update.message.reply_text("❌ Watermark text cannot be empty.")
                return
            out_path = os.path.join(tmpdir, f"watermarked_{stem}.jpg")
            _make_watermark(img, text).save(out_path, "JPEG", quality=90)
            caption = "Watermark applied"

        else:
            await update.message.reply_text("Unknown parameter request.")
            return

        await update.message.reply_document(
            document=open(out_path, "rb"),
            filename=os.path.basename(out_path),
            caption=caption,
        )

    except Exception:
        logger.exception("Parameter input failed")
        await update.message.reply_text("❌ Something went wrong. Please try again.")
    finally:
        context.user_data.clear()


# ---------- Main ----------
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Catch both photos and image documents
    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handle_image,
        )
    )

    # Text messages are routed to the parameter handler only when needed
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_param_input)
    )

    logger.info("Image Tools Bot starting in polling mode…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
