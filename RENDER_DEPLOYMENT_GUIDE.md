# Deploy DaanaSetu to Render

1. Upload the **contents** of this folder to the root of a private GitHub repository. (Or upload the folder itself and set Render Root Directory to `DaanaSetu_Delivery_Verification`.)
2. Create Render > New > Web Service > Connect repository.
3. Choose Python 3, Python version 3.13.9 via environment variable `PYTHON_VERSION`.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn --bind 0.0.0.0:$PORT wsgi:app`
6. Add `SECRET_KEY` with a strong random value. Set `ENABLE_DEMO_DATA=false`.
7. Deploy and visit `/health` then `/`.

## Important limitations
- Render free web services have ephemeral filesystem: local SQLite data and uploaded files can disappear on restarts/redeploys. This is for a **temporary demonstration only**. Use persistent database and file storage before relying on user data. `DAANASETU_DATA_DIR` can relocate SQLite if using a paid persistent disk, but uploads also require persistence.
- Gmail SMTP using port 587/465 does not work on Render free. The app's current registration email OTP requires a supported HTTPS email API implementation or hosting with outbound SMTP.
- Demo accounts are disabled by default because original code used predictable passwords. For a private temporary demo only, set `ENABLE_DEMO_DATA=true`; never expose that configuration publicly.
- Do not upload `.env`, API keys, passwords or a real user database to GitHub.
- Python dependency versions are based on the uploaded ZIP; the application has not been tested end-to-end on Render.
