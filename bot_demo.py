import json
import logging
import os
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import threading
import time
import queue
import io
import qrcode
from PIL import Image
import tempfile
import shutil
import mysql.connector
import requests
import hashlib
import hmac
from flask import Flask, jsonify
import re

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
    config = load_config()
    user = update.effective_user
    
    # Verificar se captura de leads está habilitada
    lead_capture = config.get('lead_capture', {})
    if lead_capture.get('enabled', False):
        # Verificar se usuário já tem dados de contato completos
        if lead_capture.get('skip_existing_users', True):
            db = DatabaseDemo()
            try:
                db.connect()
                result = db.execute_query("SELECT email, phone FROM users WHERE id = %s", (user.id,))
                if result:
                    user_data = result[0]
                    has_email = bool(user_data.get('email'))
                    has_phone = bool(user_data.get('phone'))
                    
                    # Verificar se precisa de e-mail e telefone
                    require_email = lead_capture.get('require_email', True)
                    require_phone = lead_capture.get('require_phone', True)
                    
                    # Determinar se está completo
                    email_ok = not require_email or has_email
                    phone_ok = not require_phone or has_phone
                    is_complete = email_ok and phone_ok
                    
                    if is_complete:
                        logger.info(f"ℹ️ Usuário {user.id} já tem dados completos - pulando captura")
                        # Usuário já tem dados completos, continuar normalmente
                        await process_start_normal(update, context)
                        return
            except Exception as e:
                logger.error(f"Erro ao verificar dados existentes: {e}")
            finally:
                db.close()
        
        # Iniciar captura de dados
        await start_lead_capture(update, context)
        return
    
    # Se captura não estiver habilitada, continuar normalmente
    await process_start_normal(update, context)

async def start_lead_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia o processo de captura de dados do lead"""
    config = load_config()
    lead_capture = config.get('lead_capture', {})
    messages = lead_capture.get('messages', {})
    
    user = update.effective_user
    
    # Salvar usuário básico primeiro (sem enviar webhook)
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
        logger.info(f"ℹ️ Usuário {user.id} salvo no banco (sem webhook)")
            
    except Exception as e:
        print(f"Erro ao salvar usuário no banco: {e}")
    finally:
        db.close()
    
    # Configurar estado de captura
    context.user_data['capturing_lead'] = True
    context.user_data['lead_step'] = 'welcome'
    
    # Enviar mensagem de boas-vindas
    Welcome_msg = messages.get('welcome', '👋 Olá! Para continuar seu registro, preciso de algumas informações:') 
    # Criar teclado com botões para captura de dados
    keyboard = [
        [KeyboardButton("📱 Compartilhar Contato", request_contact=True)],
        [KeyboardButton("📧 Enviar E-mail")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(Welcome_msg, reply_markup=reply_markup)

async def process_start_normal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa o comando start normalmente (sem captura de leads)"""
    # Salvar usuário na database para remarketing (sem enviar webhook)
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
        logger.info(f"ℹ️ Usuário {user.id} salvo no banco (sem webhook)")
        
    except Exception as e:
        print(f"Erro ao salvar usuário no banco: {e}")
    finally:
        db.close()
    
    # Continuar com o fluxo normal
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
    config = load_config()
    if str(update.effective_user.id) != str(config['admin_id']):
        await update.message.reply_text("Acesso negado.")
        return
    user_id = update.effective_user.id
    MEMORY_USERS_VIP.discard(user_id)
    await update.message.reply_text("✅ Simulação de remoção do VIP concluída. Use /vip para verificar.")

