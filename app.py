import base64
import os
import re
import time
import uuid
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BASE_URL = "https://newerp.kluniversity.in"
LOGIN_URL = f"{BASE_URL}/index.php"
HANDOUTS_URL = f"{BASE_URL}/index.php?r=studentinfo%2Fstudentcoursemappingmasterinfoview%2Fgetstudentcourseshandouts"
INTERNALS_URL = f"{BASE_URL}/index.php?r=studentinfo%2Fstudentendexamresult%2Fgetstudentinternalmarks"

# In-memory short-lived login sessions (never persisted to disk)
SESSIONS = {}
SESSION_TTL_SECONDS = 900


def cleanup_sessions():
    now = time.time()
    expired = [k for k, v in SESSIONS.items() if now - v["created_at"] > SESSION_TTL_SECONDS]
    for k in expired:
        SESSIONS.pop(k, None)


def find_option_value(select_el, target_text):
    target_norm = re.sub(r"\s+", " ", target_text.strip().lower())
    for opt in select_el.find_all("option"):
        txt = re.sub(r"\s+", " ", opt.get_text(strip=True).lower())
        val = opt.get("value", "")
        if txt == target_norm or target_norm in txt:
            return val
    # Fallback for 2026-2027 vs 2026-27
    compact = target_norm.replace("-20", "-")
    for opt in select_el.find_all("option"):
        txt = re.sub(r"\s+", " ", opt.get_text(strip=True).lower()).replace("-20", "-")
        if compact in txt:
            return opt.get("value", "")
    return None


def submit_year_sem_form(session, url, academic_year, semester):
    resp = session.get(url, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    if not form:
        return soup

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("submit", "button", "reset"):
            continue
        payload[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        lname = name.lower()
        if "year" in lname or "acad" in lname or "batch" in lname:
            val = find_option_value(sel, academic_year)
            if val is not None:
                payload[name] = val
        elif "sem" in lname:
            val = find_option_value(sel, semester)
            if val is not None:
                payload[name] = val

    action = form.get("action") or url
    post_url = urljoin(url, action)
    method = (form.get("method") or "post").lower()
    if method == "get":
        r2 = session.get(post_url, params=payload, timeout=20)
    else:
        r2 = session.post(post_url, data=payload, timeout=20)
    return BeautifulSoup(r2.text, "html.parser")


def parse_evaluation_plan(handout_soup):
    """Extract In-Semester (internal) Evaluation Plan components from a handout page."""
    components = {}
    for table in handout_soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [c.get_text(strip=True).lower() for c in first_row.find_all(["th", "td"])]
        if not any("evaluation component" in h for h in headers):
            continue

        idx_type = next((i for i, h in enumerate(headers) if "evaluation type" in h), None)
        idx_comp = next((i for i, h in enumerate(headers) if "evaluation component" in h), None)
        idx_metric = next((i for i, h in enumerate(headers) if "metric" in h), None)
        idx_val = next((i for i, h in enumerate(headers) if h == "value"), None)
        if idx_comp is None or idx_metric is None or idx_val is None:
            continue

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) <= max(idx_comp, idx_metric, idx_val):
                continue
            eval_type = cells[idx_type] if idx_type is not None and idx_type < len(cells) else ""
            if "in semester" not in eval_type.lower():
                continue

            comp_name = cells[idx_comp]
            metric = cells[idx_metric].lower()
            try:
                val = float(cells[idx_val])
            except ValueError:
                continue

            entry = components.setdefault(
                comp_name,
                {
                    "name": comp_name,
                    "type": "Formative" if "formative" in eval_type.lower() else "Summative",
                    "maxMarks": None,
                    "weight": None,
                },
            )
            if "weight" in metric:
                entry["weight"] = val
            elif "max" in metric:
                entry["maxMarks"] = val

    return [c for c in components.values() if c["maxMarks"] and c["weight"]]


def parse_component_marks(comp_soup):
    """Sum individual question/CO marks from a component table, ignoring negative values."""
    text = comp_soup.get_text(" ", strip=True)
    if "no results found" in text.lower():
        return None

    for table in comp_soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not headers:
            continue
        lower_h = [h.lower() for h in headers]
        if "max marks" not in lower_h or "student uniid" not in lower_h:
            continue

        idx_uniid = lower_h.index("student uniid")
        idx_max = lower_h.index("max marks")
        rows = table.find_all("tr")
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) <= idx_max:
                continue
            q_cells = cells[idx_uniid + 1 : idx_max]
            total = 0.0
            found_num = False
            for c in q_cells:
                try:
                    num = float(c)
                    found_num = True
                    if num >= 0:
                        total += num
                except ValueError:
                    pass
            if found_num:
                return round(total, 2)
    return None


def fetch_handouts_map(session, academic_year, semester):
    soup = submit_year_sem_form(session, HANDOUTS_URL, academic_year, semester)
    handouts = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        # Find course code pattern like 24EC3112, 24EDS3101A, etc.
        code = next((t for t in texts if re.match(r"^[0-9]{2}[A-Z]{2,5}[0-9]{2,4}[A-Z0-9]*$", t)), None)
        if not code:
            continue
        link = tr.find("a", href=True)
        if not link:
            continue
        handout_url = urljoin(HANDOUTS_URL, link["href"])
        h_resp = session.get(handout_url, timeout=20)
        h_soup = BeautifulSoup(h_resp.text, "html.parser")
        plan = parse_evaluation_plan(h_soup)
        if plan:
            handouts[code] = plan

    return handouts


