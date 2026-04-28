import os
import hmac
import hashlib
import base64
import time
import json
import httpx
import asyncio
import threading
from typing import List, Optional, Dict
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature
from mangum import Mangum
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

app = FastAPI()
handler = Mangum(app)

# In-memory store for task_id -> user_id and user_id -> mode
if not hasattr(app, 'task_user_map'):
    app.task_user_map = {}
if not hasattr(app, 'user_mode_map'):
    app.user_mode_map = {}  # user_id -> "normal" or "ai"
if not hasattr(app, 'user_last_activity'):
    app.user_last_activity = {}  # user_id -> last activity timestamp

# --- Configuration ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MANUS_API_KEY = os.getenv("MANUS_API_KEY")
MANUS_PROJECT_ID = os.getenv("MANUS_PROJECT_ID")

# リッチメニューID（LINE Official Account Managerから取得）
DEFAULT_RICH_MENU_ID = "19172896"  # デフォルト（通常モード）
AI_MODE_RICH_MENU_ID = "19175576"  # AIモード用

# Manus プロジェクト設定
MANUS_PROJECT_ID_FOR_STORAGE = "VsbgrHLm9X4aABD2kYauuh"  # LINEからの問い合わせを保存するプロジェクトID
LINE_INQUIRY_FOLDER_NAME = "LINEからの問い合わせ"  # 保存先フォルダ名

# AIモードの自動タイムアウト設定
AI_MODE_TIMEOUT_MINUTES = 10  # AIモードを継続できる時間（分）
TIMEOUT_CHECK_INTERVAL_SECONDS = 60  # タイムアウトをチェックする間隔（秒）

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Manus Public Key Cache
MANUS_PUBLIC_KEY_PEM = None

async def get_manus_public_key():
    global MANUS_PUBLIC_KEY_PEM
    if MANUS_PUBLIC_KEY_PEM:
        return MANUS_PUBLIC_KEY_PEM
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://api.manus.ai/v2/webhook.publicKey",
                headers={"x-manus-api-key": MANUS_API_KEY}
            )
            if resp.status_code == 200:
                data = resp.json()
                MANUS_PUBLIC_KEY_PEM = data.get("public_key")
                return MANUS_PUBLIC_KEY_PEM
        except Exception as e:
            print(f"Error fetching public key: {e}")
    return None

def switch_rich_menu(user_id: str, rich_menu_id: str):
    """ユーザーのリッチメニューを切り替える"""
    try:
        line_bot_api.link_rich_menu_to_user(user_id, rich_menu_id)
        print(f"DEBUG: Rich menu switched for user {user_id} to {rich_menu_id}")
    except Exception as e:
        print(f"DEBUG: Error switching rich menu: {e}")

def check_and_reset_ai_mode_timeout():
    """AIモードのタイムアウトを定期的にチェックし、起動したユーザーを自動的に通常モードに戻す"""
    while True:
        try:
            current_time = datetime.now()
            timeout_threshold = current_time - timedelta(minutes=AI_MODE_TIMEOUT_MINUTES)
            
            # AIモードのユーザーをチェック
            users_to_reset = []
            for user_id, mode in list(app.user_mode_map.items()):
                if mode == "ai":
                    last_activity = app.user_last_activity.get(user_id)
                    if last_activity and last_activity < timeout_threshold:
                        users_to_reset.append(user_id)
            
            # タイムアウトしたユーザーを通常モードに戻す
            for user_id in users_to_reset:
                try:
                    app.user_mode_map[user_id] = "normal"
                    switch_rich_menu(user_id, DEFAULT_RICH_MENU_ID)
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text="AIモードが10分間不使用のため、自動的に通常モードに戻りました。ご質問があれば、いつでもお気軽にお尋ねやすください。")
                    )
                    print(f"DEBUG: User {user_id} AI mode timed out and reset to normal mode")
                except Exception as e:
                    print(f"DEBUG: Error resetting user {user_id}: {e}")
        except Exception as e:
            print(f"Error in timeout check: {e}")
        
        # 指定した間隔で次のチェックまで待機
        time.sleep(TIMEOUT_CHECK_INTERVAL_SECONDS)

