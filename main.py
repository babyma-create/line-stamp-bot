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

# --- 自動押印処理 ---
def add_final_stamp(input_pdf_path, output_pdf_path, stamp_image_path="sample_stamp.png"):
    STAMP_X = 480.0
    STAMP_Y = 750.0
    STAMP_SIZE = 35.0

    doc = fitz.open(input_pdf_path)
    page = doc[0]
    
    # 1. 印鑑の配置
    stamp_rect = fitz.Rect(STAMP_X, STAMP_Y, STAMP_X + STAMP_SIZE, STAMP_Y + STAMP_SIZE)
    page.insert_image(stamp_rect, filename=stamp_image_path)
    
    # 2. 確認日時の印字
    now_str = datetime.now().strftime("確認日: %Y/%m/%d %H:%M")
    page.insert_text(
        fitz.Point(STAMP_X - 10, STAMP_Y + STAMP_SIZE + 10),
        now_str,
        fontsize=8,
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

# --- 1. テキスト（メッセージ）を受信した時の処理 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text

    # 「確認」や「案内」といったキーワードが来たら承認ボタンカードを送る
    # （またはどのメッセージに対しても案内カードを返す設定にできます）
    flex_json = {
      "type": "bubble",
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "📄 書類確認・承諾のお願い",
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

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[FlexMessage(alt_text="書類確認のお願い", contents=FlexContainer.from_dict(flex_json))]
            )
        )

# --- 2. 「承諾する」ボタン（Postback）が押された時の処理 ---
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data

    if data == "action=approve":
        input_pdf = "sample_record.pdf"  # 元となる対象PDF（事前にリポジトリに配置）
        output_filename = f"stamped_{event.source.user_id}.pdf"
        output_pdf = f"/tmp/{output_filename}"

        try:
            # 押印実行
            add_final_stamp(input_pdf, output_pdf, "sample_stamp.png")
            
            download_url = f"https://line-stamp-bot-y4g2.onrender.com/files/{output_filename}"
            reply_text = f"ご承諾ありがとうございます！\n自動押印が完了しました。\n\n【押印済みPDFの確認URL】\n{download_url}"

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
