import os
import hmac
import hashlib
import base64
import time
import json
import httpx
import threading
from typing import List, Optional, Dict
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__)

# In-memory store for task_id -> user_id and user_id -> mode
task_user_map = {}
user_mode_map = {}  # user_id -> "normal" or "ai"
user_last_activity = {}  # user_id -> last activity timestamp

# --- Configuration ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MANUS_API_KEY = os.getenv("MANUS_API_KEY")
MANUS_PROJECT_ID = os.getenv("MANUS_PROJECT_ID")

# リッチメニューID
DEFAULT_RICH_MENU_ID = "19172896"
AI_MODE_RICH_MENU_ID = "19175576"

# AIモードの自動タイムアウト設定
AI_MODE_TIMEOUT_MINUTES = 10
TIMEOUT_CHECK_INTERVAL_SECONDS = 60

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

def switch_rich_menu(user_id: str, rich_menu_id: str):
    try:
        line_bot_api.link_rich_menu_to_user(user_id, rich_menu_id)
        print(f"DEBUG: Rich menu switched for user {user_id} to {rich_menu_id}")
    except Exception as e:
        print(f"DEBUG: Error switching rich menu: {e}")

def check_and_reset_ai_mode_timeout():
    while True:
        try:
            current_time = datetime.now()
            timeout_threshold = current_time - timedelta(minutes=AI_MODE_TIMEOUT_MINUTES)
            users_to_reset = []
            for user_id, mode in list(user_mode_map.items()):
                if mode == "ai":
                    last_activity = user_last_activity.get(user_id)
                    if last_activity and last_activity < timeout_threshold:
                        users_to_reset.append(user_id)
            for user_id in users_to_reset:
                try:
                    user_mode_map[user_id] = "normal"
                    switch_rich_menu(user_id, DEFAULT_RICH_MENU_ID)
                    line_bot_api.push_message(user_id, TextSendMessage(text="AIモードが10分間不使用のため、自動的に通常モードに戻りました。ご質問があれば、いつでもお気軽にお尋ねやすください。"))
                except Exception as e:
                    print(f"DEBUG: Error resetting user {user_id}: {e}")
        except Exception as e:
            print(f"Error in timeout check: {e}")
        time.sleep(TIMEOUT_CHECK_INTERVAL_SECONDS)

@app.route("/webhook/line", methods=["POST"])
def line_webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    if not signature or not body:
        return "OK"
    
    try:
        json_body = json.loads(body)
        if "events" in json_body and len(json_body["events"]) == 0:
            return "OK"
        
        try:
            line_handler.handle(body, signature)
        except InvalidSignatureError:
            print("DEBUG: InvalidSignatureError. Handling manually...")
            if "events" in json_body and len(json_body["events"]) > 0:
                for event_data in json_body["events"]:
                    if event_data.get("type") == "message" and event_data.get("message", {}).get("type") == "text":
                        handle_manual_event(event_data)
            else:
                abort(400)
    except Exception as e:
        print(f"DEBUG: Error in webhook: {e}")
        return "OK"
    
    return "OK"

def handle_manual_event(event_data):
    user_id = event_data.get("source", {}).get("userId")
    user_message = event_data.get("message", {}).get("text", "").strip()
    if not user_id or not user_message:
        return

    current_mode = user_mode_map.get(user_id, "normal")
    
    if current_mode == "normal" and user_message in ["AIに質問する", "設定について聞く", "AIに質問"]:
        user_mode_map[user_id] = "ai"
        user_last_activity[user_id] = datetime.now()
        switch_rich_menu(user_id, AI_MODE_RICH_MENU_ID)
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを起動しました。LOGGの設定方法などについてご質問いただけます。\\n\\n※回答の生成に10〜20秒ほどお時間をいただく場合があります。少々お待ちください。"))
        return

    if current_mode == "ai" and user_message == "AIモードを終了する":
        user_mode_map[user_id] = "normal"
        switch_rich_menu(user_id, DEFAULT_RICH_MENU_ID)
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを終了いたします。ご不明な点などございましたら、またお気軽にお尋ねやすください。"))
        return

    if current_mode == "ai":
        user_last_activity[user_id] = datetime.now()
        create_manus_task(user_id, user_message)

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    current_mode = user_mode_map.get(user_id, "normal")
    
    if current_mode == "normal" and user_message in ["AIに質問する", "設定について聞く", "AIに質問"]:
        user_mode_map[user_id] = "ai"
        user_last_activity[user_id] = datetime.now()
        switch_rich_menu(user_id, AI_MODE_RICH_MENU_ID)
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを起動しました。LOGGの設定方法などについてご質問いただけます。\\n\\n※回答の生成に10〜20秒ほどお時間をいただく場合があります。少々お待ちください。"))
    elif current_mode == "ai" and user_message == "AIモードを終了する":
        user_mode_map[user_id] = "normal"
        switch_rich_menu(user_id, DEFAULT_RICH_MENU_ID)
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを終了いたします。ご不明な点などございましたら、またお気軽にお尋ねやすください。"))
    elif current_mode == "ai":
        user_last_activity[user_id] = datetime.now()
        create_manus_task(user_id, user_message)

def create_manus_task(user_id, user_message):
    try:
        headers = {"Content-Type": "application/json", "x-manus-api-key": MANUS_API_KEY}
        
        system_instruction = """
あなたはLOGGシステムのLINEサポートAIです。

【役割】
LOGGシステムのLINEサポートAIとして、お客様からのメッセージを受け取った際に、丁寧かつ簡潔な日本語で対応してください。

【参照情報】
提供された知識ベース（LOGGシステムの仕様書）のみを参考にしてください。
お客様の個人情報やシステム内部のデータには絶対にアクセスしないでください。

【回答ルール】
もし知識ベースにない質問や、システム内部の調査が必要な場合は、深掘りせず以下のように回答してください：
「申し訳ありませんが、この件については担当者より改めてご連絡させていただきます。」

【出力形式】
回答は丁寧で簡潔な日本語でお願いします。
"""
        full_prompt = f"{system_instruction}\\n\\nお客様からのメッセージ: {user_message}"
        
        payload = {
            "message": {"content": [{"type": "text", "text": full_prompt}]},
            "interactive_mode": False
        }
        with httpx.Client() as client:
            resp = client.post("https://api.manus.ai/v2/task.create", json=payload, headers=headers)
            if resp.status_code == 200:
                task_id = resp.json().get("task_id")
                task_user_map[task_id] = user_id
                print(f"DEBUG: Task created: {task_id}")
    except Exception as e:
        print(f"Error creating task: {e}")

@app.route("/webhook/manus", methods=["POST"])
def manus_webhook():
    data = request.json
    if data and data.get("event_type") == "task_stopped":
        task_id = data.get("task_detail", {}).get("task_id")
        result_message = data.get("task_detail", {}).get("message")
        if task_id in task_user_map:
            user_id = task_user_map[task_id]
            line_bot_api.push_message(user_id, TextSendMessage(text=result_message))
    return jsonify({"status": "ok"})

timeout_thread = threading.Thread(target=check_and_reset_ai_mode_timeout, daemon=True)
timeout_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
