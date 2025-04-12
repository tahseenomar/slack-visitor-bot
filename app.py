import os
import json
import re
import time
import sys
from datetime import datetime
from flask import Flask, request, make_response, Response
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

app = Flask(__name__)

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
verifier = SignatureVerifier(signing_secret=os.environ["SLACK_SIGNING_SECRET"])

@app.route("/slack/events", methods=["POST"])
def slack_events():
    print("🚨 Slack just hit the /slack/events endpoint")  # debug
    sys.stdout.flush()

    if not verifier.is_valid_request(request.get_data(), request.headers):
        return make_response("Invalid signature", 403)

    if request.form.get("command") == "/visitor":
        trigger_id = request.form.get("trigger_id")
        open_modal(trigger_id)
        return make_response("", 200)

    if "payload" in request.form:
        payload_raw = request.form["payload"]
        print("📦 Raw payload received:")
        print(payload_raw)

        try:
            payload = json.loads(payload_raw)
        except Exception as e:
            print("❌ Failed to parse payload:", e)
            return make_response("Bad payload", 400)

        if payload.get("type") == "view_submission" and payload.get("view", {}).get("callback_id") == "visitor_form":
            print("✅ Modal submitted")

            try:
                values = payload["view"]["state"]["values"]
                errors = validate_submission(values)

                if errors:
                    return Response(
                        json.dumps({"response_action": "errors", "errors": errors}),
                        status=200,
                        content_type="application/json"
                    )

                user_id = payload["user"]["id"]
                handle_submission(values, user_id)

            except Exception as e:
                print("❌ Error in handle_submission:", e)

            return Response(
                json.dumps({"response_action": "clear"}),
                status=200,
                content_type="application/json"
            )

    return make_response("No handler", 404)

def open_modal(trigger_id):
    client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "visitor_form",
            "title": {"type": "plain_text", "text": "Register a guest"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "guest_name",
                    "label": {"type": "plain_text", "text": "Guest's name"},
                    "element": {"type": "plain_text_input", "action_id": "value"}
                },
                {
                    "type": "input",
                    "block_id": "guest_email",
                    "label": {"type": "plain_text", "text": "Guest's email"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value"
                    },
                    "optional": True
                },
                {
                    "type": "input",
                    "block_id": "date",
                    "label": {"type": "plain_text", "text": "Date of visit"},
                    "element": {
                        "type": "datepicker",
                        "action_id": "value"
                    }
                },
                {
                    "type": "input",
                    "block_id": "start_time",
                    "label": {"type": "plain_text", "text": "Start time (ET)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value"
                    },
                    "hint": {"type": "plain_text", "text": "E.g., 2:30pm"}
                },
                {
                    "type": "input",
                    "block_id": "end_time",
                    "label": {"type": "plain_text", "text": "End time (ET)"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value"
                    },
                    "hint": {"type": "plain_text", "text": "E.g., 3:30pm"}
                },
                {
                    "type": "input",
                    "block_id": "reason",
                    "label": {"type": "plain_text", "text": "Reason for or nature of visit"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "value",
                        "multiline": True
                    }
                }
            ]
        }
    )