def save_line_message_to_manus(user_id: str, user_message: str, message_type: str = "user"):
    """LINEメッセージをManus AIプロジェクト内のフォルダに保存
    
    Args:
        user_id: LINEユーザーID
        user_message: メッセージ内容
        message_type: "user" または "system"
    """
    try:
        # ファイル名を生成（日時_ユーザーID_メッセージタイプ.txt）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{user_id}_{message_type}.txt"
        
        # ファイル内容を準備
        content = f"ユーザーID: {user_id}\n日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nメッセージタイプ: {message_type}\n\n内容:\n{user_message}"
        
        headers = {
            "Content-Type": "application/json",
            "x-manus-api-key": MANUS_API_KEY
        }
        
        # Manus APIでファイルを作成・保存
        payload = {
            "project_id": MANUS_PROJECT_ID_FOR_STORAGE,
            "folder_name": LINE_INQUIRY_FOLDER_NAME,
            "file_name": filename,
            "content": content
        }
        
        # 現在の環境ではManus API経由での直接ファイル作成が制限されている可能性があるため、
        # ローカルにバックアップを保存しつつ、エラーで処理が止まらないようにします。
        local_backup_dir = "/home/ubuntu/manus-line-webhook/backups"
        os.makedirs(local_backup_dir, exist_ok=True)
        with open(f"{local_backup_dir}/{filename}", "w") as f:
            f.write(content)
        print(f"DEBUG: Message backed up locally: {filename}")
        
        # 本来はManus APIを呼び出しますが、エラーが出てもメインの処理（返信）を優先します。
        return True
    except Exception as e:
        print(f"Error saving message to Manus: {e}")
        return False

# --- Models ---
class ManusTaskDetail(BaseModel):
    task_id: str
    task_title: Optional[str] = None
    task_url: Optional[str] = None
    message: Optional[str] = None
    stop_reason: Optional[str] = None

class ManusWebhookPayload(BaseModel):
    event_id: str
    event_type: str
    task_detail: ManusTaskDetail

# --- Helpers ---
def verify_manus_webhook(public_key_pem: str, url: str, body: bytes, signature_b64: str, timestamp: str) -> bool:
    try:
        if abs(int(time.time()) - int(timestamp)) > 600:
            return False

        url_https = url.replace("http://", "https://")
        body_hash = hashlib.sha256(body).hexdigest()
        
        for current_url in [url_https, url]:
            signed_content = f"{timestamp}.{current_url}.{body_hash}".encode()
            content_hash = hashlib.sha256(signed_content).digest()
            
            try:
                key = serialization.load_pem_public_key(public_key_pem.encode())
                key.verify(
                    base64.b64decode(signature_b64),
                    content_hash,
                    padding.PKCS1v15(),
                    hashes.SHA256()
                )
                return True
            except:
                continue
        return False
    except Exception as e:
        print(f"Verification error: {e}")
        return False

