"""
🏪 BotStore — Telegram Directory for Bots, Groups & Channels
- Separate sections: Bots | Groups | Channels
- Submit by username/link, auto-approved + admin alert
- Search, categories, trending, top rated, featured (paid)
- Ratings, click tracking, owner-managed listings
- T&Cs acceptance gate for groups/channels (no NSFW, must follow Telegram ToS)
- Floating "/" command menu + popup (toast) confirmations
- Paystack-ready payment stub (manual confirmation until live keys are added)
- Multi-language scaffold
- Persistent JSON storage — listings are never auto-deleted
"""

import os
import json
import html
import logging
import random
import uuid
import httpx
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO)

# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET")  # not live yet — manual fallback used
PAYMENT_RECEIPT_EMAIL = os.environ.get("PAYMENT_RECEIPT_EMAIL", "customer@botstore.app")

# Set these AFTER you create the groups, by getting their chat IDs (forward a
# message from each group to @userinfobot, or use /getlogid /getdevid below
# once the bot is admin in them) and adding as Railway variables.
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "0")) or None   # records: listings, payments, ratings, clicks
DEV_GROUP_ID = int(os.environ.get("DEV_GROUP_ID", "0")) or None  # developer contribution group

FEATURED_PRICE_CEDIS = 20
FEATURED_DAYS = 30  # default tier; multi-tier can be added later

PREMIUM_PRICE_CEDIS = 500   # one-time payment to remove the free listing cap
FREE_BOT_LIMIT = 2          # max *bot* listings a non-premium user can have live at once

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

LISTINGS_FILE = os.path.join(DATA_DIR, "listings.json")   # all bots/groups/channels
RATINGS_FILE = os.path.join(DATA_DIR, "ratings.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")          # language pref, etc.
CLICKS_FILE = os.path.join(DATA_DIR, "clicks.json")
PAYMENTS_FILE = os.path.join(DATA_DIR, "payments.json")

LISTING_TYPES = ["bot", "group", "channel"]
PAGE_SIZE = 5  # listings per page when browsing

CATEGORIES = [
    "Finance", "Games", "Utility", "AI & Productivity", "Education",
    "Entertainment", "Crypto", "News", "Shopping", "Community", "Other"
]

# ════════════════════════════════════════════════════════════════════════════
# LANGUAGE SCAFFOLD (starter set — extend toward 25 over time)
# ════════════════════════════════════════════════════════════════════════════

LANGUAGES = {
    "en": "English", "fr": "Français", "tw": "Twi", "ha": "Hausa",
    "sw": "Swahili", "ar": "العربية"
}

STRINGS = {
    "en": {
        "welcome": "🏪 *Welcome to BotStore*\nDiscover, list, and grow bots, groups & channels.",
        "tos_warning": "⚠️ *Rules*: No nudity, no illegal content, must follow Telegram's Terms of Service. Listings violating this will be removed and reported.",
        "tos_accept": "✅ I Agree to the Terms",
        "tos_required": "You must accept the Terms before submitting a listing.",
    },
    # Other languages fall back to English until translated.
}

def t(lang: str, key: str) -> str:
    return STRINGS.get(lang, {}).get(key) or STRINGS["en"].get(key, key)

# ════════════════════════════════════════════════════════════════════════════
# STORAGE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def load_json(path) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_user(uid: int) -> dict:
    users = load_json(USERS_FILE)
    key = str(uid)
    if key not in users:
        users[key] = {
            "uid": uid, "lang": "en", "joined": datetime.now().isoformat(),
            "tos_accepted": False, "premium": False,
        }
        save_json(USERS_FILE, users)
    return users[key]

def save_user(u: dict):
    users = load_json(USERS_FILE)
    users[str(u["uid"])] = u
    save_json(USERS_FILE, users)

def is_premium(uid: int) -> bool:
    return get_user(uid).get("premium", False)

def set_premium(uid: int):
    u = get_user(uid)
    u["premium"] = True
    save_user(u)

def set_tos_accepted(uid: int):
    u = get_user(uid)
    u["tos_accepted"] = True
    save_user(u)

def has_accepted_tos(uid: int) -> bool:
    return get_user(uid).get("tos_accepted", False)

# ── Listings ──────────────────────────────────────────────────────────────

def new_listing_id() -> str:
    return uuid.uuid4().hex[:10]

def to_url(identifier: str) -> str:
    """Turns a stored identifier (@username, t.me/xxx, or full link) into a
    tappable https://t.me/... URL suitable for an inline URL button."""
    ident = (identifier or "").strip()
    if ident.startswith("http://") or ident.startswith("https://"):
        return ident
    if ident.startswith("t.me/"):
        return f"https://{ident}"
    if ident.startswith("@"):
        ident = ident[1:]
    return f"https://t.me/{ident}"

def esc(text: str) -> str:
    """Escapes legacy-Markdown special characters in user-supplied text
    (titles, descriptions, identifiers) before it goes into a parse_mode
    'Markdown' message, so a stray _ * ` or [ in a listing can't break
    message rendering."""
    text = "" if text is None else str(text)
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

