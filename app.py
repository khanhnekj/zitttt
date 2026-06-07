"""
app.py — FBBot Dashboard · Per-user bot accounts + role-based command limits
"""
from __future__ import annotations
import json, os, sys, threading, time, traceback, importlib, hashlib, secrets
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, session

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _core._session import dataGetHome
from _messaging._send import api as SendAPI
from _messaging._listening import listeningEvent

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
USERS_PATH    = HERE / "users.json"
ACCOUNTS_PATH = HERE / "accounts.json"   # {acc_id: {owner_username, name, cookies, prefix, ...}}
CONFIG_PATH   = HERE / "config.json"
MODULES_DIR   = HERE / "modules"
DISABLED_PATH = HERE / "disabled_cmds.json"  # owner-level global disable

LOG_BUFFER: list[dict] = []
ANNOUNCEMENTS: list[dict] = []

# Global bot registry
bots: dict[str, "SimpleBot"] = {}
bot_status: dict[str, dict] = {}

ROLES = ["owner", "admin", "ndh", "member"]
ROLE_LEVEL = {"owner": 4, "admin": 3, "ndh": 2, "member": 1}

# Lệnh mỗi role được phép dùng (member ⊂ ndh ⊂ admin ⊂ owner)
ROLE_ALLOWED_CMDS = {
    "member": ["ping", "help", "id", "ff", "lq", "love"],
    "ndh":    ["ping", "help", "id", "ff", "lq", "love", "echo", "search", "pin", "kb"],
    "admin":  ["ping", "help", "id", "ff", "lq", "love", "echo", "search", "pin", "kb", "unsend"],
    "owner":  ["ping", "help", "id", "ff", "lq", "love", "echo", "search", "pin", "kb", "unsend"],
}
ALL_COMMANDS = ["ping", "help", "id", "echo", "search", "unsend", "pin", "love", "ff", "lq", "kb"]
CMD_DESCS = {
    "ping":   "Kiểm tra độ trễ",
    "help":   "Danh sách lệnh",
    "id":     "Xem threadID/userID",
    "echo":   "Lặp lại nội dung",
    "search": "Tìm user Facebook",
    "unsend": "Thu hồi tin nhắn",
    "pin":    "Tìm ảnh Pinterest",
    "love":   "Reaction ❤️",
    "ff":     "Thông tin Free Fire",
    "lq":     "QR chuyển khoản",
    "kb":     "Gửi lời mời kết bạn",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(tag, msg, level="info"):
    line = {"ts": datetime.now().strftime("%H:%M:%S"), "tag": tag, "msg": msg, "level": level}
    print(f"[{line['ts']}] [{tag}] {msg}", flush=True)
    LOG_BUFFER.append(line)
    if len(LOG_BUFFER) > 300: LOG_BUFFER.pop(0)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def load_users() -> dict:
    if not USERS_PATH.exists():
        default = {"quockhanh": {"password": hash_pw("zitcte"), "uid": "260729", "role": "owner", "created_at": "2026-06-05"}}
        USERS_PATH.write_text(json.dumps(default, indent=2, ensure_ascii=False))
    data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    changed = False
    for u, d in data.items():
        pw = d.get("password", "")
        if pw and len(pw) != 64:
            d["password"] = hash_pw(pw); changed = True
    if changed: USERS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data

def save_users(data): USERS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_accounts() -> dict:
    if not ACCOUNTS_PATH.exists():
        ACCOUNTS_PATH.write_text("{}")
    return json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))

def save_accounts(data): ACCOUNTS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({"prefix": "/", "admins": []}, indent=2))
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def save_config(cfg): CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

def load_global_disabled() -> set:
    if DISABLED_PATH.exists():
        return set(json.loads(DISABLED_PATH.read_text()))
    return set()

def save_global_disabled(s): DISABLED_PATH.write_text(json.dumps(list(s)))

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def current_user():
    username = session.get("username")
    if not username: return None
    users = load_users()
    if username not in users: return None
    u = dict(users[username]); u["username"] = username; u.pop("password", None)
    return u

