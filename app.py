import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import threading
import time
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

app = Flask(__name__)

BASE_URL = "https://newerp.kluniversity.in"
LOGIN_URL = f"{BASE_URL}/index.php"
HANDOUTS_URL = f"{BASE_URL}/index.php?r=studentinfo%2Fstudentcoursemappingmasterinfoview%2Fgetstudentcourseshandouts"
INTERNALS_URL = f"{BASE_URL}/index.php?r=studentinfo%2Fstudentendexamresult%2Fgetstudentinternalmarks"

SERIALIZER = URLSafeTimedSerializer(os.environ.get("SECRET_KEY", "kl-internals-stateless-session-v10"))
SESSION_TTL_SECONDS = 1800
STATS_FILE = os.path.join(os.path.dirname(__file__), "visit_stats.json")
STATS_LOCK = threading.Lock()
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
BASE_VISITS = int(os.environ.get("BASE_VISITS", "0"))
BASE_CALCS = int(os.environ.get("BASE_CALCS", "0"))


def _upstash_cmd(cmd_parts):
    r = requests.post(
        UPSTASH_URL,
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=cmd_parts,
        timeout=3,
    )
    return r.json().get("result")


def update_stats(inc_visits=0, inc_calcs=0):
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            v = int(_upstash_cmd(["INCRBY", "kl_visits", inc_visits]) if inc_visits else (_upstash_cmd(["GET", "kl_visits"]) or 0))
            c = int(_upstash_cmd(["INCRBY", "kl_calcs", inc_calcs]) if inc_calcs else (_upstash_cmd(["GET", "kl_calcs"]) or 0))
            return {"visits": BASE_VISITS + v, "calculations": BASE_CALCS + c}
        except Exception:
            pass

    with STATS_LOCK:
        stats = {"visits": 0, "calculations": 0}
        try:
            if os.path.exists(STATS_FILE):
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    stats["visits"] = int(loaded.get("visits", 0))
                    stats["calculations"] = int(loaded.get("calculations", 0))
        except Exception:
            pass
        stats["visits"] += inc_visits
        stats["calculations"] += inc_calcs
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(stats, f)
        except Exception:
            pass
        return {"visits": BASE_VISITS + stats["visits"], "calculations": BASE_CALCS + stats["calculations"]}


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
            rowspan = int(str(cell.get("rowspan") or 1).strip() or 1)
            colspan = int(str(cell.get("colspan") or 1).strip() or 1)
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
        idx_wm = next((i for i, h in enumerate(headers) if "weightage/marks" in h), None)
        idx_metric = idx_wm if idx_wm is not None else next((i for i, h in enumerate(headers) if "metric" in h), None)
        idx_val = (idx_wm + 1) if idx_wm is not None else next((i for i, h in enumerate(headers) if h == "value"), None)
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


def fetch_handouts_map(session, academic_year, semester, preloaded_soup=None):
    soup = preloaded_soup if preloaded_soup is not None else submit_year_sem_form(session, HANDOUTS_URL, academic_year, semester)
    handouts = {}
    targets = []

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        texts = [c.get_text(strip=True) for c in cells]
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
        targets.append((code, urljoin(HANDOUTS_URL, raw_href)))

    def _fetch_one(item):
        code, h_url = item
        h_resp = session.get(
            h_url,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": HANDOUTS_URL},
            timeout=20,
        )
        return code, parse_evaluation_plan(extract_ajax_html(h_resp))

    if targets:
        with ThreadPoolExecutor(max_workers=min(10, len(targets))) as pool:
            for code, plan in pool.map(_fetch_one, targets):
                if plan:
                    handouts[code] = plan

    return handouts


def fetch_internals_data(session, academic_year, semester, handouts_map, preloaded_soup=None):
    soup = preloaded_soup if preloaded_soup is not None else submit_year_sem_form(session, INTERNALS_URL, academic_year, semester)
    courses = []
    fetch_jobs = []  # (course_idx, comp_idx, url)

    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        code = next((t for t in texts if re.match(r"^[0-9]{2}[A-Z]{2,5}[0-9]{2,4}[A-Z0-9]*$", t)), None)
        if not code or code not in handouts_map:
            continue

        code_idx = texts.index(code)
        course_name = texts[code_idx + 1] if code_idx + 1 < len(texts) else code

        comp_links = {}
        for a in tr.find_all("a", href=True):
            comp_label = a.get_text(strip=True)
            comp_url = urljoin(INTERNALS_URL, a["href"])
            comp_links[comp_label.lower()] = comp_url

        course_idx = len(courses)
        course_components = []
        for comp_idx, plan_item in enumerate(handouts_map[code]):
            cname = plan_item["name"]
            matched_url = comp_links.get(cname.lower())
            if not matched_url:
                for k, u in comp_links.items():
                    if cname.lower() in k or k in cname.lower():
                        matched_url = u
                        break

            course_components.append(
                {
                    "name": cname,
                    "type": plan_item["type"],
                    "maxMarks": plan_item["maxMarks"],
                    "weight": plan_item["weight"],
                    "raw": None,
                    "status": "not_uploaded",
                }
            )
            if matched_url:
                fetch_jobs.append((course_idx, comp_idx, matched_url))

        courses.append({"code": code, "name": course_name, "components": course_components})

    def _fetch_comp(job):
        ci, ri, url = job
        c_resp = session.get(
            url,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": INTERNALS_URL},
            timeout=20,
        )
        return ci, ri, parse_component_marks(extract_ajax_html(c_resp))

    if fetch_jobs:
        with ThreadPoolExecutor(max_workers=min(12, len(fetch_jobs))) as pool:
            for ci, ri, raw_val in pool.map(_fetch_comp, fetch_jobs):
                if raw_val is not None:
                    courses[ci]["components"][ri]["raw"] = raw_val
                    courses[ci]["components"][ri]["status"] = "uploaded"

    return courses


