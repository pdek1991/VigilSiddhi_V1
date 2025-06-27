from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from elasticsearch import Elasticsearch

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests

# Connect to Elasticsearch
es = Elasticsearch("http://192.168.56.30:9200")

@app.route("/")
def home():
    return render_template("test_alarm_overview.html")

@app.route("/api/get_alarms", methods=["POST"])
def get_alarms():
    try:
        # Modify the size if you expect more data
        result = es.search(
            index="monitor_historical_alarms",
            body={
                "query": {"match_all": {}},
                "size": 1000,
                "sort": [{"timestamp": {"order": "desc"}}]
            }
        )
        return jsonify(result["hits"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