def require_role(min_role):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u: return jsonify({"ok": False, "msg": "Chưa đăng nhập.", "auth": False}), 401
            if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL.get(min_role, 0):
                return jsonify({"ok": False, "msg": "Không đủ quyền."}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------
class SimpleBot:
    def __init__(self, acc_id, dataFB, name, owner_username, prefix="/"):
        self.acc_id         = acc_id
        self.dataFB         = dataFB
        self.name           = name
        self.owner_username = owner_username
        self.prefix         = prefix
        self.sender         = SendAPI()
        self.listener       = listeningEvent(dataFB)
        self._last_mid      = None
        self._last_activity = time.time()
        self._load_handlers()

    def _load_handlers(self):
        self._handlers = {}
        for cmd in ALL_COMMANDS:
            try:
                mod = importlib.import_module(f"modules.{cmd}")
                self._handlers[cmd] = mod.handle
            except Exception as e:
                log("bot", f"[{self.name}] load {cmd}: {e}", "warn")

    def run(self):
        log("bot", f"[{self.name}] UID={self.dataFB.get('FacebookID')}")
        bot_status[self.acc_id].update({
            "running": True,
            "uid": self.dataFB.get("FacebookID"),
            "start_time": time.time(),
        })
        self.listener.get_last_seq_id()
        threading.Thread(target=self.listener.connect_mqtt, daemon=True).start()
        log("bot", f"[{self.name}] Listener ✓")
        self._last_activity = time.time()
        while bot_status.get(self.acc_id, {}).get("running"):
            self._poll()
            if time.time() - self._last_activity > 600:
                self._refresh_session()
            time.sleep(0.3)

    def _refresh_session(self):
        log("bot", f"[{self.name}] Refresh session...", "warn")
        try:
            accs   = load_accounts()
            acc    = accs.get(self.acc_id, {})
            new_df = dataGetHome(acc.get("cookies", ""))
            if new_df.get("FacebookID"):
                self.dataFB = new_df
                self.sender = SendAPI()
                self.listener = listeningEvent(self.dataFB)
                self.listener.get_last_seq_id()
                threading.Thread(target=self.listener.connect_mqtt, daemon=True).start()
                log("bot", f"[{self.name}] Refresh OK ✓")
            else:
                log("bot", f"[{self.name}] Cookie hết hạn!", "error")
        except Exception as e:
            log("bot", f"[{self.name}] Refresh lỗi: {e}", "error")
        self._last_activity = time.time()

    def stop(self):
        if self.acc_id in bot_status:
            bot_status[self.acc_id]["running"] = False
        log("bot", f"[{self.name}] Dừng.")

    def _poll(self):
        snap = self.listener.bodyResults
        mid  = snap.get("messageID")
        body = snap.get("body", "")
        if not mid or mid == self._last_mid: return
        self._last_mid  = mid
        sender_id   = str(snap.get("userID") or "")
        thread_id   = str(snap.get("replyToID") or "")
        bot_id      = str(self.dataFB.get("FacebookID"))
        ts          = snap.get("timestamp", int(time.time() * 1000))
        sender_name = snap.get("senderName") or snap.get("authorName") or f"UID:{sender_id}"

        msg_obj = {
            "id": mid, "sender": sender_id, "sender_name": sender_name,
            "body": body or "", "ts": ts, "from_bot": sender_id == bot_id,
        }
        st   = bot_status.setdefault(self.acc_id, {})
        chat = st.setdefault("chat_messages", {})
        meta = st.setdefault("threads_meta", {})
        if thread_id:
            chat.setdefault(thread_id, []).append(msg_obj)
            if len(chat[thread_id]) > 200: chat[thread_id].pop(0)
            prev = meta.get(thread_id, {})
            meta[thread_id] = {
                "id": thread_id, "last_msg": body or "", "last_ts": ts,
                "name": prev.get("name", f"Thread {thread_id[-6:]}"),
                "last_sender": sender_name,
            }

        self._last_activity = time.time()
        if not body or sender_id == bot_id: return
        if not body.startswith(self.prefix): return
        parts = body[len(self.prefix):].strip().split(maxsplit=1)
        if not parts: return
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        # Kiểm tra global disabled (owner level)
        global_disabled = load_global_disabled()
        if cmd in global_disabled: return

        handler = self._handlers.get(cmd)
        if not handler: return
        log("cmd", f"[{self.name}] /{cmd} từ {sender_id}")
        self._last_activity = time.time()
        try:
            handler(self, snap, arg)
        except Exception as e:
            log("err", f"[{self.name}] /{cmd}: {e}", "error")

    def send_message(self, thread_id, content):
        try:
            result = self.sender.send(self.dataFB, content, thread_id)
            ok = isinstance(result, dict) and result.get("success") == 1
            if ok:
                mid  = result.get("payload", {}).get("messageID", "")
                st   = bot_status.setdefault(self.acc_id, {})
                chat = st.setdefault("chat_messages", {})
                meta = st.setdefault("threads_meta", {})
                chat.setdefault(thread_id, []).append({
                    "id": mid, "sender": str(self.dataFB.get("FacebookID")),
                    "sender_name": "Bot", "body": content,
                    "ts": int(time.time() * 1000), "from_bot": True,
                })
                meta.setdefault(thread_id, {"id": thread_id, "name": f"Thread {thread_id[-6:]}", "last_ts": 0})
                meta[thread_id].update({"last_msg": content, "last_ts": int(time.time() * 1000), "last_sender": "Bot"})
                log("send", f"[{self.name}] → {thread_id}: {content!r}")
            return ok
        except Exception as e:
            log("err", f"[{self.name}] send: {e}", "error"); return False

    def _reply(self, snap, content):
        thread_id = snap["replyToID"]
        type_chat = "user" if snap.get("type") == "user" else None
        result = self.sender.send(self.dataFB, content, thread_id,
                                  typeChat=type_chat, replyMessage=True, messageID=snap.get("messageID"))
        if isinstance(result, dict) and result.get("success") == 1:
            try: self._last_bot_msg = result["payload"]["messageID"]
            except: pass
            log("send", f"[{self.name}] → {thread_id}: {content!r}")
        else:
            log("send", f"[{self.name}] FAIL: {result}", "warn")

# ---------------------------------------------------------------------------
# Bot management
# ---------------------------------------------------------------------------
def start_bot(acc_id):
    accs = load_accounts()
    if acc_id not in accs: return False, "Acc không tồn tại."
    acc = accs[acc_id]
    if bot_status.get(acc_id, {}).get("running"): return False, "Bot đã chạy."
    cookies = acc.get("cookies", "")
    if not cookies or len(cookies) < 20: return False, "Chưa có cookie."
    try:
        log("boot", f"[{acc['name']}] Lấy session...")
        dataFB = dataGetHome(cookies)
        if not dataFB.get("FacebookID"): return False, "Cookie không hợp lệ hoặc hết hạn."
        cfg = load_config()
        bot = SimpleBot(
            acc_id, dataFB, acc["name"],
            owner_username=acc.get("owner", ""),
            prefix=acc.get("prefix", cfg.get("prefix", "/")),
        )
        bots[acc_id] = bot
        bot_status[acc_id] = {
            "running": False, "uid": None, "name": acc["name"],
            "start_time": None, "owner": acc.get("owner", ""),
            "chat_messages": {}, "threads_meta": {},
        }
        threading.Thread(target=bot.run, daemon=True).start()
        return True, f"Bot [{acc['name']}] đã khởi động!"
    except Exception as e:
        log("err", str(e), "error"); return False, f"Lỗi: {e}"

def stop_bot(acc_id):
    if not bot_status.get(acc_id, {}).get("running"):
        return False, "Bot không chạy."
    if acc_id in bots: bots[acc_id].stop()
    bots.pop(acc_id, None)
    return True, f"Bot [{bot_status[acc_id].get('name', acc_id)}] đã dừng."

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="web/static")

