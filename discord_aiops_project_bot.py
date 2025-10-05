import os
import sqlite3
import json
import logging
from datetime import datetime, date, timedelta
import io
import asyncio

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button
from PIL import Image, ImageDraw, ImageFont

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
BOT_PREFIX = os.environ.get("BOT_PREFIX", "!")
DB_PATH = os.environ.get("DB_PATH", "project_bot.db")
PROJECT_CHANNEL_ID = int(os.environ.get("PROJECT_CHANNEL_ID", "0"))
GANTT_IMAGE_PATH = os.environ.get("GANTT_IMAGE_PATH", "gantt.png")
GANTT_START = os.environ.get("GANTT_START", "2025-09-21")
GANTT_END = os.environ.get("GANTT_END", "2026-02-08")
REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "48"))
REMINDER_LOOP_MINUTES = int(os.environ.get("REMINDER_LOOP_MINUTES", "60"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("project_bot")

_conn = None

def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT,
              due_date TEXT,
              created_by INTEGER,
              created_at TEXT,
              completed INTEGER DEFAULT 0,
              claimed_by TEXT DEFAULT '[]',
              week_start TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS announcements (
              message_id INTEGER PRIMARY KEY,
              week_start TEXT
    )
    """)
    conn.commit()

def iso_date(d: date):
    return d.isoformat()

def parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    raise ValueError("Date format should be YYYY-MM-DD or DD/MM/YYYY")

def add_task(title, due_date_str, description, created_by):
    due = parse_date(due_date_str)
    week_start = (due - timedelta(days=due.weekday())).isoformat()
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (title, description, due_date, created_by, created_at, week_start) VALUES (?, ?, ?, ?, ?, ?)",
        (title, description or "", due.isoformat(), created_by, datetime.utcnow().isoformat(), week_start),
    )
    conn.commit()
    return c.lastrowid

def get_tasks_for_week(week_start_iso: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE week_start = ? ORDER BY due_date", (week_start_iso,))
    rows = c.fetchall()
    return [dict(r) for r in rows]

def get_open_tasks_for_week(week_start_iso: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE week_start = ? AND completed = 0 ORDER BY due_date", (week_start_iso,))
    rows = c.fetchall()
    return [dict(r) for r in rows]

def get_task(task_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    return dict(row) if row else None

def claim_task(task_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    task = get_task(task_id)
    if not task:
        return None
    claimed = json.loads(task.get("claimed_by") or "[]")
    if user_id not in claimed:
        claimed.append(user_id)
    c.execute("UPDATE tasks SET claimed_by = ? WHERE id = ?", (json.dumps(claimed), task_id))
    conn.commit()
    return claimed

def unclaim_task(task_id, user_id):
    conn = get_conn()
    c = conn.cursor()
    task = get_task(task_id)
    if not task:
        return None
    claimed = json.loads(task.get("claimed_by") or "[]")
    if user_id in claimed:
        claimed.remove(user_id)
    c.execute("UPDATE tasks SET claimed_by = ? WHERE id = ?", (json.dumps(claimed), task_id))
    conn.commit()
    return claimed

def complete_task(task_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE tasks SET completed = 1 WHERE id = ?", (task_id,))
    conn.commit()

def overlay_gantt_with_today(image_path, project_start_str, project_end_str):
    GANTT_LEFT_MARGIN = 380
    GANTT_RIGHT_MARGIN = 30

    base_img = Image.open(image_path).convert("RGBA")
    draw = ImageDraw.Draw(base_img)

    start = parse_date(project_start_str)
    end = parse_date(project_end_str)
    total_days = (end - start).days
    if total_days <= 0:
        total_days = 1
    
    today = date.today()
    width = base_img.width

    bar_left = GANTT_LEFT_MARGIN
    bar_right = width - GANTT_RIGHT_MARGIN

    if today < start:
        pos = bar_left
    elif today > end:
        pos = bar_right
    else:
        pos = bar_left + (bar_right - bar_left) * (today - start).days / total_days
    
    draw.line([pos, 0, pos, base_img.height], fill=(255, 0, 0, 255), width=4)

    b = io.BytesIO()
    base_img.save(b, format="PNG")
    b.seek(0)
    return b

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

@bot.event
async def on_ready():
    init_db()
    logger.info(f"Bot ready as {bot.user} (ID {bot.user.id})")
    if REMINDER_LOOP_MINUTES > 0:
        check_deadlines.start()

@bot.command(name="help")
async def _help(ctx):
    txt = (
        f"Commandes disponibles:\n"
        f"{BOT_PREFIX}addtask \"titre\" YYYY-MM-DD [description] - ajoute une tâche\n"
        f"{BOT_PREFIX}listtasks [YYYY-MM-DD] - liste les tâches de la semaine donnée (par défaut semaine en cours)\n"
        f"{BOT_PREFIX}announce_week [YYYY-MM-DD] - annonce les tâches de la semaine et publie boutons de claim/completion\n"
        f"{BOT_PREFIX}gantt - génère et envoie le diagramme de Gantt (image)\n"
        f"{BOT_PREFIX}progress [YYYY-MM-DD] - affiche la progression des tâches pour la semaine\n"
        f"{BOT_PREFIX}complete TASK_ID - marque la tâche comme complétée\n"
    )
    await ctx.send(f"```{txt}```")

@bot.command(name="addtask")
async def _addtask(ctx, title: str, due_date: str, *, description: str = ""):
    if PROJECT_CHANNEL_ID and ctx.channel.id != PROJECT_CHANNEL_ID:
        await ctx.send(f"Les tâches doivent être ajoutées dans le salon dédié (ID {PROJECT_CHANNEL_ID}).")
        return
    try:
        tid = add_task(title, due_date, description, ctx.author.id)
    except ValueError as e:
        await ctx.send(f"Erreur de date: {e}")
        return
    await ctx.send(f"Tâche ajoutée #{tid} - {title} (due {due_date})")

@bot.command(name="listtasks")
async def _listtasks(ctx, week_start: str = None):
    if week_start is None:
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
    try:
        parse_date(week_start)
    except ValueError:
        await ctx.send("Date de début de semaine invalide. Format attendu: YYYY-MM-DD")
        return
    tasks_week = get_tasks_for_week(week_start)
    if not tasks_week:
        await ctx.send("Aucune tâche pour cette semaine.")
        return
    lines = []
    for t in tasks_week:
        claimer_ids = json.loads(t.get("claimed_by") or "[]")
        claimer_names = ", ".join([f"<@{i}>" for i in claimer_ids]) or "(personne)"
        status = "✅" if t.get("completed") else "🔲"
        lines.append(f"#{t['id']} {status} {t['title']} — due {t['due_date']} — {claimer_names}")
    await ctx.send("\n".join(lines))

@bot.command(name="announce_week")
async def _announce_week(ctx, week_start: str = None):
    if PROJECT_CHANNEL_ID and ctx.channel.id != PROJECT_CHANNEL_ID:
        await ctx.send(f"L'annonce doit être faite dans le salon dédié (ID {PROJECT_CHANNEL_ID}).")
        return
    if week_start is None:
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
    try:
        parse_date(week_start)
    except ValueError:
        await ctx.send("Date de début de semaine invalide. Format attendu: YYYY-MM-DD")
        return
    tasks_week = get_open_tasks_for_week(week_start)
    if not tasks_week:
        await ctx.send("Aucune tâche ouverte pour cette semaine.")
        return
    
    embed = discord.Embed(title=f"Tâches semaine {week_start}")
    for t in tasks_week:
        claimer_ids = json.loads(t.get("claimed_by") or "[]")
        claimer_names = ", ".join([f"<@{i}>" for i in claimer_ids]) or "(personne)"
        embed.add_field(name=f"#{t['id']} {t['title']}", value=f"Due {t['due_date']} — Claimers: {claimer_names}", inline=False)
    
    view = View(timeout=None)

    for t in tasks_week:
        task_id = t['id']
        btn_claim = Button(label=f"Claim #{task_id}", style=discord.ButtonStyle.secondary, custom_id=f"claim_{task_id}")
        btn_complete = Button(label=f"Complete #{task_id}", style=discord.ButtonStyle.success, custom_id=f"complete_{task_id}")

        async def make_claim_callback(interaction, task_id=task_id):
            claimed = claim_task(task_id, interaction.user.id)
            ann = get_announcement_for_message(interaction.message.id)
            if not ann:
                wk = week_start
            else:
                wk = ann['week_start']
            await refresh_announcement(interaction.message, wk)
            await interaction.response.send_message(f"Vous avez claimé la tâche #{task_id}.", ephemeral=True)

        async def make_complete_callback(interaction, task_id=task_id):
            tdata = get_task(task_id)
            claimed = json.loads(tdata.get('claimed_by') or '[]')
            if interaction.user.id in claimed or interaction.user.guild_permissions.manage_messages:
                complete_task(task_id)
                await refresh_announcement(interaction.message, week_start)
                await interaction.response.send_message(f"Tâche #{task_id} marquée comme complétée.", ephemeral=True)
            else:
                await interaction.response.send_message("Vous devez être claimé sur cette tâche pour la marquer comme complétée.", ephemeral=True)
        
        btn_claim.callback = make_claim_callback
        btn_complete.callback = make_complete_callback
        view.add_item(btn_claim)
        view.add_item(btn_complete)
    
    message = await ctx.send(embed=embed, view=view)
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO announcements (message_id, week_start) VALUES (?, ?)", (message.id, week_start))
    conn.commit()

def get_announcement_for_message(message_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM annoucements WHERE message_id = ?", (message_id, ))
    row = c.fetchone()
    return dict(row) if row else None

async def refresh_announcement(message, week_start):
    tasks_week = get_tasks_for_week(week_start)
    embed = discord.Embed(title=f"Tâches semaine {week_start}")
    for t in tasks_week:
        claimer_ids = json.loads(t.get("claimed_by") or "[]")
        claimer_names = ", ".join([f"<@{i}>" for i in claimer_ids]) or "(personne)"
        status = "✅" if t.get("completed") else "🔲"
        embed.add_field(name=f"#{t['id']} {status} {t['title']}", value=f"Due {t['due_date']} — Claimers: {claimer_names}", inline=False)
    
    view = View(timeout=None)
    for t in [tt for tt in tasks_week if tt.get('completed') == 0]:
        task_id = t['id']
        btn_claim = Button(label=f"Claim #{task_id}", style=discord.ButtonStyle.secondary, custom_id=f"claim_{task_id}")
        btn_complete = Button(label=f"Complete #{task_id}", style=discord.ButtonStyle.success, custom_id=f"complete_{task_id}")

        async def make_claim_callback(interaction, task_id=task_id):
            claim_task(task_id, interaction.user.id)
            await refresh_announcement(interaction.message, week_start)
            await interaction.response.send_message(f"Vous avez claimé la tâche #{task_id}.", ephemeral=True)
        
        async def make_complete_callback(interaction, task_id=task_id):
            tdata = get_task(task_id)
            claimed = json.loads(tdata.get('claimed_by') or '[]')
            if interaction.user.id in claimed or interaction.user.guild_permissions.manage_messages:
                complete_task(task_id)
                await refresh_announcement(interaction.message, week_start)
                await interaction.response.send_message(f"Tâche #{task_id} marquée comme complétée.", ephemeral=True)
            else:
                await interaction.response.send_message("Vous devez être claimé pour marquer cette tâche comme complétée.", ephemeral=True)
        
        btn_claim.callback = make_claim_callback
        btn_complete.callback = make_complete_callback
        view.add_item(btn_claim)
        view.add_item(btn_complete)

    try:
        await message.edit(embed=embed, view=view)
    except Exception as e:
        logger.exception("Impossible d'éditer le message d'annonce: %s", e)

@bot.command(name="complete")
async def _complete(ctx, task_id: int):
    t = get_task(task_id)
    if not t:
        await ctx.send("Tâche introuvable.")
        return
    claimed = json.loads(t.get('claimed_by') or '[]')
    if ctx.author.id in claimed or ctx.author.guild_permissions.manage_messages:
        complete_task(task_id)
        await ctx.send(f"Tâche #{task_id} marquée comme complétée.")
    else:
        await ctx.send("Vous devez être claimé pour compléter cette tâche.")

@bot.command(name="deletetask")
async def _deletetask(ctx, task_id: int):
    if not ctx.author.guild_permissions.manage_messages:
        await ctx.send("❌ Vous n'avez pas la permission de supprimer des tâches.")
        return
    
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id, ))
    row = c.fetchone()
    if not row:
        await ctx.send(f"❌ Tâche #{task_id} introuvable.")
        return
    
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id, ))
    conn.commit()
    await ctx.send(f"🗑️ Tâche #{task_id} supprimée avec succès.")

@bot.command(name="gantt")
async def _gantt(ctx):
    if PROJECT_CHANNEL_ID and ctx.channel.id != PROJECT_CHANNEL_ID:
        await ctx.send(f"La commande gantt doit être utilisée dans le salon dédié (ID {PROJECT_CHANNEL_ID}).")
        return
    if not os.path.exists(GANTT_IMAGE_PATH):
        await ctx.send("Erreur: l'image de Gantt n'est pas disponible.")
        return
    img_bytes = overlay_gantt_with_today(GANTT_IMAGE_PATH, GANTT_START, GANTT_END)
    await ctx.send(file=discord.File(fp=img_bytes, filename="gantt.png"))

@bot.command(name="progress")
async def _progress(ctx, week_start: str = None):
    if week_start is None:
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
    tasks_week = get_tasks_for_week(week_start)
    if not tasks_week:
        await ctx.send("Aucune tâche pour cette semaine.")
        return
    total = len(tasks_week)
    done = sum(1 for t in tasks_week if t.get('completed'))
    pct = int(done / total * 100) if total else 0
    bar_length = 20
    filled = int(bar_length * done / total) if total else 0
    bar = "█" * filled + "▁" * (bar_length - filled)
    await ctx.send(f"Progression semaine {week_start}: {done}/{total} ({pct}%)\n{bar}")

@tasks.loop(minutes=REMINDER_LOOP_MINUTES)
async def check_deadlines():
    logger.info("Vérification des échéances...")
    conn = get_conn()
    c = conn.cursor()
    now = datetime.utcnow()
    soon = now + timedelta(hours=REMINDER_HOURS)
    c.execute("SELECT * FROM tasks WHERE completed = 0")
    rows = c.fetchall()
    for r in rows:
        due = None
        try:
            due = datetime.fromisoformat(r['due_date'])
        except Exception:
            try:
                due = datetime.strptime(r['due_date'], "%Y-%m-%d")
            except Exception:
                continue
        if now <= due <= soon:
            claimed = json.loads(r['claimed_by'] or '[]')
            channel = bot.get_channel(PROJECT_CHANNEL_ID)
            if channel is None:
                logger.warning("Salon de projet introuvable (ID %s)", PROJECT_CHANNEL_ID)
                continue
            if claimed:
                mentions = " ".join([f"<@{i}>" for i in claimed])
                await channel.send(f"Rappel: la tâche #{r['id']} **{r['title']}** est due le {r['due_date']} — {mentions}")
            else:
                await channel.send(f"Rappel: la tâche #{r['id']} **{r['title']}** est due le {r['due_date']} — personne ne s'est encore positionné.")

def run():
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN non configuré. Mettez la variable d'environnement DISCORD_TOKEN avec le token du bot.")
        return
    init_db()
    bot.run(DISCORD_TOKEN)

if __name__ == '__main__':
    run()