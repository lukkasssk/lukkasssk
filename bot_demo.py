import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import threading
import time
import queue
import os
import io
import qrcode
from PIL import Image
import tempfile
import shutil
import json
import mysql.connector

# Banco de dados real, mas só SELECT para usuários
from database import Database

CONFIG_FILE = 'config_demo.json'

# Simulação de pagamentos e VIP em memória
MEMORY_USERS_VIP = set()
MEMORY_PAYMENTS = {}

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Utilitários de configuração

def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Erro ao carregar config_demo.json: {e}")
        return {}

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Erro ao salvar config_demo.json: {e}")
        return False

# Utilitários de banco (apenas leitura para usuários)
def get_all_users():
    db = Database()
    try:
        db.connect()
        if not db.connection:
            return []
        return db.execute_fetch_all("SELECT id, username, first_name, last_name FROM users")
    except Exception as e:
        logger.error(f"Erro ao buscar usuários: {e}")
        return []
    finally:
        db.close()

# Função para salvar assinatura demo no JSON
# Agora permite múltiplas assinaturas ativas por usuário/plano

def add_subscription_demo(user_id, plan_id):
    config = load_config()
    if 'subscriptions' not in config:
        config['subscriptions'] = []
    # Não remove mais assinaturas antigas: permite múltiplas
    # Verifica se já existe assinatura ativa deste plano para o usuário
    existing = [s for s in config['subscriptions'] if s['user_id'] == user_id and s['plan_id'] == plan_id]
    if existing:
        # Se já existe, não adiciona de novo (ou pode renovar, se quiser)
        return
    # Busca plano
    plan = next((p for p in config.get('plans', []) if p['id'] == plan_id), None)
    if not plan:
        return
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if plan['duration_days'] == -1:
        end_date = '2099-12-31 23:59:59'
    else:
        end_dt = datetime.now() + timedelta(days=plan['duration_days'])
        end_date = end_dt.strftime('%Y-%m-%d %H:%M:%S')
    config['subscriptions'].append({
        'user_id': user_id,
        'plan_id': plan_id,
        'plan_name': plan['name'],
        'start_date': now,
        'end_date': end_date,
        'is_permanent': plan['duration_days'] == -1
    })
    save_config(config)

# Função para buscar todas assinaturas ativas do usuário

def get_active_subscriptions_demo(user_id):
    config = load_config()
    subs = config.get('subscriptions', [])
    now = datetime.now()
    result = []
    for s in subs:
        if s['user_id'] == user_id:
            if s['is_permanent']:
                result.append(s)
            else:
                try:
                    if datetime.strptime(s['end_date'], '%Y-%m-%d %H:%M:%S') > now:
                        result.append(s)
                except:
                    continue
    return result

