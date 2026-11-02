"""Sync NCU MyCampus class schedule to a dedicated Google Calendar.

Security: portal credentials are stored in the operating system keyring. Never
print or share passwords, bearer tokens, cookies, or full login request/response
bodies. Configure the exact ValidateUser endpoint and JSON field names in .env.
"""

import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import keyring
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()
SCOPES = ["https://www.googleapis.com/auth/calendar"]
SERVICE_NAME = "ncu-google-calendar-sync"
USERNAME_KEY = "ncu_username"
DEFAULT_TIMEZONE = "Asia/Kolkata"


def env(name, default=None):
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


def required(name):
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing {name}. Copy .env.example to .env and configure it.")
    return value


def load_json_env(name, default):
    try:
        return json.loads(env(name, default))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must contain valid JSON: {exc.msg}.") from exc


def validate_https(url, label):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{label} must be a valid HTTPS URL.")
    return url


def safe_endpoint(url):
    """Return host + path only, never query parameters that might contain IDs/tokens."""
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}"


def get_portal_credentials():
    username = keyring.get_password(SERVICE_NAME, USERNAME_KEY)
    if not username:
        username = input("NCU portal username: ").strip()
        if not username:
            raise RuntimeError("Username cannot be blank.")
        keyring.set_password(SERVICE_NAME, USERNAME_KEY, username)

    password = keyring.get_password(SERVICE_NAME, username)
    if not password:
        password = getpass.getpass("NCU portal password (input hidden): ")
        if not password:
            raise RuntimeError("Password cannot be blank.")
        keyring.set_password(SERVICE_NAME, username, password)
    return username, password


def raise_http_error(stage, response, endpoint):
    """Raise a useful but secret-safe error without dumping response bodies."""
    status = response.status_code
    content_type = response.headers.get("Content-Type", "unknown").split(";")[0]
    print(f"{stage} HTTP status: {status}")
    print(f"{stage} endpoint: {safe_endpoint(endpoint)}")
    print(f"{stage} response content type: {content_type}")
    if status in (401, 403):
        hint = "Check the endpoint, login payload field names, credentials, and required headers."
    elif status == 404:
        hint = "The configured endpoint may be wrong or the UAT endpoint may not be the production API."
    elif status >= 500:
        hint = "The server returned an error; verify the portal/API environment and try again later."
    else:
        hint = "Check the endpoint and request format against the browser Network panel."
    raise RuntimeError(f"{stage} failed with HTTP {status}. {hint}")


