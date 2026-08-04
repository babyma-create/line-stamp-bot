import os
import json
import fitz  # PyMuPDF
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, send_from_directory, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FileMessageContent, PostbackEvent
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
DRIVE_FOLDER_ID = os.environ.get('DRIVE_FOLDER_ID')
SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

USER_LIST_FILE = "/tmp/user_list.json"
JST = timezone(timedelta(hours=9))

def get_gspread_client():
    if not SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        return None
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        print(f"Gspread Auth Error: {e}")
        return None

# --- スプレッドシートへユーザーリストを読み書き（永久保存） ---
def get_user_list():
    users = {}
    if os.path.exists(USER_LIST_FILE):
        try:
            with open(USER_LIST_FILE, "r") as f:
                users = json.load(f)
        except:
            users = {}
    
    client = get_gspread_client()
    if client:
        try:
            sh = client.open_by_key(SPREADSHEET_ID)
            try:
                sheet = sh.worksheet("Users")
            except:
                sheet = sh.add_worksheet(title="Users", rows=500, cols=5)
                sheet.append_row(["User_ID", "Name", "Last_Seen"])
            
            records = sheet.get_all_records()
            for r in records:
                uid = str(r.get("User_ID", "")).strip()
                if uid:
                    users[uid] = {
                        "name": r.get("Name", "保護者"),
                        "last_seen": r.get("Last_Seen", "")
                    }
        except Exception as e:
            print(f"User list fetch error: {e}")
            
    return users

def save_user_id(user_id, display_name=""):
    users = get_user_list()
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    name = display_name or users.get(user_id, {}).get("name", "保護者")
    users[user_id] = {
        "last_seen": now_str,
        "name": name
    }
    
    try:
        with open(USER_LIST_FILE, "w") as f:
            json.dump(users, f, ensure_ascii=False)
    except Exception as e:
        print(f"Local save error: {e}")

    client = get_gspread_client()
    if client:
        try:
            sh = client.open_by_key(SPREADSHEET_ID)
            try:
                sheet = sh.worksheet("Users")
            except:
                sheet = sh.add_worksheet(title="Users", rows=500, cols=5)
                sheet.insert_row(["User_ID", "Name", "Last_Seen"], index=1)
            
            cell = None
            try:
                cell = sheet.find(user_id)
            except:
                cell = None

            if cell:
                sheet.update_cell(cell.row, 2, name)
                sheet.update_cell(cell.row, 3, now_str)
            else:
                sheet.insert_row([user_id, name, now_str], index=2)
        except Exception as e:
            print(f"Spreadsheet save error: {e}")

# --- Google ドライブへのPDFアップロード＆スプレッドシートログ記録 ---
def log_approval_and_upload_pdf(user_id, user_name, local_pdf_path, filename):
    if not SERVICE_ACCOUNT_JSON:
        return False
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        
        drive_link = ""
        if DRIVE_FOLDER_ID:
            drive_service = build('drive', 'v3', credentials=creds)
            file_metadata = {
                'name': f"承認済み_{user_name}_{filename}",
                'parents': [DRIVE_FOLDER_ID]
            }
            media = MediaFileUpload(local_pdf_path, mimetype='application/pdf')
            uploaded_file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()
            drive_link = uploaded_file.get('webViewLink', '')

        if SPREADSHEET_ID:
            client = gspread.authorize(creds)
            sheet = client.open_by_key(SPREADSHEET_ID).sheet1
            now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([now_str, user_id, user_name, filename, drive_link])
            
        return True
    except Exception as e:
        print(f"Drive/Sheet Log Error: {e}")
        return False

def create_approval_card():
    flex_json = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "📄 出席・確認のお願い",
                    "weight": "bold",
                    "size": "md"
                },
                {
                    "type": "text",
                    "text": "内容をご確認の上、問題がなければ下の「承諾する」ボタンを押してください。",
                    "wrap": True,
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md"
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "承諾する",
                        "data": "action=approve"
                    },
                    "style": "primary",
                    "color": "#00B900"
                }
            ]
        }
    }
    return FlexMessage(alt_text="書類確認のお願い", contents=FlexContainer.from_dict(flex_json))