# SECRET_KEY phải cố định để session không bị mất sau khi restart.
# Đặt biến môi trường SECRET_KEY trong Render dashboard.
# Nếu chưa đặt, tạm dùng key ngẫu nhiên (session sẽ mất sau restart).
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    log("web", "CẢNH BÁO: Chưa đặt SECRET_KEY! Session sẽ mất sau restart. Hãy đặt biến môi trường SECRET_KEY trong Render.", "warn")
    _secret = secrets.token_hex(32)
app.secret_key = _secret

# Cấu hình session cookie để hoạt động đúng trên Render (HTTPS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Nếu deploy trên HTTPS (Render mặc định), bật Secure để tránh mất cookie
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RENDER", "") != ""

@app.route("/")
def index(): return send_from_directory("web", "index.html")

# ---- AUTH ----
@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    users = load_users()
    if username not in users or users[username]["password"] != hash_pw(password):
        return jsonify({"ok": False, "msg": "Sai tên đăng nhập hoặc mật khẩu."})
    session["username"] = username; session.permanent = True
    u = users[username]
    return jsonify({"ok": True, "role": u["role"], "uid": u.get("uid", ""), "username": username})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout(): session.clear(); return jsonify({"ok": True})

@app.route("/api/auth/me")
def api_me():
    u = current_user()
    if not u: return jsonify({"ok": False, "auth": False}), 401
    return jsonify({"ok": True, "auth": True, **u})

