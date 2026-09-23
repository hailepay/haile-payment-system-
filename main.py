import os
import re
import sqlite3
import logging
from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import easyocr
import threading

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ቶከኑን ከ Render Environment Variable ወይም በቀጥታ ማግኘት
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8916049187:AAENsAlv1HpaFOK5IPrnUOrMwdyyfnypBao")

# EasyOCR አንባቢ - ለ Render ቀላል እንዲሆን በእንግሊዝኛ ብቻ እና CPU ሞድ ተዘጋጅቷል
reader = easyocr.Reader(['en'], gpu=False)

def init_db():
    conn = sqlite3.connect('payments.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            txn_id TEXT PRIMARY KEY,
            sender TEXT,
            amount TEXT,
            raw_text TEXT,
            verified INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_transaction(txn_id, sender, amount, raw_text):
    try:
        conn = sqlite3.connect('payments.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO transactions (txn_id, sender, amount, raw_text)
            VALUES (?, ?, ?, ?)
        ''', (txn_id.upper(), sender, amount, raw_text))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"DB Error: {e}")
        return False

def check_transaction(extracted_text):
    conn = sqlite3.connect('payments.db')
    cursor = conn.cursor()
    cursor.execute('SELECT txn_id, amount, sender, verified FROM transactions')
    rows = cursor.fetchall()
    
    cleaned_extracted = re.sub(r'[^A-Za-z0-9]', '', extracted_text).upper()
    
    for row in rows:
        txn_id, amount, sender, verified = row
        clean_txn = re.sub(r'[^A-Za-z0-9]', '', txn_id).upper()
        if clean_txn and clean_txn in cleaned_extracted:
            if verified == 1:
                conn.close()
                return "ALREADY_USED", txn_id, amount, sender
            else:
                cursor.execute('UPDATE transactions SET verified = 1 WHERE txn_id = ?', (txn_id,))
                conn.commit()
                conn.close()
                return "SUCCESS", txn_id, amount, sender
                
    conn.close()
    return "NOT_FOUND", None, None, None

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return "Payment Verification System is active and running!", 200

@app.route('/sms-webhook', methods=['POST'])
def receive_sms():
    data = request.json or request.form
    sender = data.get('sender', '') or data.get('from', '')
    message = data.get('message', '') or data.get('content', '') or data.get('text', '')

    logging.info(f"Incoming SMS from: {sender} | Content: {message}")

    valid_senders = [
        'TELEBIRR', 'CBE', '127', '889', 'COMMERCIAL BANK',
        'AWASH', 'AWASHBANK', 'ABYSSINIA', 'BOA',
        'DASHEN', 'DASHENBANK', 'SIINQEE', 'SINQEE'
    ]
    is_valid_sender = any(s in sender.upper() for s in valid_senders)

    if not is_valid_sender:
        return jsonify({"status": "ignored", "reason": "Not an authorized bank SMS"}), 200

    txn_id = None
    txn_patterns = [
        r'(?:transaction\s*id|trans\.?\s*id|txn\s*id|tid)[:\s]*([A-Z0-9]+)',
        r'(?:ref(?:\s*no)?\.?|reference(?:\s*no)?\.?)[:\s]*([A-Z0-9]+)',
        r'(?:ft|tt|txn|id)[:\s]*([A-Z0-9]{8,20})'
    ]
    for pattern in txn_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            txn_id = match.group(1)
            break

    if not txn_id:
        words = re.findall(r'\b[A-Z0-9]{8,18}\b', message)
        if words:
            txn_id = words[0]

    amount_match = re.search(r'(?:ETB|BIRR|ብር|ከፍለዋል|ተቀብለዋል|credited|received)\s*([0-9,]+(?:\.[0-9]{1,2})?)', message, re.IGNORECASE)
    amount = amount_match.group(1) if amount_match else "ያልተገለጸ"

    if txn_id:
        save_transaction(txn_id, sender, amount, message)
        return jsonify({"status": "saved", "txn_id": txn_id, "amount": amount}), 200

    return jsonify({"status": "no_txn_found"}), 200

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "ሰላም! እንኳን ወደ ክፍያ ማረጋገጫ ሲስተም በደህና መጡ።\n\n"
        "የሚደገፉ ተቋማት፦\n"
        "• ቴሌብር (Telebirr)\n"
        "• የኢትዮጵያ ንግድ ባንክ (CBE)\n"
        "• አዋሽ ባንክ (Awash)\n"
        "• አቢሲኒያ ባንክ (BOA)\n"
        "• ዳሽን ባንክ (Dashen)\n"
        "• ስንቄ ባንክ (Siinqee)\n\n"
        "እባክዎ የተፈጸመውን የክፍያ ደረሰኝ ፎቶ (Screenshot) እዚህ ይላኩ።"
    )
    await update.message.reply_text(msg)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("ደረሰኙ እየተመረመረ ነው... እባክዎ ጥቂት ሰከንዶች ይጠብቁ።")
    file_path = f"temp_{update.message.chat_id}.jpg"
    try:
        photo_file = await update.message.photo[-1].get_file()
        await photo_file.download_to_drive(file_path)

        # በ EasyOCR ምስሉን ማንበብ
        results = reader.readtext(file_path, detail=0)
        extracted_text = " ".join(results)

        if os.path.exists(file_path):
            os.remove(file_path)

        result, txn_id, amount, sender = check_transaction(extracted_text)

        if result == "SUCCESS":
            response = (
                "✅ ትክክለኛ ክፍያ ተረጋግጧል!\n\n"
                f"🔹 መለያ ቁጥር (Txn ID): {txn_id}\n"
                f"🔹 የገንዘብ መጠን: {amount} ብር\n"
                f"🔹 ተቋም/ባንክ: {sender}\n\n"
                "እናመሰግናለን!"
            )
        elif result == "ALREADY_USED":
            response = (
                "⚠️ ማስጠንቀቂያ፦ ይህ ደረሰኝ ቀደም ሲል አገልግሎት ላይ ውሏል!\n"
                f"የቀድሞ መለያ ቁጥር: {txn_id}"
            )
        else:
            response = (
                "❌ ክፍያው ሊረጋገጥ አልቻለም!\n\n"
                "ምክንያቶች፦\n"
                "1. የባንክ የገቢ መልዕክት ወደ ሲስተማችን ገና አልደረሰም።\n"
                "2. የላኩት ፎቶ ግልጽ አይደለም።\n\n"
                "እባክዎ ጥቂት ቆይተው እንደገና ይሞክሩ።"
            )

        await status_msg.edit_text(response)

    except Exception as e:
        logging.error(f"Error processing receipt: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)
        await status_msg.edit_text("ደረሰኙን በማንበብ ሂደት ላይ ስህተት አጋጥሟል። እባክዎ ግልጽ ፎቶ በድጋሚ ይላኩ።")

def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

def main():
    threading.Thread(target=run_flask).start()
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.run_polling()

if __name__ == '__main__':
    main()