# Comando /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Salvar usuário na database para remarketing
    user = update.effective_user
    db = DatabaseDemo()
    db.connect()
    try:
        # Tenta inserir, se já existir faz update do nome/username
        db.execute(
            '''INSERT INTO users (id, username, first_name, last_name, joined_date)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE username=VALUES(username), first_name=VALUES(first_name), last_name=VALUES(last_name)''',
            (user.id, user.username, user.first_name, user.last_name, datetime.now())
        )
    except Exception as e:
        print(f"Erro ao salvar usuário no banco: {e}")
    finally:
        db.close()
    config = load_config()
    # Enviar mídia de boas-vindas se configurada
    welcome_file = config.get('welcome_file')
    if welcome_file and welcome_file.get('file_id'):
        file_id = welcome_file['file_id']
        file_type = welcome_file.get('file_type', 'photo')
        caption = welcome_file.get('caption', '')
        try:
            if file_type == 'photo':
                await update.message.reply_photo(photo=file_id, caption=caption)
            elif file_type == 'video':
                await update.message.reply_video(video=file_id, caption=caption)
        except Exception as e:
            logger.error(f"Erro ao enviar mídia de boas-vindas: {e}")
    user_id = update.effective_user.id
    subs = get_active_subscriptions_demo(user_id)
    plans = config.get('plans', [])
    if subs:
        msg = "✨ Você já é VIP!\n\n"
        user_plan_ids = set()
        keyboard = []
        for sub in subs:
            end_date = sub['end_date']
            plan_name = sub['plan_name']
            is_permanent = sub.get('is_permanent', False)
            user_plan_ids.add(sub['plan_id'])
            days_left = None
            msg += f"Plano: {plan_name}\n"
            if is_permanent:
                msg += "Duração: Permanente\n"
            else:
                try:
                    dt_end = datetime.strptime(end_date, '%Y-%m-%d %H:%M:%S')
                    days_left = (dt_end - datetime.now()).days
                    msg += f"Dias restantes: {days_left}\n"
                except:
                    msg += f"Expira em: {end_date}\n"
            # Botão de renovação se <=3 dias e não permanente
            if days_left is not None and days_left <= 3 and not is_permanent:
                keyboard.append([InlineKeyboardButton(f"🔄 Renovar {plan_name}", callback_data=f"renew_{sub['plan_id']}")])
            msg += "\n"
        # Botões para adquirir outros planos que o usuário ainda não tem
        other_plans = [p for p in plans if p['id'] not in user_plan_ids]
        for plan in other_plans:
            keyboard.append([InlineKeyboardButton(f"💎 {plan['name']} - R${plan['price']}", callback_data=f"plan_{plan['id']}")])
        if keyboard:
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(msg, reply_markup=reply_markup)
        else:
            await update.message.reply_text(msg)
        return
    if not plans:
        await update.message.reply_text("Nenhum plano disponível no momento.")
        return
    keyboard = [[InlineKeyboardButton(f"💎 {plan['name']} - R${plan['price']}", callback_data=f"plan_{plan['id']}")] for plan in plans]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Escolha um dos planos VIP disponíveis:", reply_markup=reply_markup)