async def log_event(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Posts an activity record to the dedicated log group, if configured.
    Never raises — logging failures shouldn't break the bot's main flow.
    """
    if not LOG_GROUP_ID:
        return
    try:
        await ctx.bot.send_message(LOG_GROUP_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"log_event failed: {e}")

def add_listing(owner_id: int, listing_type: str, identifier: str, title: str,
                 description: str, category: str) -> dict:
    listings = load_json(LISTINGS_FILE)
    lid = new_listing_id()
    entry = {
        "id": lid,
        "type": listing_type,             # bot | group | channel
        "identifier": identifier,         # @username or link
        "title": title,
        "description": description,
        "category": category,
        "owner_id": owner_id,
        "status": "live",                 # live | reported | removed
        "featured_until": None,           # ISO date string or None
        "created": datetime.now().isoformat(),
    }
    listings[lid] = entry
    save_json(LISTINGS_FILE, listings)
    return entry

def get_listing(lid: str) -> Optional[dict]:
    return load_json(LISTINGS_FILE).get(lid)

def update_listing(lid: str, **fields):
    listings = load_json(LISTINGS_FILE)
    if lid in listings:
        listings[lid].update(fields)
        save_json(LISTINGS_FILE, listings)

def list_by_type(listing_type: str, category: Optional[str] = None) -> List[dict]:
    listings = load_json(LISTINGS_FILE)
    out = [
        l for l in listings.values()
        if l["type"] == listing_type and l["status"] == "live"
        and (category is None or l["category"] == category)
    ]
    return out

def search_listings(query: str, listing_type: Optional[str] = None) -> List[dict]:
    q = query.lower().strip()
    listings = load_json(LISTINGS_FILE)
    out = []
    for l in listings.values():
        if l["status"] != "live":
            continue
        if listing_type and l["type"] != listing_type:
            continue
        if q in l["title"].lower() or q in l["description"].lower() or q in l["identifier"].lower():
            out.append(l)
    return out

def owner_listings(owner_id: int) -> List[dict]:
    listings = load_json(LISTINGS_FILE)
    return [l for l in listings.values() if l["owner_id"] == owner_id]

def count_active_bots(owner_id: int) -> int:
    """How many bot listings (not removed) this user currently owns —
    used to enforce the free-tier cap."""
    return len([l for l in owner_listings(owner_id) if l["type"] == "bot" and l["status"] != "removed"])

def bot_limit_reached(uid: int) -> bool:
    return (not is_premium(uid)) and count_active_bots(uid) >= FREE_BOT_LIMIT

def premium_upsell_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Go Premium — GHS {PREMIUM_PRICE_CEDIS} (unlimited bots)", callback_data="go_premium")],
        [InlineKeyboardButton("◀ Back", callback_data="home")],
    ])

def report_listing(lid: str):
    update_listing(lid, status="reported")

# ── Ratings ──────────────────────────────────────────────────────────────

def add_rating(lid: str, uid: int, stars: int):
    ratings = load_json(RATINGS_FILE)
    ratings.setdefault(lid, {})
    ratings[lid][str(uid)] = stars
    save_json(RATINGS_FILE, ratings)

def get_avg_rating(lid: str) -> Optional[float]:
    ratings = load_json(RATINGS_FILE).get(lid, {})
    if not ratings:
        return None
    vals = list(ratings.values())
    return round(sum(vals) / len(vals), 1)

# ── Clicks (for Trending) ───────────────────────────────────────────────

def record_click(lid: str):
    clicks = load_json(CLICKS_FILE)
    clicks[lid] = clicks.get(lid, 0) + 1
    save_json(CLICKS_FILE, clicks)

def get_clicks(lid: str) -> int:
    return load_json(CLICKS_FILE).get(lid, 0)

def trending(listing_type: str, limit: int = 10) -> List[dict]:
    items = list_by_type(listing_type)
    clicks = load_json(CLICKS_FILE)
    items.sort(key=lambda l: clicks.get(l["id"], 0), reverse=True)
    return items[:limit]

def top_rated(listing_type: str, limit: int = 10) -> List[dict]:
    items = list_by_type(listing_type)
    scored = [(l, get_avg_rating(l["id"]) or 0) for l in items]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [l for l, _ in scored[:limit]]

def featured(listing_type: str) -> List[dict]:
    now = datetime.now().isoformat()
    items = list_by_type(listing_type)
    return [l for l in items if l.get("featured_until") and l["featured_until"] > now]

# ── Payments (real Paystack when configured, manual fallback otherwise) ──

PAYSTACK_BASE = "https://api.paystack.co"

async def create_payment_request(uid: int, lid: Optional[str], email: str,
                                    purpose: str = "feature", amount: Optional[int] = None) -> dict:
    """Creates a pending payment record. If PAYSTACK_SECRET is set, calls
    Paystack's initialize-transaction endpoint and stores the real checkout
    URL + reference. If not set (or the call fails), falls back to manual
    confirmation via /confirmpay.
    purpose: "feature" (boosts one listing, needs lid) or "premium" (account-wide
    upgrade removing the free listing cap, lid is None).
    """
    amount = amount or (PREMIUM_PRICE_CEDIS if purpose == "premium" else FEATURED_PRICE_CEDIS)
    payments = load_json(PAYMENTS_FILE)
    pid = new_listing_id()
    record = {
        "id": pid, "uid": uid, "listing_id": lid, "purpose": purpose,
        "amount_cedis": amount, "status": "pending_manual_confirmation",
        "created": datetime.now().isoformat(),
        "checkout_url": None, "paystack_reference": None, "init_error": None,
    }

    if PAYSTACK_SECRET:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{PAYSTACK_BASE}/transaction/initialize",
                    headers={
                        "Authorization": f"Bearer {PAYSTACK_SECRET}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "email": email,
                        "amount": amount * 100,  # Paystack uses kobo/pesewas (smallest unit)
                        "currency": "GHS",
                        "reference": pid,  # must match our internal id — verify_paystack_payment()
                                            # and /confirmpay both look up Paystack by this pid
                        "metadata": {"uid": uid, "listing_id": lid, "purpose": purpose},
                    },
                )
                data = r.json()
                if r.status_code == 200 and data.get("status"):
                    record["checkout_url"] = data["data"]["authorization_url"]
                    record["paystack_reference"] = data["data"]["reference"]
                    record["status"] = "pending_paystack"
                else:
                    record["init_error"] = data.get("message") or f"HTTP {r.status_code}"
                    logging.warning(f"Paystack init failed: {data}")
        except Exception as e:
            record["init_error"] = str(e)
            logging.error(f"Paystack init error: {e}")
    else:
        record["init_error"] = "PAYSTACK_SECRET is not set on this deployment."

    payments[pid] = record
    save_json(PAYMENTS_FILE, payments)
    return payments[pid]

async def verify_paystack_payment(pid: str) -> bool:
    """Calls Paystack's verify endpoint for a given payment id/reference.
    Returns True only if Paystack confirms the transaction actually succeeded.
    """
    if not PAYSTACK_SECRET:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{PAYSTACK_BASE}/transaction/verify/{pid}",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"},
            )
            data = r.json()
            if r.status_code == 200 and data.get("status"):
                return data["data"]["status"] == "success"
    except Exception as e:
        logging.error(f"Paystack verify error: {e}")
    return False

def confirm_payment(pid: str) -> Optional[str]:
    """Marks a payment confirmed and applies its effect. Returns the purpose
    string ("feature" or "premium") on success, or None if the payment id
    doesn't exist."""
    payments = load_json(PAYMENTS_FILE)
    p = payments.get(pid)
    if not p:
        return None
    p["status"] = "confirmed"
    save_json(PAYMENTS_FILE, payments)
    purpose = p.get("purpose", "feature")
    if purpose == "premium":
        set_premium(p["uid"])
    else:
        until = (datetime.now() + timedelta(days=FEATURED_DAYS)).isoformat()
        update_listing(p["listing_id"], featured_until=until)
    return purpose

async def run_payment_flow(query, ctx: ContextTypes.DEFAULT_TYPE, uid: int,
                             lid: Optional[str], purpose: str, price: int, label: str):
    """One-tap payment flow, no email prompt, no 'request created' text:
    tap -> '⏳ Generating your payment link…' -> Pay + Verify + Back card.
    `label` is the human title shown on the card, e.g. 'VIP Membership' / 'Go Premium'.
    Uses HTML parse mode (not Markdown) so a stray listing title can never
    break message rendering the way a raw underscore did before.
    """
    await query.answer()
    await query.edit_message_text("⏳ Generating your payment link…")
    payment = await create_payment_request(uid, lid, email=PAYMENT_RECEIPT_EMAIL, purpose=purpose)
    if payment.get("checkout_url"):
        back_cb = "feature_info" if purpose == "feature" else "home"
        pay_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💳 Pay GHS {price}", url=payment["checkout_url"])],
            [InlineKeyboardButton("✅ I've Paid — Verify", callback_data=f"verifypay_{payment['id']}")],
            [InlineKeyboardButton("◀ Back", callback_data=back_cb)],
        ])
        card = (
            f"👑 <b>{html.escape(label)} — GHS {price}</b>\n\n"
            f"Tap the button below to complete your payment securely via Paystack 👇"
        )
        await query.message.reply_text(card, parse_mode="HTML", reply_markup=pay_kb)
    else:
        reason = payment.get("init_error") or "Unknown error"
        await query.message.reply_text("⚠️ Payment setup failed. Please try again in a moment.")
        if ADMIN_ID:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Paystack checkout failed\nPurpose: {purpose}\nRef: {payment['id']}\n"
                    f"User: {uid}\nListing: {lid}\nReason: {reason}\n"
                    f"Manual confirm: /confirmpay {payment['id']}"
                )
            except Exception as e:
                logging.error(f"Admin payment alert failed: {e}")

