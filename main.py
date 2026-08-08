import os
import json
import requests
import fitz  # PyMuPDF
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, send_from_directory, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FileMessageContent, PostbackEvent

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
JST = timezone(timedelta(hours=9))

# 最後に話しかけてきたユーザーのIDを一時保存するファイル
LAST_USER_FILE = "/tmp/last_user_id.txt"

def save_last_user(user_id):
    try:
        with open(LAST_USER_FILE, "w") as f:
            f.write(user_id)
    except Exception:
        pass

def get_last_user():
    try:
        if os.path.exists(LAST_USER_FILE):
            with open(LAST_USER_FILE, "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

def add_text_stamp(input_pdf_path, output_pdf_path, user_name="保護者"):
    doc = fitz.open(input_pdf_path)
    page = doc[0]
    page_width = page.rect.width
    page_height = page.rect.height
    
    mm_to_pt = 2.83465
    right_margin_pt = 20.0 * mm_to_pt
    bottom_margin_pt = 3.0 * mm_to_pt
    stamp_width = 18.0 * mm_to_pt
    stamp_height = 10.0 * mm_to_pt
    date_area_width = 65.0
    
    STAMP_X = page_width - right_margin_pt - stamp_width - date_area_width
    STAMP_Y = page_height - bottom_margin_pt - stamp_height
    
    rect = fitz.Rect(STAMP_X, STAMP_Y, STAMP_X + stamp_width, STAMP_Y + stamp_height)
    stamp_color = (0.9, 0.1, 0.1)
    
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(color=stamp_color, width=1.5)
    shape.commit()
    
    page.insert_textbox(
        rect, "【 承 認 】",
        fontsize=8.0, fontname="japan", color=stamp_color,
        align=fitz.TEXT_ALIGN_CENTER
    )
    
    now = datetime.now(JST)
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")
    
    start_x = STAMP_X + stamp_width + 4.0
    start_y = STAMP_Y + 1.0
    line_height = 8.0
    
    lines = [f"{date_str}", f"{time_str}", f"{user_name}"]
    for i, line in enumerate(lines):
        point = fitz.Point(start_x, start_y + (i * line_height))
        page.insert_text(point, line, fontsize=6.0, fontname="japan", color=(0.3, 0.3, 0.3))
        
    permissions = fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT
    doc.save(
        output_pdf_path,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw=None,
        permissions=permissions
    )
    doc.close()

@app.route("/", methods=['GET'])
def index():
    return "OK", 200

@app.route("/files/<filename>", methods=['GET'])
def download_file(filename):
    return send_from_directory("/tmp", filename)

ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>管理者用 送信画面</title>
    <style>
        body { font-family: sans-serif; padding: 20px; max-width: 550px; margin: auto; background: #f4f7f6; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        h2 { color: #333; margin-top: 0; }
        label { font-weight: bold; display: block; margin-top: 15px; margin-bottom: 5px; }
        input[type="text"], input[type="file"] { width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 5px; background: #fafafa; }
        button { background: #00B900; color: white; border: none; padding: 12px; width: 100%; border-radius: 5px; font-weight: bold; font-size: 16px; margin-top: 20px; cursor: pointer; }
        button:hover { background: #009900; }
        .msg { margin-top: 15px; padding: 10px; background: #e2f0d9; border: 1px solid #b2d8a0; border-radius: 5px; color: #2d572c; }
        .warn { background: #fff3cd; border: 1px solid #ffeeba; color: #856404; padding: 10px; border-radius: 5px; font-size: 13px; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📄 PDF送信・自動押印（管理画面）</h2>
        {% if msg %}
            <div class="msg">{{ msg|safe }}</div>
        {% endif %}
        <div class="warn">
            💡 <strong>使い方：</strong> 直近でLINEから話しかけてきたユーザーのIDが自動セットされます。PDFを選んで送信ボタンを押してください。
        </div>
        <form method="POST" enctype="multipart/form-data">
            <label>① 送信先のLINE User ID （自動取得済み）</label>
            <input type="text" name="user_id" id="user_id" value="{{ last_user }}" placeholder="LINEから何かメッセージを送ってください" required>
            
            <label>② 送付するPDFファイルを選択</label>
            <input type="file" name="pdf_file" id="pdf_file" accept=".pdf" required>
            
            <button type="submit">送信実行</button>
        </form>
    </div>
</body>
</html>
"""

@app.route("/admin", methods=['GET', 'POST'])
def admin_page():
    msg = None
    if request.method == 'POST':
        target_user_id = request.form.get('user_id', '').strip()
        pdf_file = request.files.get('pdf_file')
        
        if target_user_id and pdf_file:
            save_path = f"/tmp/latest_{target_user_id}.pdf"
            pdf_file.save(save_path)
            
            host_url = request.host_url.rstrip('/')
            pdf_download_url = f"{host_url}/files/latest_{target_user_id}.pdf"
            
            try:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    text_msg = TextMessage(text=f"保護者様\n出席記録のPDFをお送りいたします。\n下記よりご確認ください。\n\n【確認用PDF】\n{pdf_download_url}\n\n※内容を確認したら、このトークに「承諾」と返信してください。自動で押印済みPDFを発行いたします。")
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=target_user_id,
                            messages=[text_msg]
                        )
                    )
                msg = "✅ 送信完了しました！"
            except Exception as e:
                msg = f"❌ 送信エラー: {str(e)}"
                
    last_user = get_last_user()
    return render_template_string(ADMIN_HTML, msg=msg, last_user=last_user)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    save_last_user(user_id)  # ユーザーIDを自動記憶！
    
    text = event.message.text.strip()
    
    if "承諾" in text:
        input_pdf = f"/tmp/latest_{user_id}.pdf"
        if not os.path.exists(input_pdf):
            input_pdf = "sample_record.pdf"
            
        output_filename = f"stamped_{user_id}.pdf"
        output_pdf = f"/tmp/{output_filename}"
        user_name = "保護者"
        
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                profile = line_bot_api.get_profile(user_id)
                if profile and profile.display_name:
                    user_name = profile.display_name
        except Exception:
            pass
            
        try:
            add_text_stamp(input_pdf, output_pdf, user_name=user_name)
            
            host_url = request.url_root.rstrip('/')
            download_link = f"{host_url}/files/{output_filename}"
            reply_text = f"ご承諾ありがとうございます！\n自動押印と改ざん防止ロックが完了しました。\n\n【承諾済みPDFのダウンロード】\n{download_link}"
            
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)]
                    )
                )
        except Exception as e:
            error_text = f"❌ 処理エラーが発生しました: {str(e)}"
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=error_text)]
                    )
                )
    else:
        reply_text = "メッセージありがとうございます！IDを自動記憶しました。管理者画面からPDFを送信いたします。"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