# Seleção de plano
async def handle_plan_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    query = update.callback_query
    await query.answer()
    plan_id = int(query.data.split('_')[1])
    plans = config.get('plans', [])
    plan = next((p for p in plans if p['id'] == plan_id), None)
    if not plan:
        await query.message.reply_text("Plano não encontrado.")
        return
    keyboard = [[InlineKeyboardButton("💳 PIX (Simulado)", callback_data=f"pix_demo_{plan_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.edit_text(
        f"💎 Plano: {plan['name']}\n💰 Valor: R${plan['price']}\n⏱️ Duração: {'Permanente' if plan['duration_days']==-1 else str(plan['duration_days'])+' dias'}\n\n*DEMO*: Nenhum pagamento é real.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# Simulação de pagamento PIX
def gerar_pix_fake(user_id, plan_id):
    config = load_config()
    payment_id = f"demo_{user_id}_{plan_id}_{int(time.time())}"
    MEMORY_PAYMENTS[payment_id] = {
        'user_id': user_id,
        'plan_id': plan_id,
        'status': 'pending',
        'created_at': datetime.now()
    }
    qr_code = config.get('pix_demo_qrcode', "00020126360014BR.GOV.BCB.PIX0114+55119999999952040000530398654041.00")
    return payment_id, qr_code

async def handle_pix_demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    query = update.callback_query
    await query.answer()
    plan_id = int(query.data.split('_')[2])
    user_id = query.from_user.id
    payment_id, qr_code = gerar_pix_fake(user_id, plan_id)
    # Buscar valor do plano
    plan = next((p for p in config.get('plans', []) if p['id'] == plan_id), None)
    valor = plan['price'] if plan else '---'
    # Aprovação automática após 5s
    asyncio.create_task(aprovar_pagamento_demo(payment_id, user_id, plan_id, context))
    # Template completo na legenda da foto
    legenda = (
        f"Escaneie o QR Code abaixo para pagar automaticamente:\n\n"
        f"💰 Valor: R${valor:.2f}\n\n"
        f"📋 Código PIX para copiar:\n"
        f"<code>{qr_code}</code>\n\n"
        f"📱 Como pagar:\n"
        f"1. Escaneie o QR Code acima, OU\n"
        f"2. Copie o código PIX acima e cole no app do seu banco\n\n"
        f"⏳ Aguardando pagamento..."
    )
    img = qrcode.make(qr_code)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    keyboard = [[InlineKeyboardButton("✅ Já Paguei", callback_data=f"demo_paid_{payment_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    sent = await context.bot.send_photo(chat_id=user_id, photo=buf, caption=legenda, parse_mode='HTML', reply_markup=reply_markup)
    # Salva o message_id da foto no contexto do usuário
    if 'pix_qr_messages' not in context.bot_data:
        context.bot_data['pix_qr_messages'] = {}
    context.bot_data['pix_qr_messages'][user_id] = sent.message_id

async def aprovar_pagamento_demo(payment_id, user_id, plan_id, context):
    config = load_config()
    await asyncio.sleep(3)
    MEMORY_PAYMENTS[payment_id]['status'] = 'approved'
    MEMORY_USERS_VIP.add(user_id)
    add_subscription_demo(user_id, plan_id)
    # Deleta a mensagem do QR Code, se possível
    qr_messages = context.bot_data.get('pix_qr_messages', {})
    msg_id = qr_messages.get(user_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception as e:
            logger.error(f"Erro ao deletar mensagem do QR Code: {e}")
    await context.bot.send_message(chat_id=user_id, text=config.get('messages', {}).get('payment_success', "✅ Pagamento aprovado! Você agora é VIP (DEMO). Aproveite para testar as funções VIP."))
    # Enviar links dos grupos VIP do plano
    plan = next((p for p in config.get('plans', []) if p['id'] == plan_id), None)
    if plan:
        grupos = config.get('vip_groups', [])
        grupos_ativos = [g for g in grupos if g.get('is_active')]
        if grupos_ativos:
            msg = '🎉 <b>Acesso VIP Liberado!</b>\n\n<b>Grupos VIP do seu plano:</b>\n'
            for g in grupos_ativos:
                nome = g.get('name', 'Grupo VIP')
                group_link = await get_group_invite_link(context.bot, g)
                msg += f'• <b>{nome}</b>: <a href="{group_link}">{group_link}</a>\n'
            msg += '\n⚠️ Estes links são apenas para demonstração.'
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='HTML', disable_web_page_preview=True)
    # Enviar comandos de teste
    comandos = (
        "\n<b>Testes disponíveis no DEMO:</b>\n"
        "• /testarbroadcast — Simula o envio de broadcast para todos.\n"
        "• /testarnotificacao — Simula notificação de renovação de assinatura.\n"
        "• /testarremocao — Simula remoção do VIP (expiração).\n"
        "\nUse os comandos acima para testar as funções administrativas do bot demo."
    )
    await context.bot.send_message(chat_id=user_id, text=comandos, parse_mode='HTML')

# Função para gerar link de convite para um grupo
async def generate_invite_link(bot, group_id):
    try:
        # Tenta criar um link de convite para o grupo
        chat_invite_link = await bot.create_chat_invite_link(
            chat_id=group_id,
            creates_join_request=False,
            expire_date=None,  # Link não expira
            member_limit=None  # Sem limite de membros
        )
        return chat_invite_link.invite_link
    except Exception as e:
        logger.error(f"Erro ao gerar link de convite para grupo {group_id}: {e}")
        # Se não conseguir gerar, retorna um link de fallback
        return f"https://t.me/c/{abs(group_id)}"

# Função para obter ou gerar link de convite para um grupo
async def get_group_invite_link(bot, group):
    group_id = group.get('group_id')
    
    # Tenta gerar um novo link de convite
    if group_id:
        return await generate_invite_link(bot, group_id)
    
    # Fallback
    return "https://t.me/"

# Handler para /testarbroadcast
def get_all_users_ids():
    users = get_all_users()
    return [u['id'] for u in users]

async def testarbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users_ids()
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text="[DEMO BROADCAST] Mensagem de teste de broadcast!")
        except:
            pass
    await update.message.reply_text("Broadcast de teste enviado para todos os usuários (DEMO).")

# Handler para /testarnotificacao
async def testarnotificacao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sub = get_active_subscriptions_demo(user_id)
    if sub:
        for dias in [3, 2, 1]:
            await update.message.reply_text(
                f"⚠️ Sua assinatura VIP está próxima de expirar!\n"
                f"Plano: {sub[0]['plan_name']}\n"
                f"Dias restantes: {dias}\n"
                f"Data de expiração: {sub[0]['end_date']}\n\n"
                f"Para renovar seu acesso VIP, use /start e escolha um novo plano! 🎉"
            )
            await asyncio.sleep(1)
        # Após a última notificação, simular remoção por falta de pagamento
        config = load_config()
        subs = config.get('subscriptions', [])
        config['subscriptions'] = [s for s in subs if s['user_id'] != user_id]
        save_config(config)
        if user_id in MEMORY_USERS_VIP:
            MEMORY_USERS_VIP.remove(user_id)
        await update.message.reply_text("🚫 Sua assinatura VIP foi expirada/removida por falta de pagamento (DEMO). Use /start para simular uma nova compra.")
    else:
        await update.message.reply_text("Você não possui assinatura VIP ativa para testar notificação.")

# Handler para /testarremocao
async def testarremocao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_config()
    # Remove assinatura do JSON
    subs = config.get('subscriptions', [])
    config['subscriptions'] = [s for s in subs if s['user_id'] != user_id]
    save_config(config)
    if user_id in MEMORY_USERS_VIP:
        MEMORY_USERS_VIP.remove(user_id)
    await update.message.reply_text("🚫 Sua assinatura VIP foi expirada/removida (DEMO). Use /start para simular uma nova compra.")

# Comando /vip
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in MEMORY_USERS_VIP:
        await update.message.reply_text("🎉 Você é VIP (DEMO)! Aproveite o acesso aos recursos exclusivos.")
    else:
        await update.message.reply_text("Você não é VIP ainda. Use /start para simular uma assinatura.")

# Comando /admin
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    admin_id = config.get('admin_id')
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Usuários", callback_data="admin_users")],
        [InlineKeyboardButton("💎 Planos", callback_data="admin_plans")]
    ]
    if user_id == admin_id:
        keyboard.append([InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")])
        keyboard.append([InlineKeyboardButton("🖼️ Anexar Mídia Welcome", callback_data="admin_attach_welcome_media")])
    keyboard.append([InlineKeyboardButton("📝 Editar Mensagens", callback_data="admin_edit_messages")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Painel Admin (DEMO)", reply_markup=reply_markup)

# Handler de callback do admin com menu de broadcast DEMO
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    query = update.callback_query
    # Só bloqueia se for menu de broadcast
    if query.data in ["admin_broadcast", "admin_broadcast_all", "admin_broadcast_video_all", "admin_broadcast_videonote_all"]:
        admin_id = config.get('admin_id')
        user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
        if user_id != admin_id:
            await update.callback_query.message.reply_text("Acesso negado.")
            return
    await query.answer()
    if query.data == "admin_stats":
        users = get_all_users()
        vips = 0  # Ajuste para DEMO
        await query.message.edit_text(f"Usuários cadastrados: {len(users)}\nVIPs (DEMO): {vips}")
    elif query.data == "admin_users":
        users = get_all_users()
        text = "Usuários:\n" + "\n".join([f"{u['id']} - {u['first_name']}" for u in users[:10]])
        await query.message.edit_text(text)
    elif query.data == "admin_plans":
        plans = config.get('plans', [])
        text = "Planos:\n" + "\n".join([f"{p['id']} - {p['name']} R${p['price']}" for p in plans])
        await query.message.edit_text(text)
    elif query.data == "admin_broadcast":
        keyboard = [
            [InlineKeyboardButton("📢 Enviar para Todos", callback_data="admin_broadcast_all")],
            [InlineKeyboardButton("📹 Enviar Vídeo para Todos", callback_data="admin_broadcast_video_all")],
            [InlineKeyboardButton("⭕ Enviar Vídeo Circular para Todos", callback_data="admin_broadcast_videonote_all")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📢 Broadcast DEMO\n\nEscolha o tipo de broadcast:\n\n"
            "📹 Vídeo Normal: Formato retangular tradicional\n"
            "⭕ Vídeo Circular: Formato circular (video_note)",
            reply_markup=reply_markup
        )
    elif query.data == "admin_broadcast_all":
        context.user_data['broadcast_type'] = 'all'  # Sempre para todos
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📢 Enviar mensagem para todos os usuários (DEMO)\n\nDigite a mensagem que deseja enviar:",
            reply_markup=reply_markup
        )
    elif query.data == "admin_edit_messages":
        context.user_data['editing_message'] = True
        msgs = config.get('messages', {})
        text = "📝 Mensagens atuais:\n"
        for k, v in msgs.items():
            text += f"\n*{k}*: {v}"
        text += "\n\nDigite o nome da mensagem que deseja editar (ex: welcome, payment_instructions):"
        await query.message.edit_text(text)
    elif query.data == "admin_broadcast_video_all":
        context.user_data['broadcast_type'] = 'video_all'
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📹 Enviar vídeo para todos os usuários (DEMO)\n\nEnvie o vídeo que deseja compartilhar:",
            reply_markup=reply_markup
        )
    elif query.data == "admin_broadcast_videonote_all":
        context.user_data['broadcast_type'] = 'videonote_all'
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "⭕ Enviar vídeo circular para todos os usuários (DEMO)\n\nEnvie o vídeo circular (video_note) que deseja compartilhar:",
            reply_markup=reply_markup
        )
    elif query.data == "admin_attach_welcome_media":
        context.user_data['waiting_for_welcome_media'] = True
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "🖼️ Envie uma foto ou vídeo para ser usado como mídia de boas-vindas.\n\nVocê pode adicionar uma legenda após o envio da mídia.",
            reply_markup=reply_markup
        )
        return
    elif query.data == "admin_use_default_welcome_caption":
        config = load_config()
        welcome_text = config.get('messages', {}).get('welcome', '')
        file_id = context.user_data.get('welcome_file_id')
        file_type = context.user_data.get('welcome_file_type')
        if file_id and file_type:
            config['welcome_file'] = {
                'file_id': file_id,
                'file_type': file_type,
                'caption': welcome_text
            }
            ok = save_config(config)
            if not ok:
                await query.message.reply_text("❌ Erro ao salvar mídia de boas-vindas (save_config retornou False).")
            else:
                await query.message.reply_text("✅ Mídia de boas-vindas salva com sucesso usando a mensagem padrão!")
        else:
            await query.message.reply_text("❌ Erro ao salvar mídia de boas-vindas.")
        context.user_data.pop('welcome_file_id', None)
        context.user_data.pop('welcome_file_type', None)
        context.user_data.pop('waiting_for_welcome_caption', None)
        return

# Handler de texto do admin adaptado para broadcast DEMO
async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('waiting_for_welcome_caption'):
        await handle_admin_files(update, context)
        return
    config = load_config()
    # Só bloqueia se o fluxo for de broadcast
    if context.user_data.get('broadcast_type'):
        admin_id = config.get('admin_id')
        user_id = update.effective_user.id
        if user_id != admin_id:
            await update.message.reply_text("Acesso negado.")
            return
    # --- NOVO FLUXO BROADCAST DEMO ---
    if context.user_data.get('broadcast_type'):
        # Se está esperando texto da mensagem
        if not context.user_data.get('waiting_for_button_choice') and not context.user_data.get('waiting_for_button_text') and not context.user_data.get('waiting_for_button_url'):
            context.user_data['broadcast_message_text'] = update.message.text
            context.user_data['waiting_for_button_choice'] = True
            await update.message.reply_text('Deseja adicionar um botão de redirecionamento? (Sim/Não)')
            return
        # Se está esperando escolha do botão
        if context.user_data.get('waiting_for_button_choice'):
            escolha = update.message.text.strip().lower()
            if escolha in ['sim', 's', 'yes', 'y']:
                context.user_data['waiting_for_button_choice'] = False
                context.user_data['waiting_for_button_text'] = True
                await update.message.reply_text('Digite o texto do botão:')
                return
            elif escolha in ['não', 'nao', 'n', 'no']:
                context.user_data['waiting_for_button_choice'] = False
                context.user_data['button_text'] = None
                context.user_data['button_url'] = None
                # Chama o broadcast herdado do bot.py
                await enviar_broadcast(update, context)
                return
            else:
                await update.message.reply_text('Por favor, responda "Sim" ou "Não". Deseja adicionar um botão de redirecionamento?')
                return
        # Se está esperando texto do botão
        if context.user_data.get('waiting_for_button_text'):
            context.user_data['button_text'] = update.message.text.strip()
            context.user_data['waiting_for_button_text'] = False
            context.user_data['waiting_for_button_url'] = True
            await update.message.reply_text('Agora envie o link do botão (começando com https://):')
            return
        # Se está esperando link do botão
        if context.user_data.get('waiting_for_button_url'):
            url = update.message.text.strip()
            if not url.startswith('http'):
                await update.message.reply_text('O link deve começar com http:// ou https://. Tente novamente:')
                return
            context.user_data['button_url'] = url
            context.user_data['waiting_for_button_url'] = False
            # Chama o broadcast herdado do bot.py
            await enviar_broadcast(update, context)
            return
    # Fluxo antigo para edição de mensagens/configs
    if context.user_data.get('editing_message'):
        # Espera o nome da mensagem
        if 'editing_message_key' not in context.user_data:
            key = update.message.text.strip()
            if key not in config.get('messages', {}):
                await update.message.reply_text("Chave não encontrada. Tente novamente.")
                return
            context.user_data['editing_message_key'] = key
            await update.message.reply_text(f"Digite o novo texto para a mensagem '{key}':")
        else:
            key = context.user_data['editing_message_key']
            new_text = update.message.text.strip()
            config['messages'][key] = new_text
            # Se for a mensagem de welcome, atualize também a legenda da mídia de boas-vindas
            if key == 'welcome' and 'welcome_file' in config:
                config['welcome_file']['caption'] = new_text
            save_config(config)
            await update.message.reply_text(f"Mensagem '{key}' atualizada com sucesso!")
            context.user_data['editing_message'] = False
            context.user_data['editing_message_key'] = None

# Handler para receber vídeos no broadcast DEMO
async def handle_admin_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    admin_id = config.get('admin_id')
    user_id = update.effective_user.id
    if user_id != admin_id:
        await update.message.reply_text("Acesso negado.")
        return
    # Novo fluxo: recebendo mídia de boas-vindas
    if context.user_data.get('waiting_for_welcome_media'):
        file_id = None
        file_type = None
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            file_type = 'photo'
        elif update.message.video:
            file_id = update.message.video.file_id
            file_type = 'video'
        else:
            await update.message.reply_text("❌ Por favor, envie uma foto ou vídeo.")
            return
        context.user_data['welcome_file_id'] = file_id
        context.user_data['welcome_file_type'] = file_type
        context.user_data['waiting_for_welcome_media'] = False
        context.user_data['waiting_for_welcome_caption'] = True
        # Adicionar botão para usar mensagem padrão
        keyboard = [[InlineKeyboardButton("Usar mensagem padrão de boas-vindas", callback_data="admin_use_default_welcome_caption")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Agora envie a legenda que deseja para a mídia de boas-vindas (ou envie - para sem legenda):", reply_markup=reply_markup)
        return
    if context.user_data.get('waiting_for_welcome_caption'):
        caption = update.message.text.strip() if update.message.text else ''
        if caption == '-':
            caption = ''
        file_id = context.user_data.get('welcome_file_id')
        file_type = context.user_data.get('welcome_file_type')
        if file_id and file_type:
            config = load_config()
            config['welcome_file'] = {
                'file_id': file_id,
                'file_type': file_type,
                'caption': caption
            }
            try:
                ok = save_config(config)
                if not ok:
                    print('ERRO: Falha ao salvar config_demo.json (save_config retornou False)')
                    await update.message.reply_text("❌ Erro ao salvar mídia de boas-vindas (save_config retornou False).")
                else:
                    await update.message.reply_text("✅ Mídia de boas-vindas salva com sucesso!")
            except Exception as e:
                print(f'ERRO: Exceção ao salvar config_demo.json: {e}')
                await update.message.reply_text(f"❌ Erro ao salvar mídia de boas-vindas: {e}")
        else:
            await update.message.reply_text("❌ Erro ao salvar mídia de boas-vindas.")
        context.user_data.pop('welcome_file_id', None)
        context.user_data.pop('welcome_file_type', None)
        context.user_data.pop('waiting_for_welcome_caption', None)
        return
    if context.user_data.get('broadcast_type', '').startswith('video_') or context.user_data.get('broadcast_type', '').startswith('videonote_'):
        # Aceitar tanto vídeo normal quanto vídeo circular (video_note)
        if update.message.video or update.message.video_note:
            if update.message.video:
                video_file_id = update.message.video.file_id
                video_duration = update.message.video.duration
                video_size = update.message.video.file_size
                video_width = update.message.video.width
                video_height = update.message.video.height
                is_videonote = context.user_data['broadcast_type'].startswith('videonote_')
            else:  # video_note
                video_file_id = update.message.video_note.file_id
                video_duration = update.message.video_note.duration
                video_size = update.message.video_note.file_size
                video_width = update.message.video_note.length
                video_height = update.message.video_note.length
                is_videonote = True
            context.user_data['broadcast_video'] = {
                'file_id': video_file_id,
                'duration': video_duration,
                'size': video_size,
                'width': video_width,
                'height': video_height,
                'is_videonote': is_videonote
            }
            context.user_data['waiting_for_broadcast_text'] = True
            if is_videonote:
                await update.message.reply_text(
                    f"✅ Vídeo circular recebido! Agora digite o texto da mensagem que será enviada junto com o vídeo circular.")
            else:
                await update.message.reply_text(
                    f"✅ Vídeo recebido! Agora digite o texto da mensagem que será enviada junto com o vídeo.")
        else:
            await update.message.reply_text("❌ Por favor, envie um vídeo ou vídeo circular.")
        return
    # ... restante do handler ...

# Função auxiliar para enviar o broadcast usando os dados do contexto (adaptada para DEMO, só todos usuários)
async def enviar_broadcast(update, context):
    config = load_config()
    admin_id = config.get('admin_id')
    user_id = update.effective_user.id if hasattr(update, 'effective_user') and update.effective_user else update.message.from_user.id if hasattr(update, 'message') and update.message else None
    if user_id != admin_id:
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("Acesso negado.")
        elif hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text("Acesso negado.")
        return
    broadcast_type = context.user_data.get('broadcast_type')
    message_text = context.user_data.get('broadcast_message_text', '')
    button_text = context.user_data.get('button_text')
    button_url = context.user_data.get('button_url')
    try:
        all_users = get_all_users()
        recipients = [user['id'] for user in all_users]
        is_video_broadcast = broadcast_type.startswith('video_') or broadcast_type.startswith('videonote_')
        success_count = 0
        error_count = 0
        if is_video_broadcast and 'broadcast_video' in context.user_data:
            video_info = context.user_data['broadcast_video']
            video_file_id = video_info['file_id']
            is_videonote = video_info.get('is_videonote', False)
            video_type_text = "vídeo circular" if is_videonote else "vídeo"
            progress_message = await update.message.reply_text(
                f"📹 Enviando {video_type_text} + mensagem para {len(recipients)} usuários...\n"
                f"✅ Enviados: 0\n"
                f"❌ Erros: 0"
            )
            for user_id in recipients:
                try:
                    if is_videonote:
                        await context.bot.send_video_note(
                            chat_id=user_id,
                            video_note=video_file_id
                        )
                        if message_text.strip() or button_text:
                            if button_text and button_url:
                                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])
                                await context.bot.send_message(
                                    chat_id=user_id,
                                    text=message_text if message_text.strip() else button_text,
                                    reply_markup=reply_markup
                                )
                            else:
                                await context.bot.send_message(
                                    chat_id=user_id,
                                    text=message_text
                                )
                    else:
                        if button_text and button_url:
                            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])
                            await context.bot.send_video(
                                chat_id=user_id,
                                video=video_file_id,
                                caption=message_text,
                                reply_markup=reply_markup
                            )
                        else:
                            await context.bot.send_video(
                                chat_id=user_id,
                                video=video_file_id,
                                caption=message_text
                            )
                    success_count += 1
                except Exception as e:
                    logger.error(f"   ❌ Erro ao enviar {video_type_text} para {user_id}: {e}")
                    error_count += 1
                if (success_count + error_count) % 10 == 0:
                    await progress_message.edit_text(
                        f"📹 Enviando {video_type_text} + mensagem para {len(recipients)} usuários...\n"
                        f"✅ Enviados: {success_count}\n"
                        f"❌ Erros: {error_count}"
                    )
            await progress_message.edit_text(
                f"📹 Broadcast com {video_type_text} concluído!\n\n"
                f"✅ {video_type_text.title()}s enviados: {success_count}\n"
                f"❌ Erros: {error_count}\n\n"
                f"Tipo: Todos os usuários"
            )
            del context.user_data['broadcast_type']
            del context.user_data['broadcast_video']
            if 'waiting_for_broadcast_text' in context.user_data:
                del context.user_data['waiting_for_broadcast_text']
        else:
            progress_message = await update.message.reply_text(
                f"📢 Enviando mensagem para {len(recipients)} usuários...\n"
                f"✅ Enviados: 0\n"
                f"❌ Erros: 0"
            )
            for user_id in recipients:
                try:
                    if button_text and button_url:
                        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=message_text,
                            reply_markup=reply_markup
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=message_text
                        )
                    success_count += 1
                except Exception as e:
                    logger.error(f"Erro ao enviar mensagem para {user_id}: {e}")
                    error_count += 1
                if (success_count + error_count) % 10 == 0:
                    await progress_message.edit_text(
                        f"📢 Enviando mensagem para {len(recipients)} usuários...\n"
                        f"✅ Enviados: {success_count}\n"
                        f"❌ Erros: {error_count}"
                    )
            await progress_message.edit_text(
                f"📢 Broadcast concluído!\n\n"
                f"✅ Mensagens enviadas: {success_count}\n"
                f"❌ Erros: {error_count}\n\n"
                f"Tipo: Todos os usuários"
            )
            del context.user_data['broadcast_type']
            if 'waiting_for_broadcast_text' in context.user_data:
                del context.user_data['waiting_for_broadcast_text']
        # Voltar ao menu de broadcast
        keyboard = [
            [InlineKeyboardButton("📢 Enviar para Todos", callback_data="admin_broadcast_all")],
            [InlineKeyboardButton("📹 Enviar Vídeo para Todos", callback_data="admin_broadcast_video_all")],
            [InlineKeyboardButton("⭕ Enviar Vídeo Circular para Todos", callback_data="admin_broadcast_videonote_all")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📢 Broadcast DEMO\n\nEscolha o tipo de broadcast:\n\n"
            "📹 Vídeo Normal: Formato retangular tradicional\n"
            "⭕ Vídeo Circular: Formato circular (video_note)",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Erro ao realizar broadcast: {e}")
        await update.message.reply_text(
            f"❌ Erro ao realizar broadcast: {str(e)}\n\n"
            "Tente novamente mais tarde."
        )
        if 'broadcast_type' in context.user_data:
            del context.user_data['broadcast_type']
        if 'broadcast_video' in context.user_data:
            del context.user_data['broadcast_video']
        if 'waiting_for_broadcast_text' in context.user_data:
            del context.user_data['waiting_for_broadcast_text']

# Handlers

def main():
    config = load_config()
    token = config.get('bot_token')
    if not token:
        print('Configure o campo "bot_token" no config_demo.json')
        return
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("vip", vip))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CallbackQueryHandler(handle_plan_selection, pattern="^plan_"))
    application.add_handler(CallbackQueryHandler(handle_pix_demo, pattern="^pix_demo_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))
    application.add_handler(CommandHandler("testarbroadcast", testarbroadcast))
    application.add_handler(CommandHandler("testarnotificacao", testarnotificacao))
    application.add_handler(CommandHandler("testarremocao", testarremocao))
    # Adicionar handler para vídeos normais e circulares
    application.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_admin_files))
    application.run_polling()

class DatabaseDemo:
    def __init__(self):
        from json import load
        with open('config_demo.json', 'r', encoding='utf-8') as f:
            config = load(f)
        db_cfg = config.get('database', {})
        self.host = db_cfg.get('host', 'localhost')
        self.port = db_cfg.get('port', 3306)
        self.user = db_cfg.get('user', 'root')
        self.password = db_cfg.get('password', '')
        self.database = db_cfg.get('database', 'bot_demo')
        self.connection = None

    def connect(self):
        self.connection = mysql.connector.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database
        )
        return self.connection

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def execute_query(self, query, params=None):
        if not self.connection:
            self.connect()
        cursor = self.connection.cursor(dictionary=True)
        cursor.execute(query, params or ())
        result = cursor.fetchall()
        cursor.close()
        return result

    def execute(self, query, params=None):
        if not self.connection:
            self.connect()
        cursor = self.connection.cursor()
        cursor.execute(query, params or ())
        self.connection.commit()
        cursor.close()

# Exemplo de uso:
# db = DatabaseDemo()
# db.connect()
# db.execute('INSERT INTO ...')
# rows = db.execute_query('SELECT * FROM ...')
# db.close()

if __name__ == '__main__':
    main() 