# ════════════════════════════════════════════════════════════════════════════
# MENUS
# ════════════════════════════════════════════════════════════════════════════

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Bots", callback_data="sec_bot"),
         InlineKeyboardButton("👥 Groups", callback_data="sec_group"),
         InlineKeyboardButton("📢 Channels", callback_data="sec_channel")],
        [InlineKeyboardButton("🔍 Search", callback_data="search"),
         InlineKeyboardButton("📂 Categories", callback_data="cats")],
        [InlineKeyboardButton("👑 Get Featured", callback_data="feature_info"),
         InlineKeyboardButton("📜 My Listings", callback_data="mylistings")],
        [InlineKeyboardButton("🌐 Language", callback_data="lang"),
         InlineKeyboardButton("ℹ️ Help / Terms", callback_data="help")],
    ])

def section_menu(listing_type: str) -> InlineKeyboardMarkup:
    label = {"bot": "Bot", "group": "Group", "channel": "Channel"}[listing_type]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add a {label}", callback_data=f"add_{listing_type}")],
        [InlineKeyboardButton("🔥 Trending", callback_data=f"trend_{listing_type}"),
         InlineKeyboardButton("⭐ Top Rated", callback_data=f"top_{listing_type}")],
        [InlineKeyboardButton("👑 Featured", callback_data=f"feat_{listing_type}"),
         InlineKeyboardButton("📂 By Category", callback_data=f"catlist_{listing_type}")],
        [InlineKeyboardButton("◀ Back", callback_data="home")],
    ])