def add_text_stamp_with_log(input_pdf_path, output_pdf_path, user_name="保護者"):
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
        rect,
        "【 承 認 】",
        fontsize=8.0,
        fontname="japan",
        color=stamp_color,
        align=fitz.TEXT_ALIGN_CENTER
    )
    
    now = datetime.now(JST)
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")
    
    start_x = STAMP_X + stamp_width + 4.0
    start_y = STAMP_Y + 1.0
    line_height = 8.0
    
    lines = [
        f"{date_str}",
        f"{time_str}",
        f"{user_name}"
    ]
    
    for i, line in enumerate(lines):
        point = fitz.Point(start_x, start_y + (i * line_height))
        page.insert_text(
            point,
            line,
            fontsize=6.0,
            fontname="japan",
            color=(0.3, 0.3, 0.3)
        )
        
    doc.save(output_pdf_path)
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
        input[type="text"], input[type="file"], select { width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 5px; }
        button { background: #00B900; color: white; border: none; padding: 12px; width: 100%; border-radius: 5px; font-weight: bold; font-size: 16px; margin-top: 20px; cursor: pointer; }
        button:hover { background: #009900; }
        .msg { margin-top: 15px; padding: 10px; background: #e2f0d9; border: 1px solid #b2d8a0; border-radius: 5px; color: #2d572c; }
        .warn { background: #fff3cd; border: 1px solid #ffeeba; color: #856404; padding: 10px; border-radius: 5px; font-size: 13px; margin-top: 10px; }
    </style>
    <script>
        function checkConfirm() {
            var select = document.getElementById("user_select");
            var selectedText = select.options[select.selectedIndex].text;
            var fileInput = document.getElementById("pdf_file");
            var fileName = fileInput.files[0] ? fileInput.files[0].name : "未選択";
            
            if (!select.value) {
                alert("送信先の保護者を選択してください。");
                return false;
            }
            var result = confirm("【送信前の最終確認】\\n\\n送信先保護者: " + selectedText + "\\n添付ファイル: " + fileName + "\\n\\n間違いありませんか？送信を実行します。");
            return result;
        }
        function updateUserId(val) {
            document.getElementById("user_id_input").value = val;
        }
    </script>
</head>
<body>
    <div class="card">
        <h2>📄 PDF・承諾カード送信（保護者用）</h2>
        {% if msg %}
            <div class="msg">{{ msg }}</div>
        {% endif %}
        
        <div class="warn">
            ⚠️ <strong>誤送信防止機能：</strong> 送信前に宛先保護者様とお子様名、添付PDF名をポップアップで確認します。
        </div>

        <form method="POST" enctype="multipart/form-data" onsubmit="return checkConfirm();">
            <label>① 送信先の保護者様を選択</label>
            <select id="user_select" onchange="updateUserId(this.value);" required>
                <option value="">-- 送信先の保護者を選択してください --</option>
                {% for uid, info in users.items() %}
                    <option value="{{ uid }}">{{ info.name }} 様 (最終更新: {{ info.last_seen }})</option>
                {% endfor %}
            </select>
            <input type="hidden" name="user_id" id="user_id_input" required>

            <label>② 送付するPDFファイルを選択</label>
            <input type="file" name="pdf_file" id="pdf_file" accept=".pdf" required>

            <button type="submit">送信実行（確認後に送信）</button>
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
            
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                
                text_msg = TextMessage(text=f"保護者様\n出席記録のPDFをお送りいたします。\n下記よりご確認ください。\n\n【添付PDF】\n{pdf_download_url}")
                card_msg = create_approval_card()
                
                try:
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=target_user_id,
                            messages=[text_msg, card_msg]
                        )
                    )
                    msg = "✅ 送信完了しました！保護者様へPDFと承諾カードが届きました。"
                except Exception as e:
                    msg = f"❌ 送信エラー: {str(e)}"
                    
    users = get_user_list()
    return render_template_string(ADMIN_HTML, users=users, msg=msg)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=FileMessageContent)
def handle_file(event):
    message_id = event.message.id
    user_id = event.source.user_id
    save_user_id(user_id)
    
    save_path = f"/tmp/latest_{user_id}.pdf"
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        content = line_bot_blob_api.get_message_content(message_id)
        with open(save_path, 'wb') as f:
            f.write(content)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    display_name = ""
    
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            if profile and profile.display_name:
                display_name = profile.display_name
    except:
        pass
        
    saved_name = user_text if user_text else (display_name or "保護者")
    
    # スプレッドシートへ強制的に2行目挿入保存
    save_user_id(user_id, saved_name)
    
    reply_text = f"ご連絡ありがとうございます！\n保護者様（お名前: {saved_name}）として登録いたしました。"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    if data == "action=approve":
        user_id = event.source.user_id
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
            
        save_user_id(user_id, user_name)
        
        try:
            add_text_stamp_with_log(input_pdf, output_pdf, user_name=user_name)
            log_approval_and_upload_pdf(user_id, user_name, output_pdf, output_filename)
            
            host_url = request.host_url.rstrip('/')
            download_url = f"{host_url}/files/{output_filename}"
            
            reply_text = f"ご承諾ありがとうございます！\n自動押印（電子承認）が完了しました。\n\n【押印済みPDFの確認URL】\n{download_url}"
        except Exception as e:
            reply_text = f"押印処理中にエラーが発生しました: {str(e)}"
            
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