@app.route("/")
def index():
    stats = update_stats(inc_visits=1)
    return render_template("index.html", visits=stats["visits"], calculations=stats["calculations"])


@app.route("/api/stats", methods=["GET"])
def get_stats():
    return jsonify(update_stats())


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/announce")
def announce():
    return render_template("announce.html")


@app.route("/api/captcha", methods=["GET"])
def get_captcha():
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
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

        sid = SERIALIZER.dumps({
            "cookies": sess.cookies.get_dict(),
            "csrf": csrf_token,
            "login_post_url": login_post_url,
        })
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

    if not sid:
        return jsonify({"error": "Missing session token. Please refresh the CAPTCHA and try again."}), 400
    try:
        entry = SERIALIZER.loads(sid, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return jsonify({"error": "CAPTCHA expired. Please refresh the CAPTCHA and try again."}), 400

    if not username or not password or not captcha:
        return jsonify({"error": "Student ID, password, and CAPTCHA are required."}), 400

    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    for ck, cv in entry.get("cookies", {}).items():
        sess.cookies.set(ck, cv, domain="newerp.kluniversity.in", path="/")

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
            errs = [el.get_text(" ", strip=True) for el in login_soup.select(".help-block, .help-block-error, .alert-danger, .error-summary") if el.get_text(strip=True)]
            if errs:
                err_msg = " | ".join(dict.fromkeys(errs))
            else:
                hist = [f"{h.status_code}->{h.headers.get('Location','')}" for h in login_resp.history]
                err_msg = f"Login returned {login_resp.status_code} ({login_resp.url}) | post={entry['login_post_url']} | hist={hist} | cookies={list(sess.cookies.get_dict().keys())}"
            return jsonify({"error": err_msg}), 401

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_h = pool.submit(submit_year_sem_form, sess, HANDOUTS_URL, academic_year, semester)
            f_i = pool.submit(submit_year_sem_form, sess, INTERNALS_URL, academic_year, semester)
            h_soup = f_h.result()
            i_soup = f_i.result()

        handouts_map = fetch_handouts_map(sess, academic_year, semester, preloaded_soup=h_soup)
        courses = fetch_internals_data(sess, academic_year, semester, handouts_map, preloaded_soup=i_soup)

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


@app.route("/api/fetch-stream", methods=["POST"])
def fetch_internals_stream():
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id", "")
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    captcha = (data.get("captcha") or "").strip()
    academic_year = (data.get("academic_year") or "2026-2027").strip()
    semester = (data.get("semester") or "Odd Sem").strip()

    def _event(kind, payload):
        return json.dumps({"type": kind, **payload}) + "\n"

    def generate():
        t0 = time.time()
        def ts():
            return f"{time.time() - t0:.1f}s"

        if not sid:
            yield _event("error", {"error": "Missing session token. Please refresh the CAPTCHA and try again."})
            return
        try:
            entry = SERIALIZER.loads(sid, max_age=SESSION_TTL_SECONDS)
        except (BadSignature, SignatureExpired):
            yield _event("error", {"error": "CAPTCHA expired. Please refresh the CAPTCHA and try again."})
            return
        if not username or not password or not captcha:
            yield _event("error", {"error": "Student ID, password, and CAPTCHA are required."})
            return

        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        for ck, cv in entry.get("cookies", {}).items():
            sess.cookies.set(ck, cv, domain="newerp.kluniversity.in", path="/")

        try:
            yield _event("log", {"msg": f"[{ts()}] Authenticating {username} with newerp.kluniversity.in..."})
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
                errs = [el.get_text(" ", strip=True) for el in login_soup.select(".help-block, .help-block-error, .alert-danger, .error-summary") if el.get_text(strip=True)]
                err_msg = " | ".join(dict.fromkeys(errs)) if errs else f"Login returned {login_resp.status_code} ({login_resp.url})"
                yield _event("error", {"error": err_msg})
                return

            yield _event("log", {"msg": f"[{ts()}] Login verified! Fetching Handouts & Internals lists..."})
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_h = pool.submit(submit_year_sem_form, sess, HANDOUTS_URL, academic_year, semester)
                f_i = pool.submit(submit_year_sem_form, sess, INTERNALS_URL, academic_year, semester)
                h_soup = f_h.result()
                i_soup = f_i.result()

            yield _event("log", {"msg": f"[{ts()}] Extracting Evaluation Plans from Course Handouts in parallel..."})
            handouts_map = fetch_handouts_map(sess, academic_year, semester, preloaded_soup=h_soup)
            for code, plan in handouts_map.items():
                yield _event("log", {"msg": f"[{ts()}] Loaded Handout {code} ({len(plan)} internal components)"})

            yield _event("log", {"msg": f"[{ts()}] Fetching & summing internal mark tables (ignoring -1/-2)..."})
            courses = fetch_internals_data(sess, academic_year, semester, handouts_map, preloaded_soup=i_soup)

            yield _event("log", {"msg": f"[{ts()}] Done! {len(courses)} credited courses calculated. Session wiped from RAM."})
            stats = update_stats(inc_calcs=1)
            yield _event("done", {
                "student_id": username,
                "academic_year": academic_year,
                "semester": semester,
                "courses": courses,
                "stats": stats,
            })
        except requests.RequestException as exc:
            yield _event("error", {"error": f"Error communicating with KL ERP: {exc}"})

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