# ---- ACCOUNTS ----
@app.route("/api/accounts")
@require_role("member")
def api_accounts_list():
    u    = current_user()
    accs = load_accounts()
    result = []
    for acc_id, acc in accs.items():
        # member/ndh chỉ thấy bot của chính mình; admin/owner thấy tất cả
        if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
            if acc.get("owner") != u["username"]: continue
        st = bot_status.get(acc_id, {})
        result.append({
            "id":      acc_id,
            "name":    acc.get("name", acc_id),
            "owner":   acc.get("owner", ""),
            "prefix":  acc.get("prefix", "/"),
            "running": st.get("running", False),
            "uid":     st.get("uid", ""),
            "uptime":  int(time.time() - st["start_time"]) if st.get("start_time") and st.get("running") else 0,
        })
    return jsonify({"accounts": result})

@app.route("/api/accounts", methods=["POST"])
@require_role("member")  # mọi role đều thêm được bot của mình
def api_accounts_create():
    u    = current_user()
    data = request.get_json(force=True) or {}
    name    = data.get("name", "").strip()
    cookies = data.get("cookies", "").strip()
    prefix  = data.get("prefix", "/") or "/"
    if not name:    return jsonify({"ok": False, "msg": "Thiếu tên acc."})
    if not cookies or len(cookies) < 20:
        return jsonify({"ok": False, "msg": "Thiếu cookie hoặc cookie quá ngắn."})
    accs   = load_accounts()
    acc_id = f"acc_{u['username']}_{int(time.time()*1000)}"
    accs[acc_id] = {
        "name":       name,
        "cookies":    cookies,
        "prefix":     prefix,
        "owner":      u["username"],
        "created_at": datetime.now().strftime("%Y-%m-%d"),
    }
    save_accounts(accs)
    log("web", f"[{u['username']}] Tạo acc: {name}")
    return jsonify({"ok": True, "msg": f"Đã tạo acc [{name}]!", "id": acc_id})

@app.route("/api/accounts/<acc_id>", methods=["PATCH"])
@require_role("member")
def api_accounts_update(acc_id):
    u    = current_user()
    accs = load_accounts()
    if acc_id not in accs: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    acc = accs[acc_id]
    # Chỉ owner của acc hoặc admin+ mới sửa được
    if acc.get("owner") != u["username"] and ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
        return jsonify({"ok": False, "msg": "Không có quyền sửa acc này."})
    data = request.get_json(force=True) or {}
    if data.get("name"):    acc["name"]    = data["name"]
    if data.get("cookies"): acc["cookies"] = data["cookies"]
    if data.get("prefix"):  acc["prefix"]  = data["prefix"]
    save_accounts(accs)
    return jsonify({"ok": True, "msg": "Đã cập nhật!"})