# Handler para /testarwebhook
async def testarwebhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para testar o webhook"""
    config = load_config()
    if str(update.effective_user.id) != str(config['admin_id']):
        await update.message.reply_text("Acesso negado.")
        return
    
    webhook_config = config.get('webhook', {})
    
    if not webhook_config.get('enabled', False):
        await update.message.reply_text("❌ Webhook está desabilitado na configuração.")
        return
    
    url = webhook_config.get('url')
    if not url:
        await update.message.reply_text("❌ URL do webhook não configurada.")
        return
    
    # Enviar webhook de teste
    test_data = {
        "user_id": update.effective_user.id,
        "username": update.effective_user.username,
        "first_name": update.effective_user.first_name,
        "last_name": update.effective_user.last_name,
        "test": True,
        "message": "Teste manual do webhook"
    }
    
    try:
        await send_webhook("user_start", test_data)
        await update.message.reply_text(
            f"✅ Webhook de teste enviado!\n\n"
            f"📤 URL: {url}\n"
            f"📋 Evento: user_start\n"
            f"📊 Dados: {len(test_data)} campos"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao enviar webhook: {str(e)}")

# Handler para /testarleads
async def testarleads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para testar o sistema de captura de leads"""
    config = load_config()
    
    if update.effective_user.id != config['admin_id']:
        await update.message.reply_text("Acesso negado.")
        return
    
    user_id = update.effective_user.id
    
    # Verificar dados no banco
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query("SELECT * FROM users WHERE id = %s", (user_id,))
        
        if result:
            user_data = result[0]
            report = f"📊 **Dados do usuário {user_id}:**\n\n"
            report += f"👤 **Informações básicas:**\n"
            report += f"• Nome: {user_data.get('first_name', 'N/A')} {user_data.get('last_name', '')}\n"
            report += f"• Username: @{user_data.get('username', 'N/A')}\n"
            report += f"• Data de entrada: {user_data.get('joined_date', 'N/A')}\n"
            report += f"• VIP: {'✅' if user_data.get('is_vip') else '❌'}\n\n"
            
            report += f"📧 **Dados de contato:**\n"
            report += f"• E-mail: {user_data.get('email', '❌ Não informado')}\n"
            report += f"• Telefone: {user_data.get('phone', '❌ Não informado')}\n\n"
            
            # Verificar se tem dados completos
            has_email = bool(user_data.get('email'))
            has_phone = bool(user_data.get('phone'))
            
            report += f"📋 **Status da captura:**\n"
            report += f"• E-mail: {'✅ Capturado' if has_email else '❌ Faltando'}\n"
            report += f"• Telefone: {'✅ Capturado' if has_phone else '❌ Faltando'}\n"
            report += f"• Completo: {'✅ Sim' if (has_email and has_phone) else '❌ Não'}\n\n"
            
            # Verificar configuração
            lead_capture = config.get('lead_capture', {})
            require_email = lead_capture.get('require_email', True)
            require_phone = lead_capture.get('require_phone', True)
            
            report += f"⚙️ **Configuração:**\n"
            report += f"• E-mail obrigatório: {'✅ Sim' if require_email else '❌ Não'}\n"
            report += f"• Telefone obrigatório: {'✅ Sim' if require_phone else '❌ Não'}\n\n"
            
            # Botões de ação
            keyboard = [
                [InlineKeyboardButton("🗑️ Limpar Dados de Contato", callback_data="clear_contact_data")],
                [InlineKeyboardButton("🔄 Testar Captura Novamente", callback_data="test_capture_again")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(report, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Usuário {user_id} não encontrado no banco de dados.")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao verificar dados: {e}")
    finally:
        db.close()

# Comando /vip
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in MEMORY_USERS_VIP:
        await update.message.reply_text("🎉 Você é VIP (DEMO)! Aproveite o acesso aos recursos exclusivos.")
    else:
        await update.message.reply_text("Você não é VIP ainda. Use /start para simular uma assinatura.")

# Comando /meusdados
async def meusdados(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para mostrar dados pessoais do usuário"""
    user_id = update.effective_user.id
    
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query("SELECT id, first_name, username, joined_date, email, phone FROM users WHERE id = %s", (user_id,))
        
        if result:
            user_data = result[0]  # Primeira linha do resultado (dicionário)
            report = f"""📋 **MEUS DADOS**

🆔 **ID do Usuário:** `{user_data['id']}`
👤 **Nome:** {user_data['first_name'] if user_data['first_name'] else 'Não informado'}
🔗 **Username:** @{user_data['username'] if user_data['username'] else 'Não informado'}
📅 **Data de Entrada:** {user_data['joined_date'].strftime('%d/%m/%Y %H:%M') if user_data['joined_date'] else 'Não registrada'}
💎 **Status VIP:** {'✅ Sim' if user_id in MEMORY_USERS_VIP else '❌ Não'}
📧 **E-mail:** {user_data['email'] if user_data['email'] else 'Não informado'}
📱 **Telefone:** {user_data['phone'] if user_data['phone'] else 'Não informado'}"""
            
            # Botões para alterar dados
            keyboard = [
                [InlineKeyboardButton("📧 Alterar E-mail", callback_data="alterar_email")],
                [InlineKeyboardButton("📱 Alterar Telefone", callback_data="alterar_telefone")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(report, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Usuário não encontrado no banco de dados.")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar dados: {e}")
        logger.error(f"Erro ao buscar dados do usuário {user_id}: {e}")
    finally:
        db.close()

# Comando /alteraremail
async def alteraremail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para alterar e-mail do usuário"""
    user_id = update.effective_user.id
    
    # Verificar se usuário existe
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query("SELECT email FROM users WHERE id = %s", (user_id,))
        
        if result:
            current_email = result[0]['email'] if result[0]['email'] else 'Não informado'
            
            # Configurar estado para captura de e-mail
            context.user_data['alterando_email'] = True
            
            keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_alteracao")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"📧 **Alterar E-mail**\n\n"
                f"E-mail atual: {current_email}\n\n"
                f"Digite seu novo e-mail:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Usuário não encontrado. Use /start primeiro.")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")
    finally:
        db.close()

# Comando /alterarnumero
async def alterarnumero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para alterar telefone do usuário"""
    user_id = update.effective_user.id
    
    # Verificar se usuário existe
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query("SELECT phone FROM users WHERE id = %s", (user_id,))
        
        if result:
            current_phone = result[0]['phone'] if result[0]['phone'] else 'Não informado'
            
            # Configurar estado para captura de telefone
            context.user_data['alterando_telefone'] = True
            
            keyboard = [
                [InlineKeyboardButton("📱 Compartilhar Contato", request_contact=True)],
                [InlineKeyboardButton("✏️ Digitar Manualmente", callback_data="digitar_telefone")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_alteracao")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            
            await update.message.reply_text(
                f"📱 **Alterar Telefone**\n\n"
                f"Telefone atual: {current_phone}\n\n"
                f"Escolha como deseja informar o novo telefone:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("❌ Usuário não encontrado. Use /start primeiro.")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: {e}")
    finally:
        db.close()

# Comando /ajuda
async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para listar todos os comandos disponíveis"""
    config = load_config()
    admin_id = config.get('admin_id')
    user_id = update.effective_user.id
    is_admin = user_id == admin_id
    
    help_text = f"🤖 **Comandos Disponíveis**\n\n"
    
    help_text += f"📋 **Comandos Gerais:**\n"
    help_text += f"• `/start` - Iniciar o bot e ver planos VIP\n"
    help_text += f"• `/vip` - Verificar status VIP\n"
    help_text += f"• `/meusdados` - Ver seus dados pessoais\n"
    help_text += f"• `/alteraremail` - Alterar seu e-mail\n"
    help_text += f"• `/alterarnumero` - Alterar seu telefone\n"
    help_text += f"• `/ajuda` - Mostrar esta lista de comandos\n\n"
    
    if is_admin:
        help_text += f"🔧 **Comandos de Administrador:**\n"
        help_text += f"• `/admin` - Painel administrativo\n"
        help_text += f"• `/testarbroadcast` - Testar broadcast\n"
        help_text += f"• `/testarnotificacao` - Testar notificações\n"
        help_text += f"• `/testarremocao` - Testar remoção de usuários\n"
        help_text += f"• `/testarwebhook` - Testar webhook\n"
        help_text += f"• `/testarleads` - Testar sistema de leads\n\n"
    
    help_text += f"📞 **Suporte:**\n"
    help_text += f"Para suporte, entre em contato com @{config.get('admin_user', 'admin')}\n\n"
    
    help_text += f"ℹ️ **Informações:**\n"
    help_text += f"• Este é um bot de demonstração\n"
    help_text += f"• Os pagamentos são simulados\n"
    help_text += f"• Seus dados são armazenados com segurança"
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

# Comando /admin
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    admin_id = config.get('admin_id')
    admin_user = config.get('admin_user')
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Verificar se é o admin (por ID e username)
    is_admin = (user_id == admin_id) and (username == admin_user)
    
    if not is_admin:
        await update.message.reply_text("❌ Acesso negado. Apenas administradores podem usar este comando.")
        return
    
    # Menu básico para todos os usuários cadastrados
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Usuários", callback_data="admin_users")],
        [InlineKeyboardButton("💎 Planos", callback_data="admin_plans")]
    ]
    
    # Apenas admin pode ver opções de broadcast e mídia
    if is_admin:
        keyboard.append([InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")])
        keyboard.append([InlineKeyboardButton("🖼️ Anexar Mídia Welcome", callback_data="admin_attach_welcome_media")])
    
    keyboard.append([InlineKeyboardButton("📝 Editar Mensagens", callback_data="admin_edit_messages")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("🔧 Painel de Administração (DEMO)", reply_markup=reply_markup)

# Handler de callback do admin com menu de broadcast DEMO
async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    config = load_config()
    admin_id = config.get('admin_id')
    admin_user = config.get('admin_user')
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Verificar se é o admin (por ID e username)
    is_admin = (user_id == admin_id) and (username == admin_user)
    
    if not is_admin:
        await query.message.edit_text("❌ Acesso negado. Apenas administradores podem usar este painel.")
        return
    
    # Handler para limpar dados de contato
    if query.data == "clear_contact_data":
        user_id = update.effective_user.id
        db = DatabaseDemo()
        try:
            db.connect()
            db.execute("UPDATE users SET email = NULL, phone = NULL WHERE id = %s", (user_id,))
            await query.message.edit_text("✅ Dados de contato limpos! Use /start para testar a captura novamente.")
            logger.info(f"🗑️ Dados de contato limpos para usuário {user_id}")
        except Exception as e:
            await query.message.edit_text(f"❌ Erro ao limpar dados: {e}")
        finally:
            db.close()
        return
    
    # Handler para testar captura novamente
    elif query.data == "test_capture_again":
        user_id = update.effective_user.id
        db = DatabaseDemo()
        try:
            db.connect()
            db.execute("UPDATE users SET email = NULL, phone = NULL WHERE id = %s", (user_id,))
            await query.message.edit_text("✅ Dados limpos! Agora use /start para testar a captura novamente.")
            logger.info(f"🔄 Dados limpos para teste de captura - usuário {user_id}")
        except Exception as e:
            await query.message.edit_text(f"❌ Erro ao limpar dados: {e}")
        finally:
            db.close()
        return
    
    # Handler para anexar mídia de boas-vindas
    elif query.data == "admin_attach_welcome_media":
        context.user_data['waiting_for_welcome_media'] = True
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "🖼️ Anexar Mídia de Boas-vindas\n\n"
            "Envie uma foto ou vídeo que será usado como mídia de boas-vindas.\n\n"
            "⚠️ O arquivo deve ser menor que 50MB.",
            reply_markup=reply_markup
        )
        return
    
    # Handler para usar legenda padrão
    elif query.data == "admin_use_default_welcome_caption":
        context.user_data['waiting_for_welcome_caption'] = False
        file_id = context.user_data.get('welcome_file_id')
        file_type = context.user_data.get('welcome_file_type')
        if file_id and file_type:
            config = load_config()
            config['welcome_file'] = {
                'file_id': file_id,
                'file_type': file_type,
                'caption': 'Bem-vindo ao Bot VIP! 🎉'
            }
            try:
                ok = save_config(config)
                if not ok:
                    await query.message.edit_text("❌ Erro ao salvar mídia de boas-vindas.")
                else:
                    await query.message.edit_text("✅ Mídia de boas-vindas salva com sucesso!")
            except Exception as e:
                await query.message.edit_text(f"❌ Erro ao salvar mídia de boas-vindas: {e}")
        else:
            await query.message.edit_text("❌ Erro ao salvar mídia de boas-vindas.")
        context.user_data.pop('welcome_file_id', None)
        context.user_data.pop('welcome_file_type', None)
        context.user_data.pop('waiting_for_welcome_media', None)
        return
    
    # Handler para voltar ao menu principal
    elif query.data == "admin_back":
        keyboard = [
            [InlineKeyboardButton("📊 Estatísticas", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 Usuários", callback_data="admin_users")],
            [InlineKeyboardButton("💎 Planos", callback_data="admin_plans")]
        ]
        # Verificar se é admin (ID + username)
        admin_id = config.get('admin_id')
        admin_user = config.get('admin_user')
        user_id = update.effective_user.id
        username = update.effective_user.username
        is_admin = (user_id == admin_id) and (username == admin_user)
        
        if is_admin:
            keyboard.append([InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")])
            keyboard.append([InlineKeyboardButton("🖼️ Anexar Mídia Welcome", callback_data="admin_attach_welcome_media")])
        keyboard.append([InlineKeyboardButton("📝 Editar Mensagens", callback_data="admin_edit_messages")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text("🔧 Painel de Administração (DEMO)", reply_markup=reply_markup)
        return
    
    # Handler para estatísticas
    elif query.data == "admin_stats":
        all_users = get_all_users()
        stats_text = f"📊 **Estatísticas do Bot (DEMO)**\n\n"
        stats_text += f"👥 Total de usuários: {len(all_users)}\n"
        stats_text += f"💎 Usuários VIP: {len([u for u in all_users if u.get('is_vip')])}\n"
        stats_text += f"📅 Última atualização: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        stats_text += "👤 **Últimos usuários:**\n"
        for user in all_users[:5]:
            stats_text += f"• ID: {user['id']}, Nome: {user.get('first_name', 'N/A')}, VIP: {'✅' if user.get('is_vip') else '❌'}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(stats_text, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    # Handler para usuários
    elif query.data == "admin_users":
        all_users = get_all_users()
        users_text = f"👥 **Usuários do Bot (DEMO)**\n\n"
        users_text += f"Total: {len(all_users)} usuários\n\n"
        for user in all_users[:10]:  # Mostrar apenas os primeiros 10
            users_text += f"• ID: {user['id']}\n"
            users_text += f"  Nome: {user.get('first_name', 'N/A')} {user.get('last_name', '')}\n"
            users_text += f"  Username: @{user.get('username', 'N/A')}\n"
            users_text += f"  VIP: {'✅' if user.get('is_vip') else '❌'}\n"
            users_text += f"  Data: {user.get('joined_date', 'N/A')}\n\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(users_text, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    # Handler para planos
    elif query.data == "admin_plans":
        config = load_config()
        plans = config.get('plans', [])
        plans_text = f"💎 **Planos VIP (DEMO)**\n\n"
        for plan in plans:
            plans_text += f"• **{plan['name']}**\n"
            plans_text += f"  💰 Preço: R$ {plan['price']:.2f}\n"
            plans_text += f"  ⏱️ Duração: {plan['duration_days']} dias\n"
            plans_text += f"  📝 Descrição: {plan.get('description', 'N/A')}\n\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(plans_text, reply_markup=reply_markup, parse_mode='Markdown')
        return
    
    # Handler para editar mensagens (menu fixo igual bot.py)
    elif query.data == "admin_edit_messages":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [
            [InlineKeyboardButton("👋 Mensagem de Boas-vindas", callback_data="admin_edit_welcome_message")],
            [InlineKeyboardButton("💎 Mensagem de Pagamento", callback_data="admin_edit_payment_message")],
            [InlineKeyboardButton("✅ Mensagem de Sucesso", callback_data="admin_edit_success_message")],
            [InlineKeyboardButton("❌ Mensagem de Erro", callback_data="admin_edit_error_message")],
            [InlineKeyboardButton("📝 Instruções PIX", callback_data="admin_edit_pix_instructions")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = "📝 Mensagens do Bot (DEMO)\n\nMensagens atuais:\n\n"
        text += f"👋 Boas-vindas: {messages.get('welcome', 'Não definida')[:50]}...\n\n"
        text += f"💎 Pagamento: {messages.get('payment_instructions', 'Não definida')[:50]}...\n\n"
        text += f"✅ Sucesso: {messages.get('payment_success', 'Não definida')[:50]}...\n\n"
        text += f"❌ Erro: {messages.get('payment_error', 'Não definida')[:50]}...\n\n"
        text += f"📝 PIX: {messages.get('pix_automatico_instructions', 'Não definida')[:50]}...\n\n"
        text += "Escolha uma mensagem para editar:"
        await query.message.edit_text(text, reply_markup=reply_markup)
        return
    elif query.data == "admin_edit_welcome_message":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_edit_messages")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "👋 Editar Mensagem de Boas-vindas\n\n"
            f"Mensagem atual:\n{messages.get('welcome', 'Não definida')}\n\n"
            "Envie a nova mensagem de boas-vindas:",
            reply_markup=reply_markup
        )
        context.user_data['editing_message'] = 'welcome'
        return
    elif query.data == "admin_edit_payment_message":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_edit_messages")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "💎 Editar Mensagem de Pagamento\n\n"
            f"Mensagem atual:\n{messages.get('payment_instructions', 'Não definida')}\n\n"
            "Envie a nova mensagem de pagamento:",
            reply_markup=reply_markup
        )
        context.user_data['editing_message'] = 'payment_instructions'
        return
    elif query.data == "admin_edit_success_message":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_edit_messages")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "✅ Editar Mensagem de Sucesso\n\n"
            f"Mensagem atual:\n{messages.get('payment_success', 'Não definida')}\n\n"
            "Envie a nova mensagem de sucesso:",
            reply_markup=reply_markup
        )
        context.user_data['editing_message'] = 'payment_success'
        return
    elif query.data == "admin_edit_error_message":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_edit_messages")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "❌ Editar Mensagem de Erro\n\n"
            f"Mensagem atual:\n{messages.get('payment_error', 'Não definida')}\n\n"
            "Envie a nova mensagem de erro:",
            reply_markup=reply_markup
        )
        context.user_data['editing_message'] = 'payment_error'
        return
    elif query.data == "admin_edit_pix_instructions":
        config = load_config()
        messages = config.get('messages', {})
        keyboard = [[InlineKeyboardButton("⬅️ Voltar", callback_data="admin_edit_messages")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📝 Editar Instruções PIX\n\n"
            f"Mensagem atual:\n{messages.get('pix_automatico_instructions', 'Não definida')}\n\n"
            "Envie a nova mensagem de instruções PIX:",
            reply_markup=reply_markup
        )
        context.user_data['editing_message'] = 'pix_automatico_instructions'
        return
    
    # Handler para broadcast
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
            "⭕ Vídeo Circular: Formato quadrado (videonote)\n\n"
            "⚠️ Apenas administradores podem usar esta função.",
            reply_markup=reply_markup
        )
        return
    
    # Handler para broadcast de texto para todos
    elif query.data == "admin_broadcast_all":
        context.user_data['broadcast_type'] = 'all'
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📢 Enviar mensagem para todos os usuários\n\n"
            "Digite a mensagem que deseja enviar:",
            reply_markup=reply_markup
        )
        return
    
    # Handler para broadcast de vídeo para todos
    elif query.data == "admin_broadcast_video_all":
        context.user_data['broadcast_type'] = 'video_all'
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "📹 Enviar vídeo para todos os usuários\n\n"
            "Primeiro, envie o vídeo que deseja compartilhar:",
            reply_markup=reply_markup
        )
        return
    
    # Handler para broadcast de vídeo circular para todos
    elif query.data == "admin_broadcast_videonote_all":
        context.user_data['broadcast_type'] = 'videonote_all'
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="admin_broadcast")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text(
            "⭕ Enviar vídeo circular para todos os usuários\n\n"
            "📋 Requisitos do vídeo circular:\n"
            "• Formato quadrado (ex: 240x240)\n"
            "• Duração máxima: 60 segundos\n"
            "• Será exibido como círculo no app\n\n"
            "Envie o vídeo que deseja compartilhar:",
            reply_markup=reply_markup
        )
        return
    
    # Handler para alterar e-mail
    elif query.data == "alterar_email":
        user_id = update.effective_user.id
        context.user_data['alterando_email'] = True
        
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_alteracao")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "📧 **Alterar E-mail**\n\n"
            "Digite seu novo e-mail:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Handler para alterar telefone
    elif query.data == "alterar_telefone":
        user_id = update.effective_user.id
        context.user_data['alterando_telefone'] = True
        
        keyboard = [
            [InlineKeyboardButton("📱 Compartilhar Contato", callback_data="compartilhar_contato")],
            [InlineKeyboardButton("✏️ Digitar Manualmente", callback_data="digitar_telefone")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_alteracao")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "📱 **Alterar Telefone**\n\n"
            "Escolha como deseja informar o novo telefone:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Handler para digitar telefone manualmente
    elif query.data == "digitar_telefone":
        user_id = update.effective_user.id
        context.user_data['alterando_telefone'] = True
        context.user_data['digitando_telefone'] = True
        
        keyboard = [[InlineKeyboardButton("❌ Cancelar", callback_data="cancelar_alteracao")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "📱 **Digitar Telefone**\n\n"
            "Digite seu telefone no formato: (11) 99999-9999",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    
    # Handler para compartilhar contato
    elif query.data == "compartilhar_contato":
        user_id = update.effective_user.id
        context.user_data['alterando_telefone'] = True
        
        keyboard = [
            [KeyboardButton("📱 Compartilhar Contato", request_contact=True)],
            [KeyboardButton("❌ Cancelar")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        
        await query.message.edit_text(
            "📱 **Compartilhar Contato**\n\n"
            "Clique no botão abaixo para compartilhar seu contato:",
            parse_mode='Markdown'
        )
        
        # Enviar mensagem com teclado para compartilhar contato
        await query.message.reply_text(
            "Use o botão abaixo para compartilhar seu contato:",
            reply_markup=reply_markup
        )
        return
    
    # Handler para cancelar alteração
    elif query.data == "cancelar_alteracao":
        # Limpar estados de alteração
        context.user_data.pop('alterando_email', None)
        context.user_data.pop('alterando_telefone', None)
        context.user_data.pop('digitando_telefone', None)
        
        await query.message.edit_text("❌ Alteração cancelada.")
        return

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

# =====================================================
# FUNÇÕES DE WEBHOOK
# =====================================================

async def send_webhook(event_type, data):
    """Envia dados para webhook externo se configurado"""
    try:
        config = load_config()
        webhook_config = config.get('webhook', {})
        
        # Verificar se webhook está habilitado
        if not webhook_config.get('enabled', False):
            return
        
        # Verificar se o evento está habilitado
        events = webhook_config.get('events', {})
        if not events.get(event_type, False):
            return
        
        url = webhook_config.get('url')
        if not url:
            logger.warning("Webhook habilitado mas URL não configurada")
            return
        
        # Preparar payload
        payload = {
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "bot_id": config.get('bot_token', '').split(':')[0] if config.get('bot_token') else None,
            "data": data
        }
        
        # Headers
        headers = webhook_config.get('headers', {})
        headers.setdefault('Content-Type', 'application/json')
        
        # Timeout
        timeout = webhook_config.get('timeout', 10)
        
        # Enviar webhook
        logger.info(f"📤 Enviando webhook {event_type} para {url}")
        
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout
        )
        
        if response.status_code in [200, 201, 202]:
            logger.info(f"✅ Webhook {event_type} enviado com sucesso")
        else:
            logger.error(f"❌ Erro ao enviar webhook {event_type}: {response.status_code} - {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar webhook {event_type}: {e}")

def send_webhook_sync(event_type, data):
    """Versão síncrona para enviar webhook (para uso em threads)"""
    try:
        config = load_config()
        webhook_config = config.get('webhook', {})
        
        # Verificar se webhook está habilitado
        if not webhook_config.get('enabled', False):
            return
        
        # Verificar se o evento está habilitado
        events = webhook_config.get('events', {})
        if not events.get(event_type, False):
            return
        
        url = webhook_config.get('url')
        if not url:
            return
        
        # Preparar payload
        payload = {
            "event_type": event_type,
            "timestamp": datetime.now().isoformat(),
            "bot_id": config.get('bot_token', '').split(':')[0] if config.get('bot_token') else None,
            "data": data
        }
        
        # Headers
        headers = webhook_config.get('headers', {})
        headers.setdefault('Content-Type', 'application/json')
        
        # Timeout
        timeout = webhook_config.get('timeout', 10)
        
        # Enviar webhook
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout
        )
        
        if response.status_code in [200, 201, 202]:
            logger.info(f"✅ Webhook {event_type} enviado com sucesso (sync)")
        else:
            logger.error(f"❌ Erro ao enviar webhook {event_type}: {response.status_code}")
            
    except Exception as e:
        logger.error(f"❌ Erro ao enviar webhook {event_type} (sync): {e}")

# =====================================================
# FIM DAS FUNÇÕES DE WEBHOOK
# =====================================================

# =====================================================
# FUNÇÕES DE CAPTURA DE LEADS
# =====================================================

def validate_email(email):
    """Valida formato de e-mail"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_phone(phone):
    """Valida formato de telefone brasileiro"""
    # Remove todos os caracteres não numéricos
    phone_clean = re.sub(r'[^\d]', '', phone)
    
    # Verifica se tem 10 ou 11 dígitos (com DDD)
    if len(phone_clean) not in [10, 11]:
        return False
    
    # Verifica se começa com DDD válido (11-99)
    ddd = int(phone_clean[:2])
    if ddd < 11 or ddd > 99:
        return False
    
    return True

def format_phone(phone):
    """Formata telefone para padrão brasileiro"""
    phone_clean = re.sub(r'[^\d]', '', phone)
    
    if len(phone_clean) == 11:
        return f"({phone_clean[:2]}) {phone_clean[2:7]}-{phone_clean[7:]}"
    elif len(phone_clean) == 10:
        return f"({phone_clean[:2]}) {phone_clean[2:6]}-{phone_clean[6:]}"
    else:
        return phone

def check_user_has_contact_data(user_id):
    """Verifica se usuário já tem dados de contato salvos"""
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query(
            "SELECT email, phone FROM users WHERE id = %s",
            (user_id,)
        )
        if result:
            user_data = result[0]
            return bool(user_data.get('email') or user_data.get('phone'))
        return False
    except Exception as e:
        logger.error(f"Erro ao verificar dados de contato: {e}")
        return False
    finally:
        db.close()

def save_user_contact_data(user_id, email=None, phone=None):
    """Salva dados de contato do usuário"""
    db = DatabaseDemo()
    try:
        db.connect()
        
        # Preparar campos para atualização
        update_fields = []
        params = []
        
        if email:
            update_fields.append("email = %s")
            params.append(email)
        
        if phone:
            update_fields.append("phone = %s")
            params.append(phone)
        
        if update_fields:
            params.append(user_id)
            query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = %s"
            db.execute(query, params)
            
            logger.info(f"Dados de contato salvos para usuário {user_id}: email={email}, phone={phone}")
            return True
            
    except Exception as e:
        logger.error(f"Erro ao salvar dados de contato: {e}")
        return False
    finally:
        db.close()

# =====================================================
# FIM DAS FUNÇÕES DE CAPTURA DE LEADS
# =====================================================

# =====================================================
# HANDLERS DE CAPTURA DE LEADS
# =====================================================

async def handle_contact_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para contatos compartilhados"""
    user_id = update.effective_user.id
    contact = update.message.contact
    
    # Verificar se está alterando telefone
    if context.user_data.get('alterando_telefone', False):
        # Processar alteração de telefone
        phone = contact.phone_number
        formatted_phone = format_phone(phone)
        
        # Salvar no banco de dados
        db = DatabaseDemo()
        try:
            db.connect()
            db.execute("UPDATE users SET phone = %s WHERE id = %s", (formatted_phone, user_id))
            
            # Limpar estado
            context.user_data.pop('alterando_telefone', None)
            context.user_data.pop('digitando_telefone', None)
            
            await update.message.reply_text(
                f"✅ Telefone alterado com sucesso!\n\n"
                f"📱 Novo telefone: {formatted_phone}\n"
                f"👤 Nome: {contact.first_name} {contact.last_name or ''}",
                reply_markup=ReplyKeyboardRemove()
            )
            
            logger.info(f"📱 Telefone alterado via contato para usuário {user_id}: {formatted_phone}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Erro ao alterar telefone: {e}")
            logger.error(f"Erro ao alterar telefone: {e}")
        finally:
            db.close()
        return
    
    # Processar contato durante captura de leads
    if context.user_data.get('capturing_lead', False):
        phone = contact.phone_number
        formatted_phone = format_phone(phone)
        
        # Salvar telefone no banco
        db = DatabaseDemo()
        try:
            db.connect()
            db.execute("UPDATE users SET phone = %s WHERE id = %s", (formatted_phone, user_id))
            
            # Verificar se precisa de e-mail
            config = load_config()
            lead_capture = config.get('lead_capture', {})
            require_email = lead_capture.get('require_email', True)
            
            if require_email:
                # Ainda precisa de e-mail
                context.user_data['lead_step'] = 'email'
                messages = lead_capture.get('messages', {})
                await update.message.reply_text(
                    f"✅ Telefone salvo: {formatted_phone}\n\n"
                    f"{messages.get('email_request', '📧 Agora envie seu e-mail:')}",
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                # Não precisa de e-mail, finalizar captura
                await finish_lead_capture(update, context)
                
        except Exception as e:
            await update.message.reply_text(f"❌ Erro ao salvar telefone: {e}")
            logger.error(f"Erro ao salvar telefone: {e}")
        finally:
            db.close()
    else:
        await update.message.reply_text("❌ Compartilhamento de contato não solicitado.")

async def handle_capture_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para callback de captura de e-mail"""
    query = update.callback_query
    await query.answer()
    
    if not context.user_data.get('capturing_lead', False):
        return
    
    config = load_config()
    lead_capture = config.get('lead_capture', {})
    messages = lead_capture.get('messages', {})
    
    context.user_data['lead_step'] = 'email'
    await query.message.edit_text(messages.get('email_request', '📧 Por favor, envie seu e-mail:'))

async def handle_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para entrada de e-mail"""
    if not context.user_data.get('capturing_lead', False) or context.user_data.get('lead_step') != 'email':
        return
    
    config = load_config()
    lead_capture = config.get('lead_capture', {})
    messages = lead_capture.get('messages', {})
    
    email = update.message.text.strip()
    user_id = update.effective_user.id
    
    # Validar e-mail
    if not validate_email(email):
        await update.message.reply_text(messages.get('invalid_email', '❌ E-mail inválido. Tente novamente:'))
        return
    
    # Salvar e-mail
    save_user_contact_data(user_id, email=email)
    logger.info(f"📧 E-mail capturado para usuário {user_id}: {email}")
    
    # Verificar se precisa capturar telefone
    if lead_capture.get('require_phone', True):
        # Verificar se já tem telefone
        db = DatabaseDemo()
        try:
            db.connect()
            result = db.execute_query("SELECT phone FROM users WHERE id = %s", (user_id,))
            has_phone = result and result[0].get('phone')
        finally:
            db.close()
        
        if not has_phone:
            # Solicitar telefone
            context.user_data['lead_step'] = 'phone'
            await update.message.reply_text(messages.get('phone_request', '📱 Agora envie seu telefone (com DDD):'))
            return
    
    # Dados completos, finalizar captura
    await finish_lead_capture(update, context)

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para entrada de telefone"""
    if not context.user_data.get('capturing_lead', False) or context.user_data.get('lead_step') != 'phone':
        return
    
    config = load_config()
    lead_capture = config.get('lead_capture', {})
    messages = lead_capture.get('messages', {})
    
    phone = update.message.text.strip()
    user_id = update.effective_user.id
    
    # Validar telefone
    if not validate_phone(phone):
        await update.message.reply_text(messages.get('invalid_phone', '❌ Telefone inválido. Use formato: (11) 99999-9999'))
        return
    
    # Formatar e salvar telefone
    formatted_phone = format_phone(phone)
    save_user_contact_data(user_id, phone=formatted_phone)
    logger.info(f"📱 Telefone capturado para usuário {user_id}: {formatted_phone}")
    
    # Finalizar captura
    await finish_lead_capture(update, context)

async def finish_lead_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza o processo de captura de leads"""
    config = load_config()
    lead_capture = config.get('lead_capture', {})
    messages = lead_capture.get('messages', {})
    
    user_id = update.effective_user.id
    
    # Buscar dados salvos
    db = DatabaseDemo()
    try:
        db.connect()
        result = db.execute_query("SELECT email, phone FROM users WHERE id = %s", (user_id,))
        user_data = result[0] if result else {}
        
        # Verificar se tem TODOS os dados de contato necessários
        has_email = bool(user_data.get('email'))
        has_phone = bool(user_data.get('phone'))
        
        # Verificar se precisa de e-mail e telefone
        require_email = lead_capture.get('require_email', True)
        require_phone = lead_capture.get('require_phone', True)
        
        # Determinar se está completo
        email_ok = not require_email or has_email
        phone_ok = not require_phone or has_phone
        is_complete = email_ok and phone_ok
        
        # SEMPRE enviar webhook quando a captura for finalizada
        webhook_data = {
            "user_id": user_id,
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "email": user_data.get('email'),
            "phone": user_data.get('phone'),
            "lead_capture_completed": True,
            "contact_data_complete": is_complete,
            "has_email": has_email,
            "has_phone": has_phone,
            "require_email": require_email,
            "require_phone": require_phone,
            "database_checked": True
        }
        await send_webhook("user_start", webhook_data)
        
        if is_complete:
            logger.info(f"✅ Lead completo para usuário {user_id} - webhook enviado")
        else:
            logger.info(f"ℹ️ Lead incompleto para usuário {user_id} - webhook enviado mesmo assim")
            logger.info(f"   Email: {'✅' if has_email else '❌'} (requerido: {require_email})")
            logger.info(f"   Phone: {'✅' if has_phone else '❌'} (requerido: {require_phone})")
            
    except Exception as e:
        logger.error(f"Erro ao buscar dados de contato: {e}")
        # Enviar webhook mesmo com erro
        webhook_data = {
            "user_id": user_id,
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "lead_capture_completed": True,
            "contact_data_complete": False,
            "error": str(e),
            "database_error": True
        }
        await send_webhook("user_start", webhook_data)
    finally:
        db.close()
    
    # Limpar estado de captura
    context.user_data.pop('capturing_lead', None)
    context.user_data.pop('lead_step', None)
    
    # Mensagem de sucesso
    success_msg = messages.get('success', '✅ Seu Cadastro foi Concluido! Agora vamos aos planos VIP:')
    await update.message.reply_text(success_msg, reply_markup=ReplyKeyboardRemove())
    
    # Continuar com o fluxo normal
    await process_start_normal(update, context)

# =====================================================
# FIM DOS HANDLERS DE CAPTURA DE LEADS
# =====================================================

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler unificado para entrada de texto"""
    # Verificar se está alterando dados pessoais
    if context.user_data.get('alterando_email', False):
        await handle_alterar_email(update, context)
        return
    
    if context.user_data.get('alterando_telefone', False):
        await handle_alterar_telefone(update, context)
        return
    
    # Verificar se está capturando leads
    if context.user_data.get('capturing_lead', False):
        lead_step = context.user_data.get('lead_step')
        text = update.message.text.strip()
        
        # Processar botões do teclado de captura
        if lead_step == 'welcome':
            if text == "📱 Compartilhar Contato":
                # O contato será processado pelo handler de CONTACT
                await update.message.reply_text("📱 Por favor, toque no botão 'Compartilhar Contato' para enviar seu telefone.")
                return
            elif text == "📧 Enviar E-mail":
                context.user_data['lead_step'] = 'email'
                config = load_config()
                messages = config.get('lead_capture', {}).get('messages', {})
                await update.message.reply_text(
                    messages.get('email_request', '📧 Por favor, envie seu e-mail:'),
                    reply_markup=ReplyKeyboardRemove()
                )
                return
            else:
                # Texto não reconhecido, mostrar opções novamente
                config = load_config()
                messages = config.get('lead_capture', {}).get('messages', {})
                keyboard = [
                    [KeyboardButton("📱 Compartilhar Contato", request_contact=True)],
                    [KeyboardButton("📧 Enviar E-mail")]
                ]
                reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
                await update.message.reply_text(
                    "Por favor, escolha uma das opções abaixo:",
                    reply_markup=reply_markup
                )
                return
        
        elif lead_step == 'email':
            await handle_email_input(update, context)
            return
        elif lead_step == 'phone':
            await handle_phone_input(update, context)
            return
    
    # Se não está capturando leads, usar handler de admin
    await handle_admin_text(update, context)

# Handler para alterar e-mail
async def handle_alterar_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para processar alteração de e-mail"""
    user_id = update.effective_user.id
    email = update.message.text.strip()
    
    # Validar e-mail
    if not validate_email(email):
        await update.message.reply_text("❌ E-mail inválido. Tente novamente:")
        return
    
    # Salvar no banco de dados
    db = DatabaseDemo()
    try:
        db.connect()
        db.execute("UPDATE users SET email = %s WHERE id = %s", (email, user_id))
        
        # Limpar estado
        context.user_data.pop('alterando_email', None)
        
        await update.message.reply_text(
            f"✅ E-mail alterado com sucesso!\n\n"
            f"📧 Novo e-mail: {email}",
            reply_markup=ReplyKeyboardRemove()
        )
        
        logger.info(f"📧 E-mail alterado para usuário {user_id}: {email}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao alterar e-mail: {e}")
        logger.error(f"Erro ao alterar e-mail: {e}")
    finally:
        db.close()

# Handler para alterar telefone
async def handle_alterar_telefone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para processar alteração de telefone"""
    user_id = update.effective_user.id
    phone = update.message.text.strip()
    
    # Validar telefone
    if not validate_phone(phone):
        await update.message.reply_text("❌ Telefone inválido. Use formato: (11) 99999-9999")
        return
    
    # Formatar telefone
    formatted_phone = format_phone(phone)
    
    # Salvar no banco de dados
    db = DatabaseDemo()
    try:
        db.connect()
        db.execute("UPDATE users SET phone = %s WHERE id = %s", (formatted_phone, user_id))
        
        # Limpar estado
        context.user_data.pop('alterando_telefone', None)
        context.user_data.pop('digitando_telefone', None)
        
        await update.message.reply_text(
            f"✅ Telefone alterado com sucesso!\n\n"
            f"📱 Novo telefone: {formatted_phone}",
            reply_markup=ReplyKeyboardRemove()
        )
        
        logger.info(f"📱 Telefone alterado para usuário {user_id}: {formatted_phone}")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao alterar telefone: {e}")
        logger.error(f"Erro ao alterar telefone: {e}")
    finally:
        db.close()

# Handler para texto do admin adaptado para edição de mensagens (igual bot.py)
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
        # ... fluxo broadcast ...
        pass
    # Fluxo de edição de mensagens (igual bot.py)
    if context.user_data.get('editing_message'):
        key = context.user_data.get('editing_message')
        new_text = update.message.text.strip()
        config['messages'][key] = new_text
        # Se for a mensagem de welcome, atualize também a legenda da mídia de boas-vindas
        if key == 'welcome' and 'welcome_file' in config:
            config['welcome_file']['caption'] = new_text
        save_config(config)
        await update.message.reply_text(f"Mensagem '{key}' atualizada com sucesso!")
        context.user_data['editing_message'] = None
        # Voltar ao menu de mensagens
        messages = config.get('messages', {})
        keyboard = [
            [InlineKeyboardButton("👋 Mensagem de Boas-vindas", callback_data="admin_edit_welcome_message")],
            [InlineKeyboardButton("💎 Mensagem de Pagamento", callback_data="admin_edit_payment_message")],
            [InlineKeyboardButton("✅ Mensagem de Sucesso", callback_data="admin_edit_success_message")],
            [InlineKeyboardButton("❌ Mensagem de Erro", callback_data="admin_edit_error_message")],
            [InlineKeyboardButton("📝 Instruções PIX", callback_data="admin_edit_pix_instructions")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="admin_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = "📝 Mensagens do Bot (DEMO)\n\nMensagens atuais:\n\n"
        text += f"👋 Boas-vindas: {messages.get('welcome', 'Não definida')[:50]}...\n\n"
        text += f"💎 Pagamento: {messages.get('payment_instructions', 'Não definida')[:50]}...\n\n"
        text += f"✅ Sucesso: {messages.get('payment_success', 'Não definida')[:50]}...\n\n"
        text += f"❌ Erro: {messages.get('payment_error', 'Não definida')[:50]}...\n\n"
        text += f"📝 PIX: {messages.get('pix_automatico_instructions', 'Não definida')[:50]}...\n\n"
        text += "Escolha uma mensagem para editar:"
        await update.message.reply_text(text, reply_markup=reply_markup)
        return
    # ... resto do handler ...

# Handlers

def main():
    config = load_config()
    token = config.get('bot_token')
    if not token:
        return
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("vip", vip))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(CommandHandler("meusdados", meusdados))
    application.add_handler(CommandHandler("alteraremail", alteraremail))
    application.add_handler(CommandHandler("alterarnumero", alterarnumero))
    application.add_handler(CommandHandler("ajuda", ajuda))
    application.add_handler(CallbackQueryHandler(handle_plan_selection, pattern="^plan_"))
    application.add_handler(CallbackQueryHandler(handle_pix_demo, pattern="^pix_demo_"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin_"))
    
    # Handlers para renovação e pagamento
    application.add_handler(CallbackQueryHandler(handle_plan_selection, pattern="^renew_"))
    application.add_handler(CallbackQueryHandler(handle_pix_demo, pattern="^demo_paid_"))
    
    # Handlers de captura de leads
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact_shared))
    application.add_handler(CallbackQueryHandler(handle_capture_email_callback, pattern="^capture_email$"))
    
    # Handlers para botões de teste
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^clear_contact_data$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^test_capture_again$"))
    
    # Handlers para alteração de dados pessoais
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^alterar_email$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^alterar_telefone$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^digitar_telefone$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^compartilhar_contato$"))
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^cancelar_alteracao$"))
    
    # Handler unificado de texto
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    
    application.add_handler(CommandHandler("testarbroadcast", testarbroadcast))
    application.add_handler(CommandHandler("testarnotificacao", testarnotificacao))
    application.add_handler(CommandHandler("testarremocao", testarremocao))
    application.add_handler(CommandHandler("testarwebhook", testarwebhook))
    application.add_handler(CommandHandler("testarleads", testarleads))
    # Adicionar handler para vídeos normais e circulares
    application.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_admin_files))
    # Adicionar handler para fotos
    application.add_handler(MessageHandler(filters.PHOTO, handle_admin_files))
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