def portal_login(session):
    login_url = validate_https(required("NCU_LOGIN_URL"), "NCU_LOGIN_URL")
    username, password = get_portal_credentials()
    user_field = required("NCU_USERNAME_FIELD")
    pass_field = required("NCU_PASSWORD_FIELD")

    payload = load_json_env("NCU_LOGIN_EXTRA_JSON", "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("NCU_LOGIN_EXTRA_JSON must be a JSON object.")
    payload[user_field] = username
    payload[pass_field] = password

    print("Authenticating with NCU portal...")
    try:
        response = session.post(
            login_url,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://mycampus.ncuindia.edu",
                "Referer": "https://mycampus.ncuindia.edu/",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the NCU login endpoint ({type(exc).__name__}).") from None

    if not response.ok:
        raise_http_error("NCU login", response, login_url)

    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            "NCU login endpoint returned a non-JSON response. Confirm NCU_LOGIN_URL is the ValidateUser API endpoint."
        ) from None

    if not isinstance(body, dict):
        raise RuntimeError("NCU login response JSON was not an object.")

    token_field = env("NCU_TOKEN_FIELD", "token")
    user_object_field = env("NCU_USER_OBJECT_FIELD", "User")
    user_id_field = env("NCU_USER_ID_FIELD", "UserId")
    token = body.get(token_field)
    user_obj = body.get(user_object_field) or {}
    user_id = user_obj.get(user_id_field) if isinstance(user_obj, dict) else None

    if not token or not user_id:
        # Do not print the body: it can contain personal data or credentials/tokens.
        top_level_fields = ", ".join(sorted(str(k) for k in body.keys()))
        nested_fields = ", ".join(sorted(str(k) for k in user_obj.keys())) if isinstance(user_obj, dict) else "not an object"
        raise RuntimeError(
            "Login request returned HTTP success, but expected fields were not found. "
            f"Top-level response field names: [{top_level_fields}]. "
            f"Fields under {user_object_field!r}: [{nested_fields}]. "
            "Only field names are shown; response values are withheld. Verify .env field names."
        )

    scheme = env("NCU_AUTH_SCHEME", "Bearer")
    session.headers.update({"Authorization": f"{scheme} {token}", "Accept": "application/json"})
    print("NCU authentication succeeded.")
    return str(user_id)


def fetch_schedule(session, user_id):
    schedule_url = validate_https(required("NCU_SCHEDULE_URL"), "NCU_SCHEDULE_URL")
    print("Fetching class schedule...")
    try:
        response = session.get(schedule_url, params={"userId": user_id}, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach the NCU schedule endpoint ({type(exc).__name__}).") from None

    if not response.ok:
        raise_http_error("NCU schedule", response, schedule_url)
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError("GetSchedule returned non-JSON content. Check the schedule endpoint.") from None

    if not isinstance(body, dict):
        raise RuntimeError("GetSchedule response JSON was not an object.")
    data = body.get("Data", body.get("data"))
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            raise RuntimeError("The schedule response's Data field was not valid JSON.") from None
    if not isinstance(data, dict):
        fields = ", ".join(sorted(str(k) for k in body.keys()))
        raise RuntimeError(f"Schedule response has no Data object. Top-level field names: [{fields}].")

    classes = data.get("Table1", [])
    if not isinstance(classes, list):
        raise RuntimeError("Expected Data.Table1 to be a list of class sessions.")
    print(f"Schedule response received: {len(classes)} raw class records.")
    return classes


def parse_dt(date_text, time_text):
    date_formats = ("%d-%b-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
    parsed_date = None
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(str(date_text).strip(), fmt).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise ValueError(f"Unsupported date format: {date_text!r}")

    time_value = str(time_text).strip()
    parsed_time = None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_time = datetime.strptime(time_value, fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        raise ValueError(f"Unsupported time format: {time_text!r}")
    return datetime.combine(parsed_date, parsed_time)


def clean(value):
    return str(value or "").strip()


def class_key(item):
    # Date/course/room/faculty identifies a session and survives time-only changes.
    raw = "|".join([
        clean(item.get("LdATE")),
        clean(item.get("CourseDetails")),
        clean(item.get("ClassRoomNo")),
        clean(item.get("FACULTYNAME")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def to_event(item):
    date_text = clean(item.get("LdATE"))
    start_text = clean(item.get("StartTime"))
    end_text = clean(item.get("endtime"))
    course = clean(item.get("CourseDetails")) or "College class"
    faculty = clean(item.get("FACULTYNAME"))
    room = clean(item.get("ClassRoomNo"))
    if not date_text or not start_text or not end_text:
        return None

    timezone = env("TIMEZONE", DEFAULT_TIMEZONE)
    tz = ZoneInfo(timezone)
    start = parse_dt(date_text, start_text).replace(tzinfo=tz)
    end = parse_dt(date_text, end_text).replace(tzinfo=tz)
    if end <= start:
        raise ValueError(f"Class end time is not after start time: {course} on {date_text}")

    key = class_key(item)
    description = ["Automatically synced from NCU MyCampus."]
    if faculty:
        description.append(f"Faculty: {faculty}")
    if room:
        description.append(f"Room: {room}")

    return {
        "summary": f"College · {course}",
        "description": "\n".join(description),
        "location": room,
        "start": {"dateTime": start.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        "transparency": "opaque",  # opaque means Busy in Google Calendar
        "extendedProperties": {"private": {"ncuSyncKey": key}},
    }


def google_service():
    client_file = required("GOOGLE_CLIENT_SECRET_FILE")
    token_file = env("GOOGLE_TOKEN_FILE", "google_token.json")
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            if not Path(client_file).exists():
                raise RuntimeError(
                    f"Missing {client_file}. Create a Google OAuth Desktop app client in Google Cloud Console and save it here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_or_create_calendar(service, name):
    page_token = None
    while True:
        result = service.calendarList().list(pageToken=page_token).execute()
        for calendar in result.get("items", []):
            if calendar.get("summary") == name:
                return calendar["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    created = service.calendars().insert(body={
        "summary": name,
        "timeZone": env("TIMEZONE", DEFAULT_TIMEZONE),
    }).execute()
    print(f"Created Google Calendar: {name}")
    return created["id"]



def list_calendar_events(service, calendar_id, time_min, time_max):
    events = {}
    page_token = None

    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            maxResults=2500,
            pageToken=page_token,
        ).execute()

        for event in result.get("items", []):
            private_properties = (
                event.get("extendedProperties", {}).get("private", {}) or {}
            )

            key = private_properties.get("ncuSyncKey")

            if key:
                events[key] = event

        page_token = result.get("nextPageToken")

        if not page_token:
            break

    return events



def normalized_start_end(event):
    """Compare the meaningful timestamp/time-zone values, ignoring API formatting differences."""
    start = event.get("start", {})
    end = event.get("end", {})
    return (
        start.get("dateTime", start.get("date")), start.get("timeZone"),
        end.get("dateTime", end.get("date")), end.get("timeZone"),
    )


def sync():
    session = requests.Session()
    user_id = portal_login(session)
    classes = fetch_schedule(session, user_id)

    parsed = []
    for item in classes:
        if not isinstance(item, dict):
            continue
        try:
            event = to_event(item)
            if event:
                parsed.append((class_key(item), event))
        except (ValueError, TypeError, KeyError) as exc:
            print(f"Skipping one malformed class record ({type(exc).__name__}): {exc}")

    if not parsed:
        raise RuntimeError(
            "No class sessions were parsed. Refusing to modify Google Calendar. "
            "Check that Data.Table1 contains StartTime, endtime, LdATE and CourseDetails fields."
        )

    # Avoid inserting the same session twice if the API response repeats a record.
    deduplicated = {}
    for key, event in parsed:
        deduplicated[key] = event
    parsed = list(deduplicated.items())

    service = google_service()
    calendar_name = env("GOOGLE_CALENDAR_NAME", "College")
    calendar_id = get_or_create_calendar(service, calendar_name)
    state_path = env("SYNC_STATE_FILE", "sync_state.json")

    starts = [datetime.fromisoformat(event["start"]["dateTime"]) for _, event in parsed]
    ends = [datetime.fromisoformat(event["end"]["dateTime"]) for _, event in parsed]
    min_dt = min(starts) - timedelta(days=1)
    max_dt = max(ends) + timedelta(days=1)
    existing = list_calendar_events(
        service, calendar_id, min_dt.isoformat(), max_dt.isoformat()
    )

    created = updated = unchanged = 0
    current_keys = set()
    for key, event in parsed:
        current_keys.add(key)
        old = existing.get(key)
        if old:
            fields_same = (
                old.get("summary") == event.get("summary")
                and old.get("description", "") == event.get("description", "")
                and old.get("location", "") == event.get("location", "")
                and old.get("transparency", "opaque") == event.get("transparency", "opaque")
                and normalized_start_end(old) == normalized_start_end(event)
            )
            if fields_same:
                unchanged += 1
                continue
            service.events().update(
                calendarId=calendar_id, eventId=old["id"], body=event
            ).execute()
            updated += 1
        else:
            service.events().insert(calendarId=calendar_id, body=event).execute()
            created += 1

    print(f"Sync complete for {len(parsed)} class sessions.")
    print(f"Created: {created} | Updated: {updated} | Unchanged: {unchanged}")
    print(f"Calendar: {calendar_name}")
    print("Missing/cancelled classes are not automatically deleted for safety.")



if __name__ == "__main__":
    try:
        sync()

    except HttpError as exc:
        print(f"\nGoogle Calendar API error: HTTP {exc.resp.status}")

        try:
            import json

            details = json.loads(exc.content.decode("utf-8"))
            error = details.get("error", {})

            print(
                "Google API message:",
                error.get("message", "No message provided")
            )

            for item in error.get("errors", []):
                print(
                    "Reason:",
                    item.get("reason", "unknown"),
                    "| Message:",
                    item.get("message", "")
                )

        except Exception:
            print("Could not parse Google's error response.")

        sys.exit(1)

    except requests.RequestException as exc:
        print(f"\nNetwork/API request failed: {type(exc).__name__}: {exc}")
        sys.exit(1)

    except Exception as exc:
        print(f"\nSync failed: {type(exc).__name__}: {exc}")
        sys.exit(1)