@app.route("/api/accounts/<acc_id>", methods=["DELETE"])
@require_role("member")
def api_accounts_delete(acc_id):
    u    = current_user()
    accs = load_accounts()
    if acc_id not in accs: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    acc = accs[acc_id]
    if acc.get("owner") != u["username"] and ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
        return jsonify({"ok": False, "msg": "Không có quyền xóa acc này."})
    if bot_status.get(acc_id, {}).get("running"): stop_bot(acc_id)
    del accs[acc_id]; save_accounts(accs)
    bot_status.pop(acc_id, None)
    return jsonify({"ok": True, "msg": "Đã xóa acc."})

@app.route("/api/accounts/<acc_id>/start", methods=["POST"])
@require_role("member")
def api_acc_start(acc_id):
    u    = current_user()
    accs = load_accounts()
    if acc_id not in accs: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    if accs[acc_id].get("owner") != u["username"] and ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
        return jsonify({"ok": False, "msg": "Không có quyền bật acc này."})
    ok, msg = start_bot(acc_id)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/accounts/<acc_id>/stop", methods=["POST"])
@require_role("member")
def api_acc_stop(acc_id):
    u    = current_user()
    accs = load_accounts()
    if acc_id not in accs: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    if accs[acc_id].get("owner") != u["username"] and ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
        return jsonify({"ok": False, "msg": "Không có quyền tắt acc này."})
    ok, msg = stop_bot(acc_id)
    return jsonify({"ok": ok, "msg": msg})

# ---- STATUS ----
@app.route("/api/status")
@require_role("member")
def api_status():
    u    = current_user()
    accs = load_accounts()
    bots_info = []
    for acc_id, acc in accs.items():
        if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["admin"]:
            if acc.get("owner") != u["username"]: continue
        st = bot_status.get(acc_id, {})
        bots_info.append({
            "id": acc_id, "name": acc.get("name", acc_id),
            "owner": acc.get("owner", ""),
            "running": st.get("running", False),
            "uid": st.get("uid"),
            "uptime": int(time.time() - st["start_time"]) if st.get("start_time") and st.get("running") else 0,
        })
    running = sum(1 for b in bots_info if b["running"])
    return jsonify({"total": len(bots_info), "running": running, "bots": bots_info})

@app.route("/api/logs")
@require_role("ndh")
def api_logs():
    n = int(request.args.get("n", 100))
    return jsonify({"logs": LOG_BUFFER[-n:]})

# ---- COMMANDS — per role ----
@app.route("/api/commands")
@require_role("member")
def api_commands():
    u   = current_user()
    role = u["role"]
    global_disabled = load_global_disabled()
    allowed = set(ROLE_ALLOWED_CMDS.get(role, []))
    cmds = []
    for cmd in ALL_COMMANDS:
        globally_off = cmd in global_disabled
        role_allowed = cmd in allowed
        cmds.append({
            "name":         cmd,
            "desc":         CMD_DESCS.get(cmd, ""),
            "globally_off": globally_off,          # owner tắt toàn cục
            "role_allowed": role_allowed,           # role này có được dùng không
            "can_global_toggle": ROLE_LEVEL.get(role, 0) >= ROLE_LEVEL["owner"],  # chỉ owner toggle global
        })
    return jsonify({"commands": cmds, "role": role})

@app.route("/api/commands/<name>/global-toggle", methods=["POST"])
@require_role("owner")
def api_global_toggle(name):
    if name not in ALL_COMMANDS: return jsonify({"ok": False, "msg": "Không tồn tại."})
    s = load_global_disabled()
    if name in s: s.discard(name); enabled = True
    else: s.add(name); enabled = False
    save_global_disabled(s)
    return jsonify({"ok": True, "enabled": enabled, "msg": f"{'Bật' if enabled else 'Tắt'} toàn cục /{name}"})

# ---- MODULES (admin+) ----
@app.route("/api/modules")
@require_role("admin")
def api_modules():
    return jsonify({"files": [
        {"name": f.stem, "filename": f.name, "size": f.stat().st_size}
        for f in sorted(MODULES_DIR.glob("*.py")) if not f.name.startswith("_")
    ]})

