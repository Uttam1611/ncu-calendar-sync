# NCU MyCampus → Google Calendar sync (starter)

This is a local-first starter for syncing the NCU timetable API into a separate Google Calendar named `College`. Events use `transparency: opaque`, which makes them show as **Busy** in Google Calendar.

## Important limitations

- The observed login endpoint is `https://uatapi.ncuindia.edu/api/Authentication/ValidateUser`. Its request fields are `UserName`, `Password`, `IpAddress`, and `UserType`. The starter `.env.example` is preconfigured with those names; do not send credentials or tokens in chat.
- The endpoint observed is on `uatapi.ncuindia.edu`. Verify that it is the endpoint used in normal portal sessions before depending on it long-term.
- The first version does not automatically delete events missing from an API response. That is deliberate protection against accidental deletion if the API returns an incomplete timetable.
- The sync key uses date, course, room and faculty. If the API can return two identical classes on the same date, the key function must be extended with a stable unique field.
- This is a starter; test with a small date range or a disposable Google calendar before relying on it.

## 1. Set up Python on Windows

Open PowerShell in this folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope Process Bypass` in that PowerShell window, then activate again.

## 2. Configure NCU API details

Open `.env` in a text editor.

- `NCU_LOGIN_URL` is set to the observed `ValidateUser` endpoint.
- `NCU_USERNAME_FIELD=UserName` and `NCU_PASSWORD_FIELD=Password` match the observed JSON property names.
- `NCU_LOGIN_EXTRA_JSON={"IpAddress":"","UserType":""}` matches the observed additional fields. If the portal changes its request format, verify it in DevTools without sharing secret values.
- Keep the schedule URL as `https://uatapi.ncuindia.edu/api/myapp/Student/GetSchedule` if this is the endpoint your portal currently uses.
- The script assumes `ValidateUser` returns JSON fields `token` and `User.UserId`, and that `GetSchedule` accepts `userId` as a query parameter. Change `.env` field-name settings if the response differs.

The script prompts for the portal username/password on first run and stores them using the operating system's keyring. If keyring is unavailable on your machine, fix the keyring backend rather than putting your password into the script.

## 3. Configure Google Calendar API

1. Open Google Cloud Console: https://console.cloud.google.com/
2. Create/select a project and enable **Google Calendar API**.
3. Configure the OAuth consent screen for your own use.
4. Create an OAuth client ID of type **Desktop app**.
5. Download its JSON and save it as `credentials.json` in this folder.
6. Keep `credentials.json` private and never commit it.

On first run, a browser window opens for Google authorization. A local `google_token.json` is created; keep it private.

## 4. Run a test sync

```powershell
python ncu_sync.py
```

The script creates the `College` calendar if it does not exist, then creates or updates class events. It does not print credentials or access tokens.

## 5. Make it automatic only after validating

After confirming event dates, times, rooms, duplicates and Busy status, use Windows Task Scheduler to run `python.exe` with argument `ncu_sync.py` from this folder every few hours. Configure the task to run under the same Windows account that has the keyring and Google OAuth token files.

Do not run it every minute; timetable changes do not require that. Start with every 4–6 hours and adjust if needed.

## Security notes

- Never share `.env`, `credentials.json`, `google_token.json`, `sync_state.json`, raw cookies, Authorization headers or login request payloads.
- Do not commit these files to Git.
- If you pasted a real password or token into a public location, change/revoke it.