def fetch_internals_data(session, academic_year, semester, handouts_map):
    soup = submit_year_sem_form(session, INTERNALS_URL, academic_year, semester)
    courses = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        code = next((t for t in texts if re.match(r"^[0-9]{2}[A-Z]{2,5}[0-9]{2,4}[A-Z0-9]*$", t)), None)
        if not code:
            continue
        # Skip courses without a handout (e.g., non-credited courses)
        if code not in handouts_map:
            continue

        code_idx = texts.index(code)
        course_name = texts[code_idx + 1] if code_idx + 1 < len(texts) else code

        # Extract component links from the last cell
        comp_links = {}
        for a in tr.find_all("a", href=True):
            comp_label = a.get_text(strip=True)
            comp_url = urljoin(INTERNALS_URL, a["href"])
            comp_links[comp_label.lower()] = comp_url

        course_components = []
        for plan_item in handouts_map[code]:
            cname = plan_item["name"]
            matched_url = comp_links.get(cname.lower())
            if not matched_url:
                # Fuzzy fallback match
                for k, u in comp_links.items():
                    if cname.lower() in k or k in cname.lower():
                        matched_url = u
                        break

            raw_val = None
            if matched_url:
                c_resp = session.get(matched_url, timeout=20)
                c_soup = BeautifulSoup(c_resp.text, "html.parser")
                raw_val = parse_component_marks(c_soup)

            course_components.append(
                {
                    "name": cname,
                    "type": plan_item["type"],
                    "maxMarks": plan_item["maxMarks"],
                    "weight": plan_item["weight"],
                    "raw": raw_val,
                    "status": "uploaded" if raw_val is not None else "not_uploaded",
                }
            )

        courses.append(
            {
                "code": code,
                "name": course_name,
                "components": course_components,
            }
        )

    return courses


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/captcha", methods=["GET"])
def get_captcha():
    cleanup_sessions()
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    try:
        r = sess.get(LOGIN_URL, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        login_form = soup.find("form", {"id": "login-form"}) or soup
        csrf_el = login_form.find("input", {"name": "_csrf"})
        csrf_token = csrf_el["value"] if csrf_el else ""
        form_action = login_form.get("action") if login_form.name == "form" else None
        login_post_url = urljoin(LOGIN_URL, form_action or "/index.php?r=site%2Flogin")

        img_el = soup.find("img", {"id": "loginFormCaptcha-image"})
        if not img_el or not img_el.get("src"):
            return jsonify({"error": "Could not locate KL ERP CAPTCHA image."}), 502

        captcha_url = urljoin(LOGIN_URL, img_el["src"])
        cap_resp = sess.get(captcha_url, timeout=20, headers={"Referer": LOGIN_URL})
        b64 = base64.b64encode(cap_resp.content).decode("ascii")
        mime = cap_resp.headers.get("Content-Type", "image/png")

        sid = str(uuid.uuid4())
        SESSIONS[sid] = {
            "session": sess,
            "csrf": csrf_token,
            "login_post_url": login_post_url,
            "created_at": time.time(),
        }
        return jsonify({"session_id": sid, "captcha_url": f"data:{mime};base64,{b64}"})
    except requests.RequestException as exc:
        return jsonify({"error": f"Failed to reach KL ERP: {exc}"}), 502


@app.route("/api/fetch", methods=["POST"])
def fetch_internals():
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id", "")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    captcha = (data.get("captcha") or "").strip()
    academic_year = (data.get("academic_year") or "2026-2027").strip()
    semester = (data.get("semester") or "Odd Sem").strip()

    if not sid or sid not in SESSIONS:
        return jsonify({"error": "Session expired. Please refresh the CAPTCHA and try again."}), 400
    if not username or not password or not captcha:
        return jsonify({"error": "Student ID, password, and CAPTCHA are required."}), 400

    entry = SESSIONS.pop(sid)
    sess = entry["session"]

    try:
        login_payload = {
            "_csrf": entry["csrf"],
            "LoginForm[username]": username,
            "LoginForm[password]": password,
            "LoginForm[captcha]": captcha,
            "LoginForm[qr_code]": "",
            "login-button": "",
        }
        login_resp = sess.post(
            entry["login_post_url"],
            data=login_payload,
            headers={"Referer": LOGIN_URL, "Origin": BASE_URL},
            timeout=20,
            allow_redirects=True,
        )
        login_soup = BeautifulSoup(login_resp.text, "html.parser")

        if login_soup.find("input", {"name": "LoginForm[username]"}):
            errs = [el.get_text(" ", strip=True) for el in login_soup.select(".help-block-error, .alert-danger") if el.get_text(strip=True)]
            err_msg = " | ".join(errs) if errs else "Invalid ID, password, or CAPTCHA."
            return jsonify({"error": err_msg}), 401

        handouts_map = fetch_handouts_map(sess, academic_year, semester)
        courses = fetch_internals_data(sess, academic_year, semester, handouts_map)

        return jsonify(
            {
                "student_id": username,
                "academic_year": academic_year,
                "semester": semester,
                "courses": courses,
            }
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"Error communicating with KL ERP: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