@app.route("/api/modules/<name>", methods=["GET"])
@require_role("admin")
def api_module_get(name):
    path = MODULES_DIR / f"{name}.py"
    if not path.exists(): return jsonify({"ok": False, "msg": "Không tồn tại."})
    return jsonify({"ok": True, "content": path.read_text(encoding="utf-8")})

@app.route("/api/modules/<name>", methods=["POST"])
@require_role("admin")
def api_module_save(name):
    if not name.isidentifier(): return jsonify({"ok": False, "msg": "Tên không hợp lệ."})
    content = (request.get_json(force=True) or {}).get("content", "")
    (MODULES_DIR / f"{name}.py").write_text(content, encoding="utf-8")
    for bot in bots.values():
        try:
            mod = importlib.import_module(f"modules.{name}")
            importlib.reload(mod); bot._handlers[name] = mod.handle
            if name not in ALL_COMMANDS: ALL_COMMANDS.append(name)
        except Exception as e: log("web", f"Reload lỗi: {e}", "warn")
    log("web", f"{name}.py đã lưu.")
    return jsonify({"ok": True, "msg": f"Đã lưu {name}.py"})

@app.route("/api/modules/<name>", methods=["DELETE"])
@require_role("admin")
def api_module_delete(name):
    path = MODULES_DIR / f"{name}.py"
    if not path.exists(): return jsonify({"ok": False, "msg": "Không tồn tại."})
    path.unlink()
    for bot in bots.values(): bot._handlers.pop(name, None)
    return jsonify({"ok": True, "msg": f"Đã xóa {name}.py"})

# ---- CHAT (per acc) ----
@app.route("/api/accounts/<acc_id>/threads")
@require_role("member")
def api_threads(acc_id):
    st   = bot_status.get(acc_id, {})
    meta = st.get("threads_meta", {})
    threads = sorted(meta.values(), key=lambda x: x.get("last_ts", 0), reverse=True)
    return jsonify({"threads": threads})

@app.route("/api/accounts/<acc_id>/threads/<thread_id>/messages")
@require_role("member")
def api_messages(acc_id, thread_id):
    st   = bot_status.get(acc_id, {})
    chat = st.get("chat_messages", {})
    return jsonify({"messages": chat.get(thread_id, [])[-100:]})

@app.route("/api/accounts/<acc_id>/threads/<thread_id>/send", methods=["POST"])
@require_role("member")
def api_send(acc_id, thread_id):
    u   = current_user()
    bot = bots.get(acc_id)
    if not bot or not bot_status.get(acc_id, {}).get("running"):
        return jsonify({"ok": False, "msg": "Bot chưa chạy."})
    # Kiểm tra quyền gửi: owner của acc hoặc ndh+
    accs = load_accounts()
    acc  = accs.get(acc_id, {})
    if acc.get("owner") != u["username"] and ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["ndh"]:
        return jsonify({"ok": False, "msg": "Không có quyền gửi tin nhắn."})
    content = (request.get_json(force=True) or {}).get("content", "").strip()
    if not content: return jsonify({"ok": False, "msg": "Nội dung trống."})
    ok = bot.send_message(thread_id, content)
    return jsonify({"ok": ok, "msg": "Đã gửi!" if ok else "Gửi thất bại."})

@app.route("/api/accounts/<acc_id>/threads/<thread_id>/name", methods=["POST"])
@require_role("member")
def api_rename_thread(acc_id, thread_id):
    name = (request.get_json(force=True) or {}).get("name", "").strip()
    meta = bot_status.get(acc_id, {}).get("threads_meta", {})
    if thread_id in meta:
        meta[thread_id]["name"] = name or f"Thread {thread_id[-6:]}"
    return jsonify({"ok": True})

# ---- ANNOUNCEMENTS ----
@app.route("/api/announcements")
@require_role("member")
def api_ann_get(): return jsonify({"announcements": ANNOUNCEMENTS[-50:]})