def tos_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I Agree to the Terms", callback_data="tos_accept")],
        [InlineKeyboardButton("◀ Cancel", callback_data="home")],
    ])

def stars_keyboard(lid: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("⭐" * n, callback_data=f"star_{lid}_{n}") for n in range(1, 6)]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("◀ Cancel", callback_data=f"backview_{lid}")]])

# ════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    await update.message.reply_text(
        t("en", "welcome"), parse_mode="Markdown", reply_markup=main_menu()
    )

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["mode"] = "search_all"
    await update.message.reply_text("🔍 Type a name or keyword to search bots, groups & channels:")

async def cmd_addbot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await begin_add_flow(update.effective_user.id, "bot", ctx, update.message)

async def cmd_addgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await begin_add_flow(update.effective_user.id, "group", ctx, update.message)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t("en", "welcome") + "\n\n" + t("en", "tos_warning"), parse_mode="Markdown"
    )

async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID:
        await update.message.reply_text("Support contact isn't configured yet.")
        return
    await update.message.reply_text(
        f"🛠 *Support / Developer*\nFor issues, listing disputes, or to contribute, "
        f"contact: [Developer](tg://user?id={ADMIN_ID}) (ID: `{ADMIN_ID}`)",
        parse_mode="Markdown"
    )

async def cmd_getchatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: run this inside the records/log group or dev group to get
    its chat ID for the LOG_GROUP_ID / DEV_GROUP_ID Railway variables."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"This chat's ID: `{update.effective_chat.id}`", parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════════════════
# ADD-LISTING FLOW (multi-step, stored in user_data)
# ════════════════════════════════════════════════════════════════════════════

async def begin_add_flow(uid: int, listing_type: str, ctx: ContextTypes.DEFAULT_TYPE, message):
    if listing_type == "bot" and bot_limit_reached(uid):
        await message.reply_text(
            f"🚫 Free accounts can list up to {FREE_BOT_LIMIT} bots, and you're at that limit.\n"
            f"Go Premium for a one-time GHS {PREMIUM_PRICE_CEDIS} to add unlimited bots.",
            reply_markup=premium_upsell_keyboard()
        )
        return
    if listing_type in ("group", "channel") and not has_accepted_tos(uid):
        await message.reply_text(
            t("en", "tos_warning"), parse_mode="Markdown", reply_markup=tos_keyboard()
        )
        ctx.user_data["pending_add_type"] = listing_type
        return
    ctx.user_data["adding"] = {"type": listing_type, "step": "identifier"}
    await message.reply_text(
        f"Send the @username or public link of the {listing_type} you want to add:"
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    mode = ctx.user_data.get("mode")
    adding = ctx.user_data.get("adding")

    # Multi-step add-listing flow
    if adding:
        step = adding["step"]
        if step == "identifier":
            adding["identifier"] = text
            adding["step"] = "title"
            ctx.user_data["adding"] = adding
            await update.message.reply_text("Now send a short title/name for it:")
            return
        if step == "title":
            adding["title"] = text
            adding["step"] = "description"
            ctx.user_data["adding"] = adding
            await update.message.reply_text("Now send a short description (what it does / what it's about):")
            return
        if step == "description":
            adding["description"] = text
            adding["step"] = "category"
            ctx.user_data["adding"] = adding
            cat_buttons = [[InlineKeyboardButton(c, callback_data=f"setcat_{c}")] for c in CATEGORIES]
            await update.message.reply_text(
                "Choose a category:", reply_markup=InlineKeyboardMarkup(cat_buttons)
            )
            return

    if mode == "search_all":
        results = search_listings(text)
        ctx.user_data["mode"] = None
        if not results:
            await update.message.reply_text("No results found. Try a different keyword.")
            return
        await send_listing_results(update.message, results, ctx, header=f"🔍 Results for \"{text}\"", back="home")
        return

    # Fallback
    await update.message.reply_text("Use /start to open the menu.")

async def send_listing_results(target, results: List[dict], ctx: ContextTypes.DEFAULT_TYPE,
                                 header: str = "Results", back: str = "home", edit: bool = False):
    """Stores the result set in user_data and renders page 1 as a tappable list.
    `target` is a Message (edit=False) or a CallbackQuery (edit=True)."""
    ctx.user_data["browse"] = {
        "lids": [l["id"] for l in results],
        "page": 0,
        "header": header,
        "back": back,
    }
    await render_browse_page(target, ctx, edit=edit)

async def render_browse_page(target, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = True):
    """Renders the current page of ctx.user_data['browse'] as a tappable list
    with Prev/Next pagination and a 'Get Featured' ad button. `target` is
    either a Message (edit=False, sends new) or a CallbackQuery (edit=True)."""
    browse = ctx.user_data.get("browse")
    if not browse or not browse["lids"]:
        text = "Nothing here yet."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data=(browse or {}).get("back", "home"))]])
        if edit:
            await target.edit_message_text(text, reply_markup=kb)
        else:
            await target.reply_text(text, reply_markup=kb)
        return

    lids = browse["lids"]
    total_pages = max(1, (len(lids) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(browse["page"], total_pages - 1))
    browse["page"] = page
    chunk = lids[page * PAGE_SIZE: page * PAGE_SIZE + PAGE_SIZE]

    listings = load_json(LISTINGS_FILE)
    rows = []
    for lid in chunk:
        l = listings.get(lid)
        if not l:
            continue
        avg = get_avg_rating(lid)
        rating_str = f"⭐{avg}" if avg else "⭐–"
        tag = "👑 " if l.get("featured_until") and l["featured_until"] > datetime.now().isoformat() else ""
        label = f"{tag}{l['title']} ({l['type']}) · {rating_str}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"view_{lid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data="page_prev"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data="page_next"))
    rows.append(nav)

    rows.append([InlineKeyboardButton("👑 Get Featured — promote your listing", callback_data="feature_info")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=browse.get("back", "home"))])

    text = f"{browse.get('header', 'Results')}\nShowing {len(lids)} result(s) · page {page + 1}/{total_pages}"
    kb = InlineKeyboardMarkup(rows)
    if edit:
        await target.edit_message_text(text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)

