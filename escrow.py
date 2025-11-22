import os
import asyncio
import random
import string
import hashlib
import sqlite3
import requests
from datetime import datetime, timedelta
from web3 import Web3
from telethon import TelegramClient, functions, types
from telethon.errors import SessionPasswordNeededError, AuthRestartError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from telegram.constants import ParseMode, ChatType
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes

# Configurable variables
UPDATES_CHANNEL = "https://t.me/PagaLEscrowUpdates"
VOUCHES_CHANNEL = "https://t.me/PagaLEscrowVouches"
BOT_USERNAME = "wingoaiprobot"  # Without '@' prefix
ADMIN_CONTACT = "@bsr_official"
TOKEN = "6833024552:AAE6FUn-KsONGWZam_0hyDGgpSjQfdq_-2I"

# Telethon configuration
API_ID = 24795598
API_HASH = "1bfb567f25febfbec2a8b34a37937828"
SESSION_DIR = "escrowsessions"
MAX_ACCOUNTS = 10

# Blockchain configuration
INFURA_URL = "https://mainnet.infura.io/v3/YOUR_INFURA_PROJECT_ID"
BSC_URL = "https://bsc-dataseed.binance.org/"
BLOCKCYPHER_TOKEN = "YOUR_BLOCKCYPHER_TOKEN"
ESCROW_FEE = 0.01  # 1% fee
MNEMONIC = "your twelve word seed phrase here"  # Replace with your actual mnemonic

# USDT Contracts
USDT_ERC20 = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_BEP20 = "0x55d398326f99059fF775485246999027B3197955"
erc20_abi = '[{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]'

# State management
ACCOUNT_MANAGER = {}
GROUP_CREATION_STATE = {}
LOGIN_STATES = {}
GROUP_STATES = {}  # Track group states for /dd command
GROUP_ESCROWS = {}  # Track escrow data per group: {chat_id: {token, network, address, key, status, amount, buyer, seller}}

# Blockchain clients
w3_eth = Web3(Web3.HTTPProvider(INFURA_URL))
w3_bsc = Web3(Web3.HTTPProvider(BSC_URL))

# Create session directory if not exists
os.makedirs(SESSION_DIR, exist_ok=True)

# Generate seed from mnemonic
seed_bytes = Bip39SeedGenerator(MNEMONIC).Generate()
bip44_mst = Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)

# Database connection pool
DB_CONNECTION_POOL = {}

def get_db_connection(user_id):
    """Create or get a database connection for a user"""
    if user_id not in DB_CONNECTION_POOL:
        DB_CONNECTION_POOL[user_id] = sqlite3.connect(f"{SESSION_DIR}/user_{user_id}.db", 
                                                     check_same_thread=False,
                                                     timeout=30)
    return DB_CONNECTION_POOL[user_id]

# ================= UTILITIES =================
def derive_address(index):
    """Derive a new blockchain address from the master seed"""
    acc = bip44_mst.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
    return acc.PublicKey().ToAddress(), acc.PrivateKey().Raw().ToHex()

async def check_balance(data):
    """Check balance of an escrow address"""
    bal = 0
    if data["token"] == "btc":
        url = f"https://api.blockcypher.com/v1/btc/main/addrs/{data['address']}/balance?token={BLOCKCYPHER_TOKEN}"
        bal = requests.get(url).json().get("balance", 0) / 1e8
    elif data["token"] == "ltc":
        url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{data['address']}/balance?token={BLOCKCYPHER_TOKEN}"
        bal = requests.get(url).json().get("balance", 0) / 1e8
    elif data["token"] == "usdt":
        if data["network"] == "erc":
            c = w3_eth.eth.contract(address=USDT_ERC20, abi=erc20_abi)
            bal = c.functions.balanceOf(data["address"]).call() / 1e6
        elif data["network"] == "bep":
            c = w3_bsc.eth.contract(address=USDT_BEP20, abi=erc20_abi)
            bal = c.functions.balanceOf(data["address"]).call() / 1e6
    return bal