@app.route("/api/announcements", methods=["POST"])
@require_role("admin")
def api_ann_post():
    u = current_user()
    content = (request.get_json(force=True) or {}).get("content", "").strip()
    if not content: return jsonify({"ok": False, "msg": "Nội dung trống."})
    ANNOUNCEMENTS.append({
        "id": int(time.time() * 1000), "content": content,
        "author": u["username"], "role": u["role"],
        "ts": datetime.now().strftime("%H:%M %d/%m/%Y"),
    })
    return jsonify({"ok": True, "msg": "Đã đăng!"})

@app.route("/api/announcements/<int:ann_id>", methods=["DELETE"])
@require_role("admin")
def api_ann_del(ann_id):
    global ANNOUNCEMENTS
    ANNOUNCEMENTS = [a for a in ANNOUNCEMENTS if a["id"] != ann_id]
    return jsonify({"ok": True})

# ---- USERS (owner) ----
@app.route("/api/users")
@require_role("owner")
def api_users_get():
    users = load_users()
    return jsonify({"users": [
        {"username": u, "uid": d.get("uid", ""), "role": d.get("role", "member"), "created_at": d.get("created_at", "")}
        for u, d in users.items()
    ]})

@app.route("/api/users", methods=["POST"])
@require_role("owner")
def api_users_create():
    data = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    uid      = data.get("uid", "").strip()
    role     = data.get("role", "member")
    if not username or not password: return jsonify({"ok": False, "msg": "Thiếu thông tin."})
    if not username.isidentifier():  return jsonify({"ok": False, "msg": "Tên không hợp lệ."})
    if role not in ROLES:            return jsonify({"ok": False, "msg": "Role không hợp lệ."})
    users = load_users()
    if username in users: return jsonify({"ok": False, "msg": "Tên đã tồn tại."})
    users[username] = {"password": hash_pw(password), "uid": uid, "role": role, "created_at": datetime.now().strftime("%Y-%m-%d")}
    save_users(users)
    return jsonify({"ok": True, "msg": f"Đã tạo {username}!"})

@app.route("/api/users/<username>", methods=["PATCH"])
@require_role("owner")
def api_users_update(username):
    data  = request.get_json(force=True) or {}
    users = load_users()
    if username not in users: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    if "role" in data: users[username]["role"] = data["role"]
    if data.get("password"): users[username]["password"] = hash_pw(data["password"])
    if "uid" in data: users[username]["uid"] = data["uid"]
    save_users(users)
    return jsonify({"ok": True, "msg": f"Đã cập nhật {username}."})

@app.route("/api/users/<username>", methods=["DELETE"])
@require_role("owner")
def api_users_delete(username):
    u = current_user()
    if username == u["username"]: return jsonify({"ok": False, "msg": "Không thể xóa chính mình."})
    users = load_users()
    if username not in users: return jsonify({"ok": False, "msg": "Không tìm thấy."})
    del users[username]; save_users(users)
    return jsonify({"ok": True, "msg": f"Đã xóa {username}."})

@app.route("/api/users/change-password", methods=["POST"])
@require_role("member")
def api_change_pw():
    u    = current_user()
    data = request.get_json(force=True) or {}
    old_pw = data.get("old_password", ""); new_pw = data.get("new_password", "")
    if not old_pw or not new_pw: return jsonify({"ok": False, "msg": "Thiếu thông tin."})
    users = load_users()
    if users[u["username"]]["password"] != hash_pw(old_pw):
        return jsonify({"ok": False, "msg": "Mật khẩu cũ sai."})
    users[u["username"]]["password"] = hash_pw(new_pw); save_users(users)
    return jsonify({"ok": True, "msg": "Đã đổi mật khẩu!"})

# ---- CONFIG (owner) ----
@app.route("/api/config", methods=["GET"])
@require_role("owner")
def api_config_get(): return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
@require_role("owner")
def api_config_save():
    data = request.get_json(force=True) or {}
    cfg  = load_config()
    if "prefix" in data: cfg["prefix"] = data["prefix"] or "/"
    if "admins" in data:
        raw = data["admins"]
        cfg["admins"] = [x.strip() for x in raw.split(",") if x.strip()] if isinstance(raw, str) else raw
    save_config(cfg)
    return jsonify({"ok": True, "msg": "Đã lưu!"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log("web", f"Dashboard: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