def parse_flexible_time(date_str, time_str):
    time_str = time_str.strip().lower().replace(".", ":").replace("  ", " ").replace("pm", " pm").replace("am", " am")
    formats = [
        "%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I.%M %p", "%I.%M%p", "%H.%M"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(f"{date_str} {time_str}", f"%Y-%m-%d {fmt}")
        except ValueError:
            continue
    raise ValueError(f"Could not parse time: {time_str}")

def validate_submission(values):
    errors = {}
    try:
        date_str = values["date"]["value"]["selected_date"]
        start_str = values["start_time"]["value"]["value"]
        end_str = values["end_time"]["value"]["value"]

        start_dt = parse_flexible_time(date_str, start_str)
        end_dt = parse_flexible_time(date_str, end_str)

        if start_dt >= end_dt:
            errors["end_time"] = "End time must be after start time."

    except Exception as e:
        print("❌ Time parse error:", e)
        errors["start_time"] = "Enter time like 2:30 PM or 14:30"
        errors["end_time"] = "Enter time like 3:30 PM or 15:30"

    email = values["guest_email"]["value"]["value"] if "guest_email" in values else ""
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        errors["guest_email"] = "Must be a valid email address."

    return errors

def get_user_profile(user_id):
    try:
        user_info = client.users_info(user=user_id)
        profile = user_info["user"]["profile"]
        return {
            "email": profile.get("email"),
            "first_name": profile.get("first_name") or profile.get("real_name").split()[0]
        }
    except Exception as e:
        print(f"⚠️ Could not fetch Slack user profile for {user_id}:", e)
        return {"email": None, "first_name": "Unknown"}

def handle_submission(values, user_id):
    try:
        guest_name = values["guest_name"]["value"]["value"]
        guest_email = values["guest_email"]["value"]["value"] if "guest_email" in values else None
        date = values["date"]["value"]["selected_date"]
        start_str = values["start_time"]["value"]["value"]
        end_str = values["end_time"]["value"]["value"]
        reason = values["reason"]["value"]["value"]

        start_dt = parse_flexible_time(date, start_str)
        end_dt = parse_flexible_time(date, end_str)

        host = get_user_profile(user_id)
        host_email = host["email"]
        host_first_name = host["first_name"]

        print("📥 Visitor (NYC) submitted:")
        print(f"👤 Guest: {guest_name}")
        print(f"📧 Email: {guest_email}")
        print(f"🧑 Host: {host_first_name} ({host_email})")
        print(f"📅 Date: {date}")
        print(f"🕐 Start: {start_dt}")
        print(f"🕔 End: {end_dt}")
        print(f"📝 Reason: {reason}")

        create_event(
            start_dt=start_dt,
            end_dt=end_dt,
            guest_name=guest_name,
            host_first_name=host_first_name,
            host_email=host_email,
            reason=reason
        )

        # DM confirmation to host
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"✅ Your visitor *{guest_name}* has been registered for the NYC office.\n"
                f"📆 {start_dt.strftime('%b %d, %I:%M %p')} – {end_dt.strftime('%I:%M %p')}\n"
                f"📝 *Reason*: {reason}"
            )
        )

        # DM notification to Alanna
        try:
            alanna_info = client.users_lookupByEmail(email="alanna.cooper@anterior.com")
            alanna_id = alanna_info["user"]["id"]

            client.chat_postMessage(
                channel=alanna_id,
                text=(
                    f"🚪 A visitor has been registered for the NYC office:\n"
                    f"👤 *Guest*: {guest_name}\n"
                    f"📅 {start_dt.strftime('%b %d')} from {start_dt.strftime('%I:%M %p')} to {end_dt.strftime('%I:%M %p')}\n"
                    f"📝 *Reason*: {reason}\n"
                    f"🧑 *Host*: {host_first_name}"
                )
            )
        except Exception as e:
            print("⚠️ Failed to DM Alanna:", e)

    except Exception as e:
        print("❌ Exception in handle_submission:", e)

def create_event(start_dt, end_dt, guest_name, host_first_name, host_email, reason):
    calendar_id = "c_0bf55940769b0bb747f5b5d7b7bdcd1cdcb1ee99d56abf367c2da3b6d632ac81@group.calendar.google.com"
    service_account_file = "service-account.json"

    SCOPES = ['https://www.googleapis.com/auth/calendar']
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    ).with_subject("tahseen@anterior.com")

    service = build('calendar', 'v3', credentials=credentials)

    attendees = []
    if host_email:
        attendees.append({"email": host_email})
    attendees.append({"email": "alanna.cooper@anterior.com"})

    event = {
        'summary': f"Visitor (NYC): {guest_name} to see {host_first_name}",
        'description': reason,
        'start': {
            'dateTime': start_dt.isoformat(),
            'timeZone': 'America/New_York',
        },
        'end': {
            'dateTime': end_dt.isoformat(),
            'timeZone': 'America/New_York',
        },
        'attendees': attendees
    }

    created = service.events().insert(calendarId=calendar_id, body=event, sendUpdates='all').execute()
    print("📆 Event created:", created.get("htmlLink"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)