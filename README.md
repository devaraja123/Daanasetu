# DaanaSetu – Intelligent Donation and Community Resource Redistribution System

Flask + SQLite prototype with donor, verified NGO, volunteer-account and admin workflows; open self-service donation pickup; NGO resource requirements; explainable matching; GPS maps; SMTP email; Twilio SMS; in-app notifications and analytics.

## Run in VS Code (Windows PowerShell)
```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```
If activation is blocked: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again. CMD: `venv\Scripts\activate.bat`. Open http://127.0.0.1:5000. Stop with Ctrl+C.

## Demo accounts
| Role | Email | Password |
|---|---|---|
| Admin | admin@daanasetu.local | Admin@123 |
| Donor | donor@demo.com | Donor@123 |
| NGO | ngo@demo.com | Ngo@123 |
| Volunteer | volunteer@demo.com | Volunteer@123 |

## Self-service pickup (updated workflow)
- Any signed-in account (donor, NGO representative, volunteer account or admin) can accept another user's available donation. A user cannot accept their own listing.
- Acceptance reserves the whole listing for one recipient. The donor's phone number and exact GPS coordinates are private until the acceptance succeeds.
- After acceptance, the recipient opens **Pickup details** from the success page or dashboard. They can call the donor, view the map and open Google Maps directions.
- The recipient updates the pickup in order: **Accepted → Pickup in Progress → Collected**. The donor confirms **Completed**. The donor or recipient may cancel before collection; the item becomes available again.
- Only one active pickup is permitted per donation. Cancelled pickup history is retained, and the listing can be accepted again.
- Verified NGOs can publish resource requirements. All registered users can browse those needs; compatible donations are ranked on the Matches page. A verified NGO can accept a suggested match, which creates the same private self-pickup workflow.
- Volunteer delivery scheduling is disabled in this self-service version. A volunteer-role account can still accept and personally collect a donation like any other registered user.

## GPS maps
Donor and NGO forms have **Use my current location**. The browser asks permission and stores coordinates only when the form is submitted. Geolocation works on localhost/127.0.0.1 and HTTPS. Open GPS Map to view verified NGO need locations. Exact donor pickup pins are deliberately not shown on the public map; they are shown only on an accepted pickup's private details page. GPS distance uses straight-line Haversine distance, not road distance/ETA. It falls back to comparing area names if coordinates are unavailable. Volunteer live/background tracking is not included. OpenStreetMap provides map tiles; follow its tile usage policy and use an appropriate provider for production traffic.

## Email via SMTP
1. Open `.env`; set `ENABLE_EMAIL=true`.
2. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`.
3. For Gmail, create an App Password with 2-Step Verification; do not use your normal account password.
4. Restart Flask and trigger a donation acceptance or pickup status update.

## SMS via Twilio
1. Create a Twilio account and configure an SMS-capable sender.
2. Add Account SID, Auth Token and sender number to `.env`.
3. Set `ENABLE_SMS=true`. Recipient numbers must use international format, e.g. `+91XXXXXXXXXX`.
4. Update demo phone numbers in your local database or register your own test account. Restart Flask and trigger an event. Trial accounts can restrict recipients to verified numbers. SMS fees, regulations and country availability apply.

The app sends in-app notifications regardless of provider configuration. When enabled, email/SMS notifications are attempted for donation acceptance, self-pickup progress/cancellation/completion, NGO match acceptance and NGO verification. Missing credentials do not create false delivery success. Provider errors appear in the Flask log.

## Database
The app adds GPS columns to older databases automatically. To reset demo data, stop Flask and delete `daanasetu.db`, then restart. Back up real data first.

## Security and limitations
Change `SECRET_KEY`; never commit `.env` or expose Twilio credentials. Location is optional and consent-based. This is an academic prototype, not proof of NGO authenticity or a certified logistics service. Use synthetic/anonymized data for evaluation. Exclude medicines, hazardous items, recalled goods and specialist-handling perishables. Matching is rule-based, not a trained ML model.

## Account, profile and notification features

- Use **My profile** after login to update your name, phone number, city/service area, private address and profile photo. The page also shows donation and pickup history.
- Change a password from My profile. If you forget it, use **Forgot password?** on the login page. Reset links expire after 30 minutes and can only be used once.
- In local development with SMTP disabled, the forgot-password page displays a development-only reset link. Configure SMTP and `ENABLE_EMAIL=true` in `.env` to send reset links by email. Do not expose development reset links in a public deployment.
- Edit or withdraw a donation from the donor dashboard while its status is **Available**. Donations with an active pickup are locked.
- Use **Notifications** to review in-app notices and email/SMS delivery attempts. Use **Alerts setup** to choose in-app, email and SMS preferences. Email/SMS still require administrator-side provider configuration in `.env`.


## Feedback feature
Log in and open **Feedback** in the navigation bar. Submit a 1–5 star rating and comment, view your feedback and recent community reviews. Completed pickup pages also link to a pickup-specific review. The `feedback` table is created automatically when the app starts.


## Delivery verification and post-delivery feedback
1. Recipient accepts an available donation. The donor gets a unique 6-digit code in their private in-app Notifications.
2. Recipient opens Pickup Details, selects **I am going to collect**, and asks the donor for the code at handover.
3. Recipient enters the code in Pickup Details and selects **Verify code & mark collected**. Incorrect codes do not mark the item collected.
4. Donor selects **Confirm handover completed**; the donation is now Completed.
5. Both participants may leave one rating and written feedback for the completed pickup. Before completion, feedback is unavailable.

**Security note:** Codes are stored as hashes in pickup claims and delivered via in-app notifications to the donor. Use HTTPS, a strong SECRET_KEY, and Flask debug=False in production. This demonstration app does not implement rate limiting for code attempts; add it before public deployment.


## Email OTP at registration
Copy `.env.example` to `.env` and set `SMTP_USER`, `SMTP_PASSWORD` (Gmail 16-character App Password), `SMTP_FROM`, and a strong `SECRET_KEY`. Keep `.env` private. New accounts are stored in `pending_registrations` until the email code is verified. Codes expire after 10 minutes, allow 5 attempts, and can be resent after 60 seconds. No SMS is required for registration. Existing users can still log in.
