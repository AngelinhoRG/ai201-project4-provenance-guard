import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

import scoring
import signals
import storage

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

storage.init_db()


def _now():
    return datetime.now(timezone.utc).isoformat()


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    creator_id = body.get("creator_id")

    if not text or not creator_id:
        return jsonify({"error": "text and creator_id are required"}), 400

    llm_result = signals.llm_signal(text)
    stylo_result = signals.stylo_signal(text)

    llm_score = llm_result["ai_probability"]
    stylo_score = stylo_result["ai_likeness"]
    combined_score = scoring.combine_scores(llm_score, stylo_score)
    attribution, label = scoring.generate_label(combined_score)

    content_id = str(uuid.uuid4())
    timestamp = _now()

    record = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "attribution": attribution,
        "confidence": round(combined_score, 4),
        "llm_score": round(llm_score, 4),
        "stylo_score": round(stylo_score, 4),
        "label": label,
        "status": "classified",
        "created_at": timestamp,
    }
    storage.save_submission(record)

    storage.log_event(
        "submission",
        content_id,
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "timestamp": timestamp,
            "attribution": attribution,
            "confidence": round(combined_score, 4),
            "llm_score": round(llm_score, 4),
            "stylo_score": round(stylo_score, 4),
            "llm_reasoning": llm_result["reasoning"],
            "stylo_components": stylo_result["components"],
            "status": "classified",
        },
    )

    return jsonify(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": attribution,
            "confidence": round(combined_score, 4),
            "llm_score": round(llm_score, 4),
            "stylo_score": round(stylo_score, 4),
            "label": label,
            "status": "classified",
        }
    )


@app.route("/appeal", methods=["POST"])
def appeal():
    body = request.get_json(silent=True) or {}
    content_id = body.get("content_id")
    creator_reasoning = body.get("creator_reasoning")

    if not content_id or not creator_reasoning:
        return jsonify({"error": "content_id and creator_reasoning are required"}), 400

    submission = storage.get_submission(content_id)
    if submission is None:
        return jsonify({"error": "content_id not found"}), 404

    storage.update_submission_status(content_id, "under_review")

    timestamp = _now()
    storage.log_event(
        "appeal",
        content_id,
        {
            "content_id": content_id,
            "timestamp": timestamp,
            "status": "under_review",
            "appeal_reasoning": creator_reasoning,
            "original_attribution": submission["attribution"],
            "original_confidence": submission["confidence"],
        },
    )

    return jsonify(
        {
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received and logged for human review.",
        }
    )


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": storage.get_log()})


if __name__ == "__main__":
    app.run(debug=True)