async def send_transaction(data, to_address, amount):
    """Send funds from escrow address to recipient"""
    fee = amount * ESCROW_FEE
    send_amount = amount - fee

    if data["token"] == "btc" or data["token"] == "ltc":
        url = f"https://api.blockcypher.com/v1/{data['token']}/main/txs/new"
        tx_data = {
            "inputs": [{"addresses": [data["address"]]}],
            "outputs": [{"addresses": [to_address], "value": int(send_amount * 1e8)}]
        }
        res = requests.post(url, json=tx_data).json()
        priv_key = data["key"]
        for tosign in res['tosign']:
            sig = requests.post("https://api.blockcypher.com/v1/" + data['token'] + "/main/txs/sign", json={"tosign": [tosign], "keys": [priv_key]}).json()
            res['signatures'] = sig['signatures']
        send_res = requests.post(url.replace("/new", "/send"), json=res).json()
        return send_res.get("tx", {}).get("hash")

    elif data["token"] == "usdt":
        if data["network"] in ["erc", "bep"]:
            web3 = w3_eth if data["network"] == "erc" else w3_bsc
            contract_addr = USDT_ERC20 if data["network"] == "erc" else USDT_BEP20
            contract = web3.eth.contract(address=contract_addr, abi=erc20_abi)
            priv_key = data["key"]
            nonce = web3.eth.get_transaction_count(data["address"])
            tx = contract.functions.transfer(to_address, int(send_amount * 1e6)).build_transaction({
                'chainId': 1 if data["network"] == "erc" else 56,
                'gas': 100000,
                'gasPrice': web3.to_wei('5', 'gwei'),
                'nonce': nonce
            })
            signed_tx = web3.eth.account.sign_transaction(tx, private_key=priv_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
            return web3.to_hex(tx_hash)
    return None

# ================= UI KEYBOARDS =================
def token_menu():
    """Keyboard for token selection"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("BTC", callback_data="tok_btc"),
         InlineKeyboardButton("LTC", callback_data="tok_ltc"),
         InlineKeyboardButton("USDT", callback_data="tok_usdt")]
    ])

def usdt_network_menu():
    """Keyboard for USDT network selection"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ERC20", callback_data="net_erc"),
         InlineKeyboardButton("BEP20", callback_data="net_bep"),
         InlineKeyboardButton("TRC20", callback_data="net_trc")]
    ])