def detail_card_text(l: dict) -> str:
    avg = get_avg_rating(l["id"])
    rating_str = f"⭐ {avg}/5" if avg else "⭐ No ratings yet"
    featured_tag = "👑 FEATURED\n" if l.get("featured_until") and l["featured_until"] > datetime.now().isoformat() else ""
    return (
        f"{featured_tag}*{esc(l['title'])}*\n"
        f"📂 {esc(l['category'])} · {esc(l['type'].title())}\n"
        f"{rating_str} · 👁 {get_clicks(l['id'])} views\n\n"
        f"_{esc(l['description'])}_\n\n"
        f"`{esc(l['identifier'])}`"
    )

def detail_card_keyboard(lid: str, identifier: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Open", url=to_url(identifier))],
        [InlineKeyboardButton("⭐ Rate", callback_data=f"rate_{lid}"),
         InlineKeyboardButton("📤 Share", callback_data=f"share_{lid}")],
        [InlineKeyboardButton("🚩 Report", callback_data=f"report_{lid}")],
        [InlineKeyboardButton("◀ Back to list", callback_data="browse_back")],
    ])

# ════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLER
# ════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    data = query.data

    # HOME
    if data == "home":
        await query.answer()
        await query.edit_message_text(t("en", "welcome"), parse_mode="Markdown", reply_markup=main_menu())
        return

    # SECTIONS
    if data.startswith("sec_"):
        await query.answer()
        ltype = data.split("_", 1)[1]
        await query.edit_message_text(f"📂 *{ltype.title()}s*\nChoose an option:", parse_mode="Markdown",
                                        reply_markup=section_menu(ltype))
        return

    # ADD LISTING (from section menu)
    if data.startswith("add_"):
        ltype = data.split("_", 1)[1]
        if ltype == "bot" and bot_limit_reached(uid):
            await query.answer()
            await query.edit_message_text(
                f"🚫 Free accounts can list up to {FREE_BOT_LIMIT} bots, and you're at that limit.\n"
                f"Go Premium for a one-time GHS {PREMIUM_PRICE_CEDIS} to add unlimited bots.",
                reply_markup=premium_upsell_keyboard()
            )
            return
        if ltype in ("group", "channel") and not has_accepted_tos(uid):
            await query.answer()
            await query.edit_message_text(
                t("en", "tos_warning"), parse_mode="Markdown", reply_markup=tos_keyboard()
            )
            ctx.user_data["pending_add_type"] = ltype
            return
        await query.answer("Opening submission form…")
        ctx.user_data["adding"] = {"type": ltype, "step": "identifier"}
        await query.edit_message_text(f"Send the @username or public link of the {ltype} you want to add:")
        return

    # T&Cs ACCEPT — popup confirmation
    if data == "tos_accept":
        set_tos_accepted(uid)
        await query.answer("✅ Terms accepted. You can now submit your listing.", show_alert=True)
        pending = ctx.user_data.pop("pending_add_type", None)
        if pending:
            ctx.user_data["adding"] = {"type": pending, "step": "identifier"}
            await query.edit_message_text(f"Send the @username or public link of the {pending} you want to add:")
        else:
            await query.edit_message_text(t("en", "welcome"), parse_mode="Markdown", reply_markup=main_menu())
        return

    # CATEGORY SELECTION during add flow
    if data.startswith("setcat_"):
        category = data.split("_", 1)[1]
        adding = ctx.user_data.get("adding")
        if not adding:
            await query.answer("Session expired, please start again with /start.", show_alert=True)
            return
        entry = add_listing(
            owner_id=uid, listing_type=adding["type"], identifier=adding["identifier"],
            title=adding["title"], description=adding["description"], category=category
        )
        ctx.user_data["adding"] = None
        await query.answer("🎉 Listing submitted and live!", show_alert=True)
        await query.edit_message_text(
            f"✅ *{esc(entry['title'])}* added to the {entry['type']} directory under *{esc(category)}*.\n"
            f"It's live now. Want more visibility? Get Featured from the main menu.",
            parse_mode="Markdown", reply_markup=main_menu()
        )
        if ADMIN_ID:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"🆕 New {entry['type']} listing: *{esc(entry['title'])}*\n"
                    f"{esc(entry['identifier'])}\nCategory: {esc(category)}\nBy user: {uid}\n"
                    f"Please check it follows the rules.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Admin alert failed: {e}")
        await log_event(ctx, f"🆕 *Listing*: {esc(entry['title'])} ({entry['type']}) by `{uid}` — {esc(category)}")
        return

    # TRENDING / TOP RATED / FEATURED / CATEGORY LIST
    if data.startswith("trend_") or data.startswith("top_") or data.startswith("feat_") or data.startswith("catlist_"):
        await query.answer()
        kind, ltype = data.split("_", 1)
        if kind == "trend":
            results = trending(ltype)
            header = f"🔥 Trending {ltype}s"
        elif kind == "top":
            results = top_rated(ltype)
            header = f"⭐ Top rated {ltype}s"
        elif kind == "feat":
            results = featured(ltype)
            header = f"👑 Featured {ltype}s"
        else:
            cat_buttons = []
            for c in CATEGORIES:
                n = len(list_by_type(ltype, c))
                cat_buttons.append([InlineKeyboardButton(f"{c} ({n})", callback_data=f"catpick_{ltype}_{c}")])
            cat_buttons.append([InlineKeyboardButton("◀ Back", callback_data=f"sec_{ltype}")])
            await query.edit_message_text(f"📂 *{ltype.title()}s by category:*", parse_mode="Markdown",
                                            reply_markup=InlineKeyboardMarkup(cat_buttons))
            return
        if not results:
            await query.edit_message_text("Nothing here yet.", reply_markup=section_menu(ltype))
            return
        await send_listing_results(query, results, ctx, header=header, back=f"sec_{ltype}", edit=True)
        return

    if data.startswith("catpick_"):
        await query.answer()
        _, ltype, category = data.split("_", 2)
        results = list_by_type(ltype, category)
        if not results:
            await query.edit_message_text("No listings in this category yet.", reply_markup=section_menu(ltype))
            return
        await send_listing_results(query, results, ctx, header=f"📂 {category} {ltype}s", back=f"catlist_{ltype}", edit=True)
        return

    # VIEW DETAIL CARD (tapped a listing from a list page) — counts as a view
    if data.startswith("view_"):
        lid = data.split("_", 1)[1]
        l = get_listing(lid)
        if not l:
            await query.answer("Listing not found.", show_alert=True)
            return
        await query.answer()
        record_click(lid)
        await log_event(ctx, f"👁 *View*: {esc(l['title'])} (`{esc(l['identifier'])}`) by `{uid}`")
        await query.edit_message_text(
            detail_card_text(l), parse_mode="Markdown",
            reply_markup=detail_card_keyboard(lid, l["identifier"])
        )
        return

    # Redisplay detail card without counting a new view (e.g. cancelling a rating)
    if data.startswith("backview_"):
        lid = data.split("_", 1)[1]
        l = get_listing(lid)
        if not l:
            await query.answer("Listing not found.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            detail_card_text(l), parse_mode="Markdown",
            reply_markup=detail_card_keyboard(lid, l["identifier"])
        )
        return

    # PAGINATION
    if data == "page_prev":
        await query.answer()
        browse = ctx.user_data.get("browse")
        if browse:
            browse["page"] = max(0, browse["page"] - 1)
        await render_browse_page(query, ctx, edit=True)
        return

    if data == "page_next":
        await query.answer()
        browse = ctx.user_data.get("browse")
        if browse:
            browse["page"] = browse["page"] + 1
        await render_browse_page(query, ctx, edit=True)
        return

    if data == "browse_back":
        await query.answer()
        await render_browse_page(query, ctx, edit=True)
        return

    if data == "noop":
        await query.answer()
        return

    # RATE
    if data.startswith("rate_"):
        await query.answer()
        lid = data.split("_", 1)[1]
        await query.edit_message_text("Rate this listing:", reply_markup=stars_keyboard(lid))
        return

    if data.startswith("star_"):
        _, lid, n = data.split("_")
        add_rating(lid, uid, int(n))
        await query.answer(f"Thanks! You rated it {n}⭐", show_alert=True)
        l = get_listing(lid)
        if l:
            await log_event(ctx, f"⭐ *Rating*: {esc(l['title'])} rated {n}/5 by `{uid}`")
            await query.edit_message_text(
                detail_card_text(l), parse_mode="Markdown",
                reply_markup=detail_card_keyboard(lid, l["identifier"])
            )
        return

    # SHARE
    if data.startswith("share_"):
        lid = data.split("_", 1)[1]
        l = get_listing(lid)
        if l:
            share_text = f"Check out {l['title']}: {l['identifier']} — found on BotStore!"
            await query.answer()
            await query.message.reply_text(f"Forward this to share:\n\n{share_text}")
        else:
            await query.answer("Listing not found.", show_alert=True)
        return

    # REPORT — popup confirmation
    if data.startswith("report_"):
        lid = data.split("_", 1)[1]
        report_listing(lid)
        await query.answer("🚩 Reported. Our team will review this listing.", show_alert=True)
        if ADMIN_ID:
            l = get_listing(lid)
            try:
                await ctx.bot.send_message(ADMIN_ID, f"🚩 Listing reported: {l['title']} ({l['identifier']}) — id {lid}")
            except Exception as e:
                logging.error(f"Admin report alert failed: {e}")
        return

    # SEARCH
    if data == "search":
        await query.answer()
        ctx.user_data["mode"] = "search_all"
        await query.edit_message_text("🔍 Type a name or keyword to search:")
        return

    # CATEGORIES (top-level browse) — choose section first
    if data == "cats":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🤖 Bots", callback_data="catlist_bot")],
            [InlineKeyboardButton("👥 Groups", callback_data="catlist_group")],
            [InlineKeyboardButton("📢 Channels", callback_data="catlist_channel")],
            [InlineKeyboardButton("◀ Back", callback_data="home")],
        ])
        await query.edit_message_text("📂 Browse by category — choose a section:", reply_markup=kb)
        return

    # FEATURE INFO — now actionable: list the user's own listings to feature directly
    if data == "feature_info":
        await query.answer()
        mine = [l for l in owner_listings(uid) if l["status"] != "removed"]
        rows = []
        for l in mine:
            tag = "👑 " if l.get("featured_until") and l["featured_until"] > datetime.now().isoformat() else ""
            rows.append([InlineKeyboardButton(
                f"{tag}{l['title']} ({l['type']})"[:64], callback_data=f"requestfeature_{l['id']}"
            )])
        rows.append([InlineKeyboardButton(
            f"🚀 Go Premium — GHS {PREMIUM_PRICE_CEDIS} (unlimited bots)", callback_data="go_premium"
        )])
        rows.append([InlineKeyboardButton("◀ Back", callback_data="home")])
        intro = (
            f"👑 *Get Featured*\n"
            f"GHS {FEATURED_PRICE_CEDIS} for {FEATURED_DAYS} days at the top of your category + homepage rotation.\n\n"
        )
        if mine:
            intro += "Pick a listing below to feature it, or go Premium for unlimited bots:"
        else:
            intro += (
                "You don't have a listing to feature yet — add one from the main menu first.\n\n"
                "That's only for *Get Featured*, though — Premium below doesn't need a listing, "
                "it just removes your free bot-listing cap:"
            )
        await query.edit_message_text(intro, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("requestfeature_"):
        lid = data.split("_", 1)[1]
        await run_payment_flow(query, ctx, uid, lid, "feature", FEATURED_PRICE_CEDIS, "Get Featured")
        return

    # GO PREMIUM — account-wide upgrade removing the free bot-listing cap
    if data == "go_premium":
        if is_premium(uid):
            await query.answer()
            await query.edit_message_text("🚀 You're already Premium — enjoy unlimited bot listings!",
                                            reply_markup=main_menu())
            return
        await run_payment_flow(query, ctx, uid, None, "premium", PREMIUM_PRICE_CEDIS, "Go Premium")
        return

    # SELF-SERVE PAYMENT VERIFICATION ("I've Paid — Verify")
    if data.startswith("verifypay_"):
        pid = data.split("_", 1)[1]
        await query.answer("🔍 Verifying…")
        payments = load_json(PAYMENTS_FILE)
        p = payments.get(pid)
        if not p:
            await query.message.reply_text("⚠️ Payment reference not found.")
            return
        if p.get("uid") != uid:
            await query.message.reply_text("⚠️ This payment doesn't belong to your account.")
            return
        if p.get("status") == "confirmed":
            await query.message.reply_text("✅ Already confirmed — you're all set!")
            return
        if not PAYSTACK_SECRET:
            await query.message.reply_text("⚠️ Online verification isn't configured. Please wait for admin confirmation.")
            return
        verified = await verify_paystack_payment(pid)
        if not verified:
            retry_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data=f"verifypay_{pid}")]])
            await query.message.reply_text(
                "❌ Payment not confirmed yet.\nIf you've just paid, wait a few seconds and tap Try Again.",
                reply_markup=retry_kb
            )
            return
        purpose = confirm_payment(pid)
        if purpose == "premium":
            await query.message.reply_text(
                "🎉 *Payment confirmed!*\nYou're Premium now — your bot listing limit is removed.",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text(
                f"🎉 *Payment confirmed!*\nYour listing is now featured for {FEATURED_DAYS} days.",
                parse_mode="Markdown"
            )
        await log_event(ctx, f"💰 *Payment confirmed (self-serve)*: `{pid}` by `{uid}`")
        if ADMIN_ID:
            try:
                await ctx.bot.send_message(ADMIN_ID, f"💰 Payment self-verified\nRef: {pid}\nUser: {uid}\nPurpose: {purpose}")
            except Exception as e:
                logging.warning(f"Admin self-verify alert failed: {e}")
        return

    # MY LISTINGS
    if data == "mylistings":
        await query.answer()
        mine = owner_listings(uid)
        if not mine:
            await query.edit_message_text("You haven't added anything yet.", reply_markup=main_menu())
            return
        await query.edit_message_text(f"You have {len(mine)} listing(s):")
        for l in mine:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Feature This", callback_data=f"requestfeature_{l['id']}")],
                [InlineKeyboardButton("◀ Back", callback_data="home")],
            ])
            await query.message.reply_text(
                f"*{esc(l['title'])}* ({l['type']})\n{esc(l['identifier'])}\nStatus: {l['status']}",
                parse_mode="Markdown", reply_markup=kb
            )
        return

    # LANGUAGE (scaffold)
    if data == "lang":
        await query.answer()
        buttons = [[InlineKeyboardButton(name, callback_data=f"setlang_{code}")] for code, name in LANGUAGES.items()]
        await query.edit_message_text("🌐 Choose your language:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("setlang_"):
        code = data.split("_", 1)[1]
        u = get_user(uid)
        u["lang"] = code
        save_user(u)
        await query.answer(f"Language set to {LANGUAGES.get(code, code)}", show_alert=True)
        await query.edit_message_text(t(code, "welcome"), parse_mode="Markdown", reply_markup=main_menu())
        return

    # HELP
    if data == "help":
        await query.answer()
        await query.edit_message_text(
            t("en", "welcome") + "\n\n" + t("en", "tos_warning"),
            parse_mode="Markdown", reply_markup=main_menu()
        )
        return

    await query.answer()

# ════════════════════════════════════════════════════════════════════════════
# ADMIN: manual payment confirmation
# ════════════════════════════════════════════════════════════════════════════

async def cmd_envcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: shows which required env vars this RUNNING process actually
    sees (masked), so you can tell a Railway variable typo/missing-redeploy
    apart from a real Paystack rejection without digging through logs."""
    if update.effective_user.id != ADMIN_ID:
        return
    def mask(v):
        if not v:
            return "❌ NOT SET"
        return f"✅ set ({v[:6]}…{v[-4:]}, {len(v)} chars)" if len(v) > 12 else "✅ set (short value)"
    lines = [
        "🔧 *Environment check* (this running deployment):",
        f"TELEGRAM_TOKEN: {mask(os.environ.get('TELEGRAM_TOKEN'))}",
        f"ADMIN_ID: {'✅ set (' + str(ADMIN_ID) + ')' if ADMIN_ID else '❌ NOT SET'}",
        f"PAYSTACK_SECRET: {mask(PAYSTACK_SECRET)}",
        f"PAYMENT_RECEIPT_EMAIL: {'✅ ' + PAYMENT_RECEIPT_EMAIL}",
        f"LOG_GROUP_ID: {'✅ set' if LOG_GROUP_ID else '❌ NOT SET'}",
    ]
    lines.append(
        "\nIf PAYSTACK_SECRET shows NOT SET here but you *do* see it in Railway "
        "Variables, the running container just hasn't picked it up yet — trigger "
        "a fresh redeploy (Railway only injects vars set *before* the deploy starts)."
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_confirmpay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /confirmpay <payment_id> [force]")
        return
    pid = ctx.args[0]
    force = len(ctx.args) > 1 and ctx.args[1].lower() == "force"

    if PAYSTACK_SECRET and not force:
        verified = await verify_paystack_payment(pid)
        if not verified:
            await update.message.reply_text(
                "⚠️ Paystack did not confirm this payment as successful. "
                "If you're sure it was paid (e.g. manual/offline payment), run:\n"
                f"`/confirmpay {pid} force`",
                parse_mode="Markdown"
            )
            return

    purpose = confirm_payment(pid)
    if not purpose:
        await update.message.reply_text("❌ Payment ID not found.")
        return

    payments = load_json(PAYMENTS_FILE)
    payer_uid = payments.get(pid, {}).get("uid")

    if purpose == "premium":
        await update.message.reply_text("✅ Confirmed — user upgraded to Premium (unlimited bot listings).")
        await log_event(ctx, f"💰 *Payment confirmed*: `{pid}` → Premium upgrade for `{payer_uid}`")
        if payer_uid:
            try:
                await ctx.bot.send_message(
                    payer_uid,
                    "🚀 *You're Premium now!*\nYour bot listing limit has been removed — add as many as you like.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.warning(f"Premium confirm DM failed: {e}")
    else:
        await update.message.reply_text("✅ Confirmed and featured.")
        await log_event(ctx, f"💰 *Payment confirmed*: `{pid}` → featured for {FEATURED_DAYS} days")
        if payer_uid:
            try:
                await ctx.bot.send_message(
                    payer_uid,
                    f"👑 *Payment confirmed!*\nYour listing is now featured for {FEATURED_DAYS} days.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.warning(f"Feature confirm DM failed: {e}")

# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

async def post_init(app):
    # Floating "/" command menu
    await app.bot.set_my_commands([
        BotCommand("start", "Open the BotStore menu"),
        BotCommand("search", "Search bots, groups & channels"),
        BotCommand("addbot", "Add a bot to the directory"),
        BotCommand("addgroup", "Add a group/channel"),
        BotCommand("support", "Contact support / developer"),
        BotCommand("help", "Rules & help"),
    ])

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("addbot", cmd_addbot))
    app.add_handler(CommandHandler("addgroup", cmd_addgroup))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("getchatid", cmd_getchatid))
    app.add_handler(CommandHandler("confirmpay", cmd_confirmpay))
    app.add_handler(CommandHandler("envcheck", cmd_envcheck))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("🏪 BotStore starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
