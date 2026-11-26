# KL ERP Live Internals Calculator

A Flask web app where any KL University student can enter their Student ID, password, and login CAPTCHA to fetch their Course Handout **Evaluation Plans** and **Internal Marks** directly from `newerp.kluniversity.in` and compute weighted internals (ignoring negative values `-1`/`-2` and marking missing components as `Not uploaded`).

## Run Locally
```bash
pip install -r requirements.txt
python app.py
```
Then open `http://localhost:5000`.

## Deploy Online (Render / Railway)
1. Push this folder to a GitHub repo.
2. Create a new **Web Service** on [Render](https://render.com) or [Railway](https://railway.app) connected to that repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