def confirmation_keyboard(action):
    """Keyboard for release/refund confirmation"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Confirm {action}", callback_data=f"confirm_{action}"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
    ])

# Main menu keyboard
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("COMMANDS LIST🤖", callback_data="commands_list")],
        [InlineKeyboardButton("☎️CONTACT", callback_data="contact")],
        [
            InlineKeyboardButton("Updates🔃", url=UPDATES_CHANNEL),
            InlineKeyboardButton("Vouches✔️", url=VOUCHES_CHANNEL)
        ],
        [
            InlineKeyboardButton("WHAT IS ESCROW ?", callback_data="what_is_escrow"),
            InlineKeyboardButton("Instructions", callback_data="instructions")
        ],
        [InlineKeyboardButton("Terms📝", callback_data="terms")],
        [InlineKeyboardButton("Invite🎭", callback_data="invite")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Back button keyboard
def back_keyboard():
    keyboard = [[InlineKeyboardButton("🔙 BACK", callback_data="back")]]
    return InlineKeyboardMarkup(keyboard)

# Get main menu content
def get_main_menu_content():
    return (
        f"💫 @{BOT_USERNAME} 💫\n"
        "Your Trustworthy Telegram Escrow Service\n\n"
        f"Welcome to @{BOT_USERNAME}. This bot provides a reliable escrow service for your transactions on Telegram.\n"
        "Avoid scams, your funds are safeguarded throughout your deals. If you run into any issues, simply type <code>/dispute</code> "
        "and an arbitrator will join the group chat within 24 hours.\n\n"
        "🎟 ESCROW FEE:\n"
        "1.0% for P2P and 1.0% for OTC Flat\n\n"
        "🌐 (UPDATES) - (VOUCHES) ☑️\n\n"
        "💬 Proceed with <code>/escrow</code> (to start with a new escrow)\n\n"
        "⚠️ IMPORTANT - Make sure coin is same of Buyer and Seller else you may lose your coin.\n\n"
        "💡 Type <code>/menu</code> to summon a menu with all bot features."
    )

# Get commands list content
def get_commands_list_content():
    return (
        "📌 AVAILABLE COMMANDS\n\n"
        "<code>/start</code> - A command to start interacting with the bot\n"
        "<code>/whatisescrow</code> - A command to tell you more about escrow\n"
        "<code>/instructions</code> - A command with text instructions\n"
        "<code>/terms</code> - A command to bring out our TOS\n"
        "<code>/dispute</code> - A command to contact the admins\n"
        "<code>/menu</code> - A command to bring out a menu for the bot\n"
        "<code>/contact</code> - A command to get admin's contact\n"
        "<code>/commands</code> - A command to get commands list\n"
        "<code>/stats</code> - A command to check user stats\n"
        "<code>/vouch</code> - A command to vouch for the bot\n"
        "<code>/newdeal</code> - A command to start a new deal\n"
        "<code>/tradeid</code> - A command to get trade id for a chat\n"
        "<code>/dd</code> - A command to add deal details\n"
        "<code>/escrow</code> - A command to get a escrow group link\n"
        "<code>/token</code> - A command to select token for the escrow\n"
        "<code>/deposit</code> - A command to generate deposit address\n"
        "<code>/verify</code> - A command to verify wallet address\n"
        "<code>/balance</code> - A command to check the balance of the escrow address\n"
        "<code>/release</code> - A command to release the funds in the escrow\n"
        "<code>/refund</code> - A command to refund the funds in the escrow\n"
        "<code>/seller</code> - A command to set the seller\n"
        "<code>/buyer</code> - A command to set the buyer\n"
        "<code>/setfee</code> - A command to set custom trade fee\n"
        "<code>/save</code> - A command to save default addresses for various chains\n"
        "<code>/saved</code> - A command to check saved addresses\n"
        "<code>/referral</code> - A command to check your referrals"
    )

# Get contact content
def get_contact_content():
    return (
        "☎️ CONTACT ARBITRATOR\n\n"
        "💬 Type <code>/dispute</code>\n\n"
        f"💡 Incase you're not getting a response can reach out to {ADMIN_CONTACT}"
    )

# Get escrow explanation
def get_what_is_escrow_content():
    return (
        "Escrow is a safe and reliable way to ensure both parties in a transaction are protected. "
        "Funds are held securely until both sides agree that conditions are met. "
        "This prevents scams and creates trust between Buyer and Seller."
    )

# Get instructions content
def get_instructions_content():
    return (
        f"📘 GUIDE “ HOW TO USE @{BOT_USERNAME} ( Escrow Bot ) “ FOR SAFE AND FASTEST HASSLE-FREE ESCROW 🚀\n\n"
        "Step 1 : Use <code>/escrow</code> command in the DM of the Bot.\n"
        "( It will auto-create a safe escrow group and drop the link so that buyer and seller can join via that link. ) 🔗👥\n\n"
        "Step 2 : Use <code>/dd</code> command to initiate the process of escrow where you will get the format to express your deal and info.\n"
        "( It will include quantity, rate, TnC’s agreed upon by both parties. ) 📝🤝\n\n"
        "Step 3 : Use <code>/buyer (your address)</code> if you are a buyer 🛒 or <code>/seller (your address)</code> if you are a seller 🏪 to verify address and continue the deal.\n"
        "( Provide your crypto address which will be used in case of release or refund. ) 💳🔐\n\n"
        "Step 4 : Choose the token and network by <code>/token</code> command and then either party has to accept it. ✅💱\n\n"
        "Step 5 : Use <code>/deposit</code> command to deposit the asset within the bot.\n"
        "( Note : Bot will give the deposit address and it has a time limit to deposit ⏳, you have to deposit within that given time. ) ⏰💸\n\n"
        "Step 6 : Once verified by the bot, you can continue the deal.\n"
        "( Bot will send the real-time deposit details in the chat. ) 📊💬\n\n"
        "Step 7 : After a successful deal, you can release the asset to the party by using <code>/release (amount/all)</code>.\n"
        "( Thus, the bot will itself release the asset to the party and send the verification in the chat. ) 🎉💼\n\n"
        "🚨 IN CASE OF ANY DISPUTE OR ISSUE, YOU CAN FEEL FREE TO USE <code>/dispute</code> COMMAND, AND SUPPORT WILL JOIN YOU SHORTLY. 🛎️👩‍💻"
    )

# Get terms content
def get_terms_content():
    return (
        "📜 TERMS\n\n"
        "Our terms of usage are simple.\n\n"
        "🎟 Fees\n"
        "1.0% for P2P and 1.0% for OTC Flat.\n\n"
        "Transactions fee will be applicable.\n\n"
        "TAKE THIS INTO ACCOUNT WHEN DEPOSITING FUNDS\n\n"
        "1️⃣ Record/screenshot the desktop while your perform any testing of logins or data, or recording of physical items being opened, "
        "this is to provide evidence that the data does not work, if the data is working and you are happy to release the funds, "
        "you can delete the recording.\n\n"
        "FAILURE TO PRODUCE SUFFICIENT EVIDENCE OF TESTING WILL RESULT IN LOSS OF FUNDS\n\n"
        "2️⃣ Before you purchase any information, please take the time to learn what you are buying\n\n"
        "IT IS NOT THE RESPONSIBILITY OF THE SELLER TO EXPLAIN HOW TO USE THE INFORMATION, ALTHOUGH IT MAY HELP MAKE TRANSACTIONS RUN SMOOTHER IF VENDORS HELP BUYERS\n\n"
        "3️⃣ Buyer should ONLY EVER release funds when they RECEIVE WHAT YOU PAID FOR.\n\n"
        "WE ARE NOT RESPONSIBLE FOR YOU RELEASING EARLY AND CAN NOT RETRIEVE FUNDS BACK\n\n"
        "4️⃣ Users should use trusted local wallets such as electrum.org or exodus wallet to prevent any issues with KYC wallets like Coinbase or Paxful.\n\n"
        "ONLINE WALLETS CAN BE SLOW AND BLOCK ACCOUNTS\n\n"
        "5️⃣ Our fee's are taken from the balance in the wallet (1.0% for P2P and 1.0% for OTC), so make sure you take that into account when depositing funds.\n\n"
        "WE ARE A SERVICE BARE THAT IN MIND\n\n"
        "6️⃣ Make sure Coin and Network are same for Buyer and Seller, else you may lose your funds."
    )

# Get invite content
def get_invite_content(user_id):
    ref_code = generate_referral_code(user_id)
    invite_link = f"https://t.me/{BOT_USERNAME}?start=ref_{ref_code}"
    return (
        "📍 Total Invites: 0 👤\n"
        "📍 Tickets: 0 �\n\n"
        "💡 Each voucher = 25% off fees\n"
        "⚡ Earn fee tickets for every invite after $1 escrow\n\n"
        f"Your Invite Link: {invite_link}\n\n"
        "💎 For every new user you invite, you get 2 fee tickets.\n"
        "💎 For every old user (who has already interacted with the bot), you get 1 fee ticket.\n\n"
        "Start sharing and enjoy CRAZY fee discounts! 🎉"
    )

# Generate referral code for user
def generate_referral_code(user_id):
    s = str(user_id) + ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    return hashlib.sha256(s.encode()).hexdigest()[:12].upper()

# Generate random group link ID
def generate_group_link_id():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=16))

# Generate escrow ID
def generate_escrow_id():
    return ''.join(random.choices(string.digits, k=5))

# Add Telegram account
async def add_telegram_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ACCOUNT_MANAGER:
        ACCOUNT_MANAGER[user_id] = {
            'accounts': []  # Changed to store account details
        }
    
    if len(ACCOUNT_MANAGER[user_id]['accounts']) >= MAX_ACCOUNTS:
        await update.message.reply_text("❌ You can only add up to 10 Telegram accounts.")
        return
    
    LOGIN_STATES[user_id] = {
        'state': 'awaiting_phone',
        'step': 1
    }
    await update.message.reply_text("Please send your Telegram phone number in international format (e.g., +1234567890):")

# Handle all login messages
async def handle_login_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    # If user is not in any login state, ignore
    if user_id not in LOGIN_STATES:
        return
    
    state = LOGIN_STATES[user_id]['state']
    
    if state == 'awaiting_phone':
        await handle_phone(update, context)
    elif state == 'awaiting_code':
        await handle_code(update, context)
    elif state == 'awaiting_password':
        await handle_password(update, context)

# Handle phone number input
async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    phone = update.message.text
    
    # Validate phone format
    if not phone.startswith('+') or not phone[1:].isdigit():
        await update.message.reply_text("❌ Invalid phone format. Please use international format (e.g., +1234567890):")
        return
    
    # Initialize account manager
    if user_id not in ACCOUNT_MANAGER:
        ACCOUNT_MANAGER[user_id] = {'accounts': []}
    
    # Check if phone already added
    if any(acc['phone'] == phone for acc in ACCOUNT_MANAGER[user_id]['accounts']):
        await update.message.reply_text("❌ This phone number is already added.")
        return
    
    session_file = f"{SESSION_DIR}/{user_id}_{phone.replace('+', '')}"
    LOGIN_STATES[user_id] = {
        'state': 'awaiting_code',
        'phone': phone,
        'session_file': session_file
    }
    
    # Create Telethon client
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    
    try:
        sent = await client.send_code_request(phone)
        LOGIN_STATES[user_id]['phone_code_hash'] = sent.phone_code_hash
        LOGIN_STATES[user_id]['client'] = client  # Store client for later use
        await update.message.reply_text("📲 Verification code sent. Please enter the code you received:")
    except AuthRestartError:
        # Handle authorization restart error
        await update.message.reply_text("⚠️ Authorization process needs restart. Please try again with /addlogin.")
        del LOGIN_STATES[user_id]
        if 'client' in LOGIN_STATES.get(user_id, {}):
            await LOGIN_STATES[user_id]['client'].disconnect()
            del LOGIN_STATES[user_id]['client']
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        del LOGIN_STATES[user_id]
        if 'client' in LOGIN_STATES.get(user_id, {}):
            await LOGIN_STATES[user_id]['client'].disconnect()
            del LOGIN_STATES[user_id]['client']

# Handle code input
async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    code = update.message.text
    
    # Validate code format
    if not code.isdigit() or len(code) < 5:
        await update.message.reply_text("❌ Invalid code format. Please enter the 5-digit verification code:")
        return
    
    # Get login state
    state = LOGIN_STATES.get(user_id, {})
    if 'client' not in state:
        await update.message.reply_text("❌ Session expired. Please start over with /addlogin.")
        del LOGIN_STATES[user_id]
        return
    
    client = state['client']
    phone = state['phone']
    phone_code_hash = state['phone_code_hash']
    session_file = state['session_file']
    
    try:
        # Try signing in with the code
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        
        # If successful, add session
        ACCOUNT_MANAGER[user_id]['accounts'].append({
            'phone': phone,
            'session_file': session_file
        })
        await client.disconnect()
        total_accounts = len(ACCOUNT_MANAGER[user_id]['accounts'])
        await update.message.reply_text(f"✅ Account added successfully! Total accounts: {total_accounts}")
        del LOGIN_STATES[user_id]
    except SessionPasswordNeededError:
        # If 2FA is enabled, request password
        LOGIN_STATES[user_id]['state'] = 'awaiting_password'
        await update.message.reply_text("🔒 This account has 2FA enabled. Please enter your password:")
    except AuthRestartError:
        # Handle authorization restart error
        await update.message.reply_text("⚠️ Authorization process needs restart. Please try again with /addlogin.")
        await client.disconnect()
        del LOGIN_STATES[user_id]
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
        await client.disconnect()
        del LOGIN_STATES[user_id]

# Handle 2FA password input
async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    password = update.message.text
    
    # Get login state
    state = LOGIN_STATES.get(user_id, {})
    if 'client' not in state:
        await update.message.reply_text("❌ Session expired. Please start over with /addlogin.")
        del LOGIN_STATES[user_id]
        return
    
    client = state['client']
    
    try:
        # Complete authentication with password
        await client.sign_in(password=password)
        
        # Add session
        session_file = state['session_file']
        phone = state['phone']
        ACCOUNT_MANAGER[user_id]['accounts'].append({
            'phone': phone,
            'session_file': session_file
        })
        await client.disconnect()
        total_accounts = len(ACCOUNT_MANAGER[user_id]['accounts'])
        await update.message.reply_text(f"✅ Account added successfully! Total accounts: {total_accounts}")
        del LOGIN_STATES[user_id]
    except AuthRestartError:
        # Handle authorization restart error
        await update.message.reply_text("⚠️ Authorization process needs restart. Please try again with /addlogin.")
        await client.disconnect()
        del LOGIN_STATES[user_id]
    except Exception as e:
        # Handle missing pbkdf2_hmac specifically
        if "has no attribute 'pbkdf2_hmac'" in str(e):
            error_msg = (
                "❌ Your Python environment is missing OpenSSL support which is required for 2FA authentication.\n\n"
                "Please try one of these solutions:\n"
                "1. Reinstall Python with OpenSSL support\n"
                "2. Use a different Python distribution (like Anaconda)\n"
                "3. Run the bot in a proper virtual environment\n"
                "4. Use an account without 2FA protection"
            )
        else:
            error_msg = f"❌ Error: {str(e)}"
        
        await update.message.reply_text(error_msg)
        await client.disconnect()
        del LOGIN_STATES[user_id]

# Create Telegram group using logged-in account
async def create_telegram_group(session_file, group_type, creator_name):
    # Add .session extension if missing
    if not session_file.endswith('.session'):
        session_file += '.session'
    
    # Check if session file exists
    if not os.path.exists(session_file):
        print(f"❌ Session file not found: {session_file}")
        return None
    
    client = TelegramClient(session_file, API_ID, API_HASH)
    
    try:
        # Connect with timeout
        await asyncio.wait_for(client.connect(), timeout=30)
        
        # Generate escrow ID
        escrow_id = generate_escrow_id()
        
        # Create group with appropriate name
        title = f"{'P2P' if group_type == 'p2p' else 'Product Deal'} Escrow By Pagal (ID: {escrow_id})"
        result = await client(functions.channels.CreateChannelRequest(
            title=title,
            about="Secure escrow group for trading",
            megagroup=True
        ))
        
        # Get created channel
        channel = result.chats[0]
        
        # Set group permissions
        await client(functions.channels.EditBannedRequest(
            channel=channel,
            participant="all",
            banned_rights=types.ChatBannedRights(
                until_date=datetime(2030, 1, 1),
                view_messages=False,  # Allow viewing messages
                send_messages=False,   # Disable sending messages initially
                send_media=False,
                send_stickers=False,
                send_gifs=False,
                send_games=False,
                send_inline=False,
                embed_links=False,
            )
        ))
        
        # Generate invite link limited to 2 users
        invite_link = await client(functions.messages.ExportChatInviteRequest(
            peer=channel,
            usage_limit=2,
            expire_date=datetime.now() + timedelta(days=365)
        ))
        
        # Send welcome message
        welcome_msg = await client.send_message(
            channel.id,
            "📍 Hey there traders! Welcome to our escrow service.\n✅ Please start with /dd command and fill the DealInfo Form"
        )
        
        # Pin the message
        await client.pin_message(channel, welcome_msg.id, notify=False)
        
        # Add bot directly without username validation
        try:
            # Add bot using its username directly
            await client(functions.channels.InviteToChannelRequest(
                channel=channel,
                users=[BOT_USERNAME]
            ))
            
            # Set admin rights for the bot
            admin_rights = types.ChatAdminRights(
                change_info=True,
                post_messages=True,
                edit_messages=True,
                delete_messages=True,
                ban_users=True,
                invite_users=True,
                pin_messages=True,
                add_admins=False,
                anonymous=False,
                manage_call=True,
                other=True
            )
            
            # Promote bot to admin
            await client(functions.channels.EditAdminRequest(
                channel=channel,
                user_id=BOT_USERNAME,
                admin_rights=admin_rights,
                rank="Escrow Bot"
            ))
        except Exception as e:
            print(f"❌ Failed to add bot: {str(e)}")
            await client.send_message(
                channel.id,
                "⚠️ Couldn't add bot automatically. Please add @wingoaiprobot manually as admin."
            )
        
        # Disconnect client
        await client.disconnect()
        print(f"✅ Group created successfully: {invite_link.link}")
        return invite_link.link, channel.id
    except AuthRestartError:
        print("⚠️ Authorization process needs restart. Session may be invalid.")
        return None, None
    except asyncio.TimeoutError:
        print("⚠️ Connection timed out during group creation.")
        return None, None
    except Exception as e:
        print(f"❌ Group creation error: {str(e)}")
        return None, None

# Start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_main_menu_content()
    
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )

# Menu command handler
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# Escrow command handler (private only)
async def escrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please use this command in DM.")
        return
    
    text = "Please select your escrow type from below:"
    keyboard = [
        [
            InlineKeyboardButton("P2P", callback_data="p2p"),
            InlineKeyboardButton("Product Deal", callback_data="product_deal")
        ]
    ]
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

# Handle group creation process
async def create_escrow_group(update: Update, context: ContextTypes.DEFAULT_TYPE, group_type: str):
    query = update.callback_query
    await query.answer()
    
    # Store group creation state
    user_id = query.from_user.id
    GROUP_CREATION_STATE[user_id] = {
        "group_type": group_type,
        "query": query
    }
    
    # Show creating message
    await query.edit_message_text(
        "🔄 Creating a safe trading place for you... Please wait...",
        parse_mode=ParseMode.HTML
    )
    
    # Select a random account
    if not ACCOUNT_MANAGER.get(user_id, {}).get('accounts'):
        await query.edit_message_text("❌ No Telegram accounts available. Please add accounts with /addlogin command.")
        return
    
    account = random.choice(ACCOUNT_MANAGER[user_id]['accounts'])
    session_file = account['session_file']
    
    # Create group using Telethon
    creator_name = query.from_user.full_name
    group_link, group_id = await create_telegram_group(session_file, group_type, creator_name)
    
    if not group_link or not group_id:
        await query.edit_message_text("❌ Failed to create group. Please try again later.")
        return
    
    # Create group created message
    text = (
        f"✅ Escrow Group Created\n\n"
        f"👤 Creator: {creator_name}\n\n"
        f"🔗 Join this escrow group and share the link with the buyer and seller:\n\n"
        f"{group_link}\n\n"
        "⚠️ Note: This link is for 2 members only—third parties are not allowed to join."
    )
    
    # Send group created message
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Join Group", url=group_link)]]),
        parse_mode=ParseMode.HTML
    )
    
    # Initialize group state
    GROUP_STATES[group_id] = False
    GROUP_ESCROWS[group_id] = {
        "token": None,
        "network": None,
        "address": None,
        "key": None,
        "status": "PENDING",
        "amount": 0,
        "buyer": None,
        "seller": None
    }

# Command handlers for all menu items
async def commands_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_commands_list_content()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_contact_content()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def what_is_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_what_is_escrow_content()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_instructions_content()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_terms_content()
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
    text = get_invite_content(user_id)
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )
    else:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=back_keyboard(),
            parse_mode=ParseMode.HTML
        )

# Button handler
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_id = query.from_user.id
    
    if data == "commands_list":
        await commands_list(update, context)
    
    elif data == "contact":
        await contact(update, context)
    
    elif data == "what_is_escrow":
        await what_is_escrow(update, context)
    
    elif data == "instructions":
        await instructions(update, context)
    
    elif data == "terms":
        await terms(update, context)
    
    elif data == "invite":
        await invite(update, context)
    
    elif data == "p2p":
        await create_escrow_group(update, context, "p2p")
    
    elif data == "product_deal":
        await create_escrow_group(update, context, "product_deal")
    
    elif data == "back":
        await start(update, context)
    
    elif data.startswith("tok_"):
        token = data.split("_")[1]
        if token == "usdt":
            await query.message.edit_text("Select USDT network:", reply_markup=usdt_network_menu())
        else:
            group_id = query.message.chat.id
            idx = len(GROUP_ESCROWS)
            addr, key = derive_address(idx)
            GROUP_ESCROWS[group_id] = {
                "token": token, 
                "network": token, 
                "address": addr, 
                "key": key, 
                "status": "PENDING",
                "amount": 0,
                "buyer": None,
                "seller": None
            }
            await query.message.edit_text(f"Deposit Address for {token.upper()}:\n<code>{addr}</code>")
    
    elif data.startswith("net_"):
        net = data.split("_")[1]
        group_id = query.message.chat.id
        idx = len(GROUP_ESCROWS)
        addr, key = derive_address(idx)
        GROUP_ESCROWS[group_id] = {
            "token": "usdt", 
            "network": net, 
            "address": addr, 
            "key": key, 
            "status": "PENDING",
            "amount": 0,
            "buyer": None,
            "seller": None
        }
        await query.message.edit_text(f"Deposit Address for USDT {net.upper()}:\n<code>{addr}</code>")
    
    elif data == "confirm_release":
        data = GROUP_ESCROWS.get(query.message.chat.id)
        if not data or data["status"] != "FUNDED" or not data["seller"]:
            return await query.answer("Cannot release funds now")
        
        txid = await send_transaction(data, data["seller"], data['amount'])
        data["status"] = "RELEASED"
        await query.message.edit_text(f"✅ Released to seller! TXID: {txid}")
    
    elif data == "confirm_refund":
        data = GROUP_ESCROWS.get(query.message.chat.id)
        if not data or data["status"] != "FUNDED" or not data["buyer"]:
            return await query.answer("Cannot refund funds now")
        
        txid = await send_transaction(data, data["buyer"], data['amount'])
        data["status"] = "REFUNDED"
        await query.message.edit_text(f"✅ Refunded to buyer! TXID: {txid}")
    
    elif data == "cancel_action":
        await query.message.edit_text("Action cancelled")

# Error handler
async def error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")
    if "database is locked" in str(context.error):
        print("⚠️ Database lock detected. Implementing retry mechanism...")
        # Implement retry logic or wait and retry
        await asyncio.sleep(1)

# List logged-in accounts command
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ACCOUNT_MANAGER or not ACCOUNT_MANAGER[user_id]['accounts']:
        await update.message.reply_text("❌ You have no logged-in accounts.")
        return
    
    accounts = ACCOUNT_MANAGER[user_id]['accounts']
    response = "📱 Logged-in Accounts:\n\n"
    for i, acc in enumerate(accounts, 1):
        response += f"{i}. {acc['phone']}\n"
    
    response += f"\nTotal accounts: {len(accounts)}"
    await update.message.reply_text(response)

# Group-only commands
async def dd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("Please use this command in a group.")
        return
    
    # Mark group as initialized
    chat_id = update.message.chat.id
    GROUP_STATES[chat_id] = True
    
    await update.message.reply_text(
        "Hello there,\n"
        "Kindly tell deal details i.e.\n\n"
        "Quantity -\n"
        "Rate -\n"
        "Conditions (if any) -\n\n"
        "Remember without it disputes wouldn’t be resolved. Once filled proceed with Specifications of the seller or buyer with /seller or /buyer [CRYPTO ADDRESS]"
    )

async def dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("Please use this command in a group.")
        return
    # Actual dispute handling would go here
    await update.message.reply_text("Dispute handling initiated...")

async def newdeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("Please use this command in a group.")
        return
    # Actual new deal handling would go here
    await update.message.reply_text("Starting a new deal...")

async def tradeid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("Please use this command in a group.")
        return
    # Actual trade ID handling would go here
    await update.message.reply_text("Trade ID generated...")

# Commands that require /dd to be used first
async def token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    await update.message.reply_text("Select token:", reply_markup=token_menu())

async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    data = GROUP_ESCROWS.get(chat_id)
    if not data or not data["address"]:
        await update.message.reply_text("Please select token first with /token")
        return
    
    token = data["token"].upper()
    if token == "USDT":
        token += f" ({data['network'].upper()})"
    
    await update.message.reply_text(
        f"Deposit Address for {token}:\n<code>{data['address']}</code>\n\n"
        "⚠️ Send only the selected token to this address. "
        "Other assets will be lost permanently."
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    data = GROUP_ESCROWS.get(chat_id)
    if not data or not data["address"]:
        await update.message.reply_text("Please select token first with /token")
        return
    
    bal = await check_balance(data)
    token = data["token"].upper()
    if token == "USDT":
        token += f" ({data['network'].upper()})"
    
    GROUP_ESCROWS[chat_id]["amount"] = bal
    await update.message.reply_text(f"Current Balance: {bal} {token}")

async def release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    data = GROUP_ESCROWS.get(chat_id)
    if not data or data["status"] != "FUNDED" or not data["seller"]:
        return await update.message.reply_text("Cannot release funds now")
    
    token = data["token"].upper()
    if token == "USDT":
        token += f" ({data['network'].upper()})"
    
    await update.message.reply_text(
        f"Release {data['amount']} {token} to seller?",
        reply_markup=confirmation_keyboard("release")
    )

async def refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    data = GROUP_ESCROWS.get(chat_id)
    if not data or data["status"] != "FUNDED" or not data["buyer"]:
        return await update.message.reply_text("Cannot refund funds now")
    
    token = data["token"].upper()
    if token == "USDT":
        token += f" ({data['network'].upper()})"
    
    await update.message.reply_text(
        f"Refund {data['amount']} {token} to buyer?",
        reply_markup=confirmation_keyboard("refund")
    )

async def seller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    if not context.args:
        return await update.message.reply_text("Please provide seller address: /seller [ADDRESS]")
    
    address = context.args[0]
    GROUP_ESCROWS[chat_id]["seller"] = address
    await update.message.reply_text(f"✅ Seller address set to:\n<code>{address}</code>")

async def buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    
    if not context.args:
        return await update.message.reply_text("Please provide buyer address: /buyer [ADDRESS]")
    
    address = context.args[0]
    GROUP_ESCROWS[chat_id]["buyer"] = address
    await update.message.reply_text(f"✅ Buyer address set to:\n<code>{address}</code>")

async def setfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat.id
    if chat_id not in GROUP_STATES or not GROUP_STATES[chat_id]:
        await update.message.reply_text("Sorry! please first use /dd first!")
        return
    # Actual fee setting would go here
    await update.message.reply_text("Fee setting updated...")

# Private-only commands
async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please use this command in DM.")
        return
    # Actual save handling would go here
    await update.message.reply_text("Address saved successfully...")

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Please use this command in DM.")
        return
    # Actual referral handling would go here
    await update.message.reply_text("Referral information retrieved...")

# Background task to monitor deposits
async def monitor_deposits():
    while True:
        for chat_id, data in GROUP_ESCROWS.items():
            if data["status"] == "PENDING":
                try:
                    bal = await check_balance(data)
                    if bal > 0:
                        data["amount"] = bal
                        data["status"] = "FUNDED"
                        token = data["token"].upper()
                        if token == "USDT":
                            token += f" ({data['network'].upper()})"
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ Deposit Confirmed!\nAmount: {bal} {token}"
                        )
                except Exception as e:
                    print(f"Error checking balance: {str(e)}")
        await asyncio.sleep(60)  # Check every minute

# Set up bot commands menu
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("menu", "Show main menu"),
        BotCommand("escrow", "Create new escrow group"),
        BotCommand("addlogin", "Add Telegram account (Admin)"),
        BotCommand("accounts", "List logged-in accounts"),
        BotCommand("commands", "Show all commands"),
        BotCommand("contact", "Contact arbitrator"),
        BotCommand("whatisescrow", "What is escrow?"),
        BotCommand("instructions", "How to use the bot"),
        BotCommand("terms", "Terms of service"),
        BotCommand("invite", "Invite friends")
    ])

# Main function
if __name__ == "__main__":
    print("Starting PagaL Escrow Bot...")
    
    # Configure SQLite to work with multiple threads
    sqlite3.threadsafety = 3
    
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    
    # Main commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("escrow", escrow))
    app.add_handler(CommandHandler("addlogin", add_telegram_account))
    app.add_handler(CommandHandler("accounts", list_accounts))
    
    # Menu command handlers
    app.add_handler(CommandHandler("commands", commands_list))
    app.add_handler(CommandHandler("contact", contact))
    app.add_handler(CommandHandler("whatisescrow", what_is_escrow))
    app.add_handler(CommandHandler("instructions", instructions))
    app.add_handler(CommandHandler("terms", terms))
    app.add_handler(CommandHandler("invite", invite))
    
    # Group-only commands
    app.add_handler(CommandHandler("dd", dd))
    app.add_handler(CommandHandler("dispute", dispute))
    app.add_handler(CommandHandler("newdeal", newdeal))
    app.add_handler(CommandHandler("tradeid", tradeid))
    
    # Commands requiring /dd first
    app.add_handler(CommandHandler("token", token))
    app.add_handler(CommandHandler("deposit", deposit))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("release", release))
    app.add_handler(CommandHandler("refund", refund))
    app.add_handler(CommandHandler("seller", seller))
    app.add_handler(CommandHandler("buyer", buyer))
    app.add_handler(CommandHandler("setfee", setfee))
    
    # Private-only commands
    app.add_handler(CommandHandler("save", save))
    app.add_handler(CommandHandler("referral", referral))
    
    # Unified login message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_messages))
    
    # Buttons
    app.add_handler(CallbackQueryHandler(button))
    
    # Errors
    app.add_error_handler(error)
    
    # Start deposit monitoring task
    app.job_queue.run_repeating(monitor_deposits, interval=60, first=10)
    
    print("Bot is running...")
    app.run_polling()
