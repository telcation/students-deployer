# webhook_test.py
from flask import Flask, request

app = Flask(__name__)

@app.route("/line_webhook", methods=["POST"])
def line_webhook():
    print(request.get_json(force=True), flush=True)
    return "OK"

if __name__ == "__main__":
    app.run(port=5009)