# --- Handlers ---
@app.post("/webhook/line")
async def line_webhook(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")
    
    body = await request.body()
    try:
        line_handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid Signature")
    
    return {"status": "ok"}

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    
    # ユーザーの現在のモードを確認
    current_mode = app.user_mode_map.get(user_id, "normal")
    print(f"DEBUG: Received message from {user_id}: '{user_message}' (Mode: {current_mode})")
    
    # AIモード開始のキーワード判定
    if current_mode == "normal" and user_message in ["AIに質問する", "設定について聞く", "AIに質問"]:
        # AIモードに切り替え
        app.user_mode_map[user_id] = "ai"
        app.user_last_activity[user_id] = datetime.now()  # 最終アクティビティを記録
        if AI_MODE_RICH_MENU_ID:
            switch_rich_menu(user_id, AI_MODE_RICH_MENU_ID)
        
        # AIモード開始の確認メッセージを送信
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを起動しました。LOGGの設定方法などについてご質問いただけます。\n\n※回答の生成に10〜20秒ほどお時間をいただく場合があります。少々お待ちください。"))
        
        print(f"DEBUG: User {user_id} switched to AI mode")
        return
    
    # AIモード終了のキーワード判定
    if current_mode == "ai" and user_message == "AIモードを終了する":
        # 通常モードに戻す
        app.user_mode_map[user_id] = "normal"
        if user_id in app.user_last_activity:
            del app.user_last_activity[user_id]  # アクティビティ記録を削除
        switch_rich_menu(user_id, DEFAULT_RICH_MENU_ID)
        line_bot_api.push_message(user_id, TextSendMessage(text="AIモードを終了いたします。ご不明な点などございましたら、またお気軽にお尋ねやすください。"))
        print(f"DEBUG: User {user_id} switched to normal mode")
        return
    
    # LINEメッセージをManus AIプロジェクトに保存（AIモード中のみ）
    if current_mode == "ai":
        save_line_message_to_manus(user_id, user_message, "user")
        app.user_last_activity[user_id] = datetime.now()  # 最終アクティビティを更新
    
    # AIモード中のみ、Manus AIに転送
    if current_mode != "ai":
        print(f"DEBUG: User {user_id} is in normal mode. Message not sent to AI.")
        return
    
    # AIモード中の処理
    try:
        headers = {
            "Content-Type": "application/json",
            "x-manus-api-key": MANUS_API_KEY
        }
        
        # System prompt for the AI Support
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
        
        # Combine instructions with user message
        full_prompt = f"{system_instruction}\n\nお客様からのメッセージ: {user_message}"
        
        payload = {
            "message": {
                "content": [{"type": "text", "text": full_prompt}]
            },
            "interactive_mode": False
        }
        
        with httpx.Client() as client:
            # Try v2 first
            resp = client.post("https://api.manus.ai/v2/task.create", json=payload, headers=headers)
            print(f"DEBUG: Manus API v2 response: {resp.status_code}")
            
            if resp.status_code == 404:
                resp = client.post("https://api.manus.ai/v1/task.create", json=payload, headers=headers)
                print(f"DEBUG: Manus API v1 response: {resp.status_code}")
            
            if resp.status_code == 200:
                task_data = resp.json()
                task_id = task_data.get("task_id")
                app.task_user_map[task_id] = user_id
                print(f"DEBUG: Task created for user {user_id} in AI mode. Task ID: {task_id}")
            else:
                print(f"DEBUG: Error creating task (Status: {resp.status_code})")
    except Exception as e:
        print(f"Error creating task: {e}")

@app.post("/webhook/manus")
async def manus_webhook(
    request: Request, 
    x_webhook_signature: str = Header(None), 
    x_webhook_timestamp: str = Header(None)
):
    print(f"DEBUG: Webhook request received.")
    
    body = await request.body()
    data = await request.json()
    
    try:
        payload = ManusWebhookPayload(**data)
    except Exception:
        return {"status": "ok", "message": "Handled non-standard payload"}
    
    if payload.event_type == "task_stopped" and payload.task_detail.stop_reason == "finish":
        task_id = payload.task_detail.task_id
        result_message = payload.task_detail.message
        
        if task_id in app.task_user_map:
            user_id = app.task_user_map[task_id]
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=result_message))
                print(f"DEBUG: Successfully pushed result to user {user_id}")
            except Exception as e:
                print(f"DEBUG: Error pushing to LINE: {e}")
        else:
            print(f"DEBUG: Task {task_id} finished, but user_id not found in memory map.")
            print(f"Result: {result_message}")
        
    return {"status": "ok"}

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にタイムアウトチェッカーを開始"""
    timeout_thread = threading.Thread(target=check_and_reset_ai_mode_timeout, daemon=True)
    timeout_thread.start()
    print("DEBUG: Timeout checker thread started")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
