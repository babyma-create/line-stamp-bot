import os
import fitz  # PyMuPDF
from datetime import datetime
from flask import Flask, request, abort, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

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
def add_text_stamp_with_log(input_pdf_path, output_pdf_path, user_name="承認者", line_user_id=""):
    doc = fitz.open(input_pdf_path)
    page = doc[0]

    STAMP_X = 450.0
    STAMP_Y = 700.0
    WIDTH = 70.0
    HEIGHT = 32.0

    rect = fitz.Rect(STAMP_X, STAMP_Y, STAMP_X + WIDTH, STAMP_Y + HEIGHT)
    stamp_color = (0.9, 0.1, 0.1)  # 朱色

    shape = page.new_shape()
    shape.draw_round_rect(rect, 4)
    shape.finish(color=stamp_color, width=1.5)
    shape.commit()

    page.insert_textbox(
        rect,
        "【 承 認 】",
        fontsize=9,
        fontname="japan",
        color=stamp_color,
        align=fitz.TEXT_ALIGN_CENTER
    )

    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    start_x = STAMP_X - 30
    start_y = STAMP_Y + HEIGHT + 12
    line_height = 9.0

    lines = [
        f"承認者: {user_name}",
        f"確認日: {now_str}",
        f"LINE ID: {line_user_id}"
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

# 押印済みPDFのダウンロード用URL
@app.route("/files/<filename>", methods=['GET'])
def download_file(filename):
    return send_from_directory("/tmp", filename)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# --- 1. メッセージを受信した時の処理 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # 「確認」や「送付」などのキーワードが含まれる場合は「承諾カード」を送信
    if "確認" in user_msg or "送付" in user_msg or "承認" in user_msg:
        reply_obj = create_approval_card()
    else:
        # それ以外の一般的な問い合わせには案内メッセージを返信
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

# --- 2. 「承諾する」ボタンが押された時の処理 ---
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data

    if data == "action=approve":
        input_pdf = "sample_record.pdf"
        user_id = event.source.user_id
        output_filename = f"stamped_{user_id}.pdf"
        output_pdf = f"/tmp/{output_filename}"

        try:
            user_name = "保護者 様"
            add_text_stamp_with_log(input_pdf, output_pdf, user_name=user_name, line_user_id=user_id)
            
            download_url = f"https://line-stamp-bot-y4g2.onrender.com/files/{output_filename}"
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
