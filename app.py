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
    options = [(re.sub(r"\s+", " ", opt.get_text(strip=True).lower()), opt.get("value", "")) for opt in select_el.find_all("option")]

    for txt, val in options:
        if txt == target_norm or target_norm in txt:
            return val

    # Fallback for 2026-2027 vs 2026-27
    compact = target_norm.replace("-20", "-")
    for txt, val in options:
        if compact in txt.replace("-20", "-"):
            return val

    # Fallback for "Odd Sem" vs "Odd" / "Even Sem" vs "Even"
    first_word = target_norm.split(" ")[0]
    if first_word:
        for txt, val in options:
            if txt == first_word or first_word in txt:
                return val
    return None


def submit_year_sem_form(session, url, academic_year, semester):
    resp = session.get(url, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    # Pick the form that actually has <select> dropdowns (skipping header/theme forms)
    form = next((f for f in soup.find_all("form") if f.find("select")), None)
    if not form:
        return soup

    payload = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("button", "reset"):
            continue
        payload[name] = inp.get("value", "")

    for btn in form.find_all("button"):
        bname = btn.get("name")
        if bname and (btn.get("type") or "submit").lower() == "submit":
            payload[bname] = btn.get("value", "")

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


def expand_table_rows(table):
    """Expand rowspan/colspan into a normalized 2D list of cell texts."""
    grid = []
    spans = {}  # (row_idx, col_idx) -> text
    rows = table.find_all("tr")
    for r_idx, tr in enumerate(rows):
        row_cells = []
        c_idx = 0
        for cell in tr.find_all(["th", "td"]):
            while (r_idx, c_idx) in spans:
                row_cells.append(spans.pop((r_idx, c_idx)))
                c_idx += 1
            text = cell.get_text(" ", strip=True)
            rowspan = int(cell.get("rowspan") or 1)
            colspan = int(cell.get("colspan") or 1)
            for dc in range(colspan):
                row_cells.append(text)
                for dr in range(1, rowspan):
                    spans[(r_idx + dr, c_idx + dc)] = text
            c_idx += colspan
        while (r_idx, c_idx) in spans:
            row_cells.append(spans.pop((r_idx, c_idx)))
            c_idx += 1
        grid.append(row_cells)
    return grid


def parse_evaluation_plan(handout_soup):
    """Extract In-Semester (internal) Evaluation Plan components from a handout page."""
    components = {}
    for table in handout_soup.find_all("table"):
        grid = expand_table_rows(table)
        if len(grid) < 2:
            continue

        header_idx = next(
            (idx for idx, row in enumerate(grid) if any("evaluation component" in c.lower() for c in row)),
            None,
        )
        if header_idx is None:
            continue

        headers = [h.lower() for h in grid[header_idx]]
        idx_type = next((i for i, h in enumerate(headers) if "evaluation type" in h), None)
        idx_comp = next((i for i, h in enumerate(headers) if "evaluation component" in h), None)
        idx_metric = next((i for i, h in enumerate(headers) if "metric" in h), None)
        idx_val = next((i for i, h in enumerate(headers) if h == "value"), None)
        if idx_comp is None or idx_metric is None or idx_val is None:
            continue

        for cells in grid[header_idx + 1 :]:
            if len(cells) <= max(idx_comp, idx_metric, idx_val):
                continue
            eval_type = cells[idx_type] if idx_type is not None and idx_type < len(cells) else ""
            eval_norm = re.sub(r"[-\s]+", " ", eval_type.lower())
            if "in semester" not in eval_norm:
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
                    "type": "Formative" if "formative" in eval_norm else "Summative",
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


def extract_ajax_html(resp):
    """Handle both raw HTML and Yii2 crudjax JSON responses ({title, content})."""
    text = resp.text or ""
    if text.lstrip().startswith("{"):
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                text = payload.get("content") or payload.get("html") or payload.get("body") or text
        except ValueError:
            pass
    return BeautifulSoup(text, "html.parser")


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
        raw_href = ""
        link = tr.find("a", href=True) or tr.find(attrs={"data-url": True})
        if link:
            raw_href = link.get("href") or link.get("data-url") or ""
        if not raw_href or raw_href == "#":
            for el in tr.find_all(["a", "button", "span"]):
                oc = el.get("onclick") or ""
                m = re.search(r"(index\.php\?[^'\"\s\)]+)", oc)
                if m:
                    raw_href = m.group(1)
                    break
        if not raw_href or raw_href == "#":
            continue
        handout_url = urljoin(HANDOUTS_URL, raw_href)
        h_resp = session.get(
            handout_url,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": HANDOUTS_URL},
            timeout=20,
        )
        h_soup = extract_ajax_html(h_resp)
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
                c_resp = session.get(
                    matched_url,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": INTERNALS_URL},
                    timeout=20,
                )
                c_soup = extract_ajax_html(c_resp)
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
            errs = [el.get_text(" ", strip=True) for el in login_soup.select(".help-block, .help-block-error, .alert-danger") if el.get_text(strip=True)]
            err_msg = " | ".join(dict.fromkeys(errs)) if errs else "Invalid ID, password, or CAPTCHA."
            return jsonify({"error": err_msg}), 401

        handouts_map = fetch_handouts_map(sess, academic_year, semester)
        courses = fetch_internals_data(sess, academic_year, semester, handouts_map)

        debug_info = None
        if not courses:
            h_soup = submit_year_sem_form(sess, HANDOUTS_URL, academic_year, semester)
            i_soup = submit_year_sem_form(sess, INTERNALS_URL, academic_year, semester)
            h_rows = h_soup.find_all("tr")
            debug_info = {
                "handout_courses_parsed": list(handouts_map.keys()),
                "handout_rows": len(h_rows),
                "internal_rows": len(i_soup.find_all("tr")),
                "sample_handout_row": str(h_rows[1])[:400] if len(h_rows) > 1 else None,
            }

        return jsonify(
            {
                "student_id": username,
                "academic_year": academic_year,
                "semester": semester,
                "courses": courses,
                "debug": debug_info,
            }
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"Error communicating with KL ERP: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
