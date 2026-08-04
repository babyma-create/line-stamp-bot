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

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

USER_LIST_FILE = "/tmp/user_list.json"

# 日本時間（JST）の定義
JST = timezone(timedelta(hours=9))

def get_user_list():
    if os.path.exists(USER_LIST_FILE):
        try:
            with open(USER_LIST_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_id(user_id):
    users = get_user_list()
    if user_id not in users:
        users[user_id] = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        with open(USER_LIST_FILE, "w") as f:
            json.dump(users, f)

# --- 確認カード（Flex Message）の共通データ作成 ---
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

# --- テキスト印鑑 ＆ 枠外ログ印字処理 ---
def add_text_stamp_with_log(input_pdf_path, output_pdf_path, user_name="承認者"):
    doc = fitz.open(input_pdf_path)
    page = doc[0]

    # ページの高さと幅を取得（単位: ポイント / 1pt ≒ 0.3528mm）
    page_width = page.rect.width
    page_height = page.rect.height

    # 単位換算: 1mm ≒ 2.83465 pt
    mm_to_pt = 2.83465

    # 右端から5mm、下から7cm（70mm）の位置を計算
    right_margin_pt = 5.0 * mm_to_pt   # 5mm
    bottom_margin_pt = 70.0 * mm_to_pt # 70mm (7cm)

    # 朱色枠のサイズ（幅約20mm、高さ約10mm）
    stamp_width = 20.0 * mm_to_pt
    stamp_height = 10.0 * mm_to_pt

    # 枠の左上X・Y座標
    STAMP_X = page_width - right_margin_pt - stamp_width - 80.0  # 右側のテキストが入るよう調整
    STAMP_Y = page_height - bottom_margin_pt - stamp_height

    rect = fitz.Rect(STAMP_X, STAMP_Y, STAMP_X + stamp_width, STAMP_Y + stamp_height)
    stamp_color = (0.9, 0.1, 0.1)  # 朱色

    # 四角い枠線を描画
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(color=stamp_color, width=1.5)
    shape.commit()

    # 「【 承 認 】」の文字を入れる
    page.insert_textbox(
        rect,
        "【 承 認 】",
        fontsize=8.0,
        fontname="japan",
        color=stamp_color,
        align=fitz.TEXT_ALIGN_CENTER
    )

    # 日本時間（JST）を取得
    now = datetime.now(JST)
    date_str = now.strftime("%Y/%m/%d")
    time_str = now.strftime("%H:%M")

    # 枠の右側にテキストを配置
    start_x = STAMP_X + stamp_width + 6.0
    start_y = STAMP_Y + 2.0
    line_height = 8.0

    lines = [
        f"承認者: {user_name}",
        f"確認日: {date_str}",
        f"        {time_str}"
    ]

    for i, line in enumerate(lines):
        point = fitz.Point(start_x, start_y + (i * line_height))
        page.insert_text(
            point,
            line,
            fontsize=6.5,
            fontname="japan",
            color=(0.2, 0.2, 0.2)
        )

    doc.save(output_pdf_path)
    doc.close()

@app.route("/", methods=['GET'])
def index():
    return "OK", 200

# 押印済みPDF・公開PDFのダウンロード用URL
@app.route("/files/<filename>", methods=['GET'])
def download_file(filename):
    return send_from_directory("/tmp", filename)

# --- 👑 管理者専用送信ページ ---
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>管理者用 送信画面</title>
    <style>
        body { font-family: sans-serif; padding: 20px; max-width: 500px; margin: auto; background: #f4f7f6; }
        .card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        h2 { color: #333; margin-top: 0; }
        label { font-weight: bold; display: block; margin-top: 15px; margin-bottom: 5px; }
        input[type="text"], input[type="file"], select { width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 5px; }
        button { background: #00B900; color: white; border: none; padding: 12px; width: 100%; border-radius: 5px; font-weight: bold; font-size: 16px; margin-top: 20px; cursor: pointer; }
        button:hover { background: #009900; }
        .msg { margin-top: 15px; padding: 10px; background: #e2f0d9; border: 1px solid #b2d8a0; border-radius: 5px; color: #2d572c; }
    </style>
</head>
<body>
    <div class="card">
        <h2>📄 PDF・承諾カード送信</h2>
        {% if msg %}
            <div class="msg">{{ msg }}</div>
        {% endif %}
        <form method="POST" enctype="multipart/form-data">
            <label>送信先の LINE User ID</label>
            <input type="text" name="user_id" placeholder="U1234567890abcdef..." required>
            <small style="color:#666;">※受信履歴のあるユーザーIDリスト：</small>
            <select onchange="this.previousElementSibling.previousElementSibling.value=this.value;">
                <option value="">-- 選択してください --</option>
                {% for uid, time in users.items() %}
                    <option value="{{ uid }}">{{ uid }} ({{ time }})</option>
                {% endfor %}
            </select>

            <label>送付するPDFファイル</label>
            <input type="file" name="pdf_file" accept=".pdf" required>

            <button type="submit">送信（PDF＋確認カード）</button>
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
                
                # 1. PDFリンクメッセージ送付
                text_msg = TextMessage(text=f"保護者様\n出席記録のPDFをお送りいたします。\n下記よりご確認ください。\n\n【添付PDF】\n{pdf_download_url}")
                # 2. 承諾カード送付
                card_msg = create_approval_card()

                try:
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=target_user_id,
                            messages=[text_msg, card_msg]
                        )
                    )
                    msg = "✅ 送信完了しました！PDFと承諾カードが届きました。"
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

# --- 1. ファイル（PDF）を受信した時の処理 ---
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

# --- 2. テキストメッセージを受信した時の処理 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    save_user_id(user_id)
    user_msg = event.message.text.strip()
    
    if "出席内容" in user_msg:
        reply_obj = create_approval_card()
    else:
        reply_obj = TextMessage(
            text="メッセージありがとうございます。\nただいま個別のお問い合わせは手動で確認しております。お時間をいただきますが少しお待ちください。"
        )

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[reply_obj]
            )
        )

# --- 3. 「承諾する」ボタンが押された時の処理 ---
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data

    if data == "action=approve":
        user_id = event.source.user_id
        save_user_id(user_id)
        input_pdf = f"/tmp/latest_{user_id}.pdf"
        
        if not os.path.exists(input_pdf):
            input_pdf = "sample_record.pdf"

        output_filename = f"stamped_{user_id}.pdf"
        output_pdf = f"/tmp/{output_filename}"

        # ユーザーのLINE表示名を取得する処理
        user_name = "保護者 様"
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                profile = line_bot_api.get_profile(user_id)
                if profile and profile.display_name:
                    user_name = f"{profile.display_name} 様"
        except Exception:
            pass

        try:
            add_text_stamp_with_log(input_pdf, output_pdf, user_name=user_name)
            
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
