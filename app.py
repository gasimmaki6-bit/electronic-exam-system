# -*- coding: utf-8 -*-
import csv
import io
import json
import os
import random
import statistics
import string
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, abort, session, Response
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename

from config import Config, BASE_DIR
from models import db, User, Exam, Question, Submission, Answer, Subject, AuditLog, Notification, BankQuestion
from translations import get_text, TRANSLATIONS, DEFAULT_LANG

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "الرجاء تسجيل الدخول للمتابعة."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# نظام تعدد اللغات (عربي / إنجليزي)
# ---------------------------------------------------------------------------
@app.before_request
def ensure_language():
    if "lang" not in session:
        session["lang"] = DEFAULT_LANG


def t(key, **kwargs):
    return get_text(session.get("lang", DEFAULT_LANG), key, **kwargs)


@app.context_processor
def inject_i18n():
    lang = session.get("lang", DEFAULT_LANG)
    return {"t": t, "lang": lang, "is_rtl": lang == "ar"}


@app.route("/set-language/<lang_code>")
def set_language(lang_code):
    if lang_code in TRANSLATIONS:
        session["lang"] = lang_code
    return redirect(request.referrer or url_for("index"))


# ---------------------------------------------------------------------------
# صلاحيات الأدوار
# ---------------------------------------------------------------------------
def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def allowed_image(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_IMAGE_EXTENSIONS"]
    )


def save_question_image(file_storage):
    """يحفظ صورة السؤال المرفوعة ويعيد المسار النسبي، أو None إن لم توجد صورة."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_image(file_storage.filename):
        flash(t("flash_image_bad_format"), "danger")
        return None
    safe_name = secure_filename(file_storage.filename)
    unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
    return f"uploads/{unique_name}"


def generate_random_username(base_prefix, existing_usernames):
    """يولّد اسم مستخدم فريد بصيغة prefix + رقم عشوائي."""
    while True:
        candidate = f"{base_prefix}{random.randint(1000, 999999)}"
        if candidate not in existing_usernames:
            return candidate


def generate_random_password(length=8):
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def parse_question_form(form, files):
    """
    يبني قاموس حقول سؤال واحد من بيانات نموذج (form) — يُستخدم لكل من أسئلة الامتحان
    المباشرة وأسئلة بنك الأسئلة، لضمان نفس منطق التحقق والتصحيح في كلا المكانين.
    يعيد (fields_dict, None) عند النجاح، أو (None, مفتاح_ترجمة_الخطأ) عند الفشل.
    """
    q_type = form.get("question_type", "mcq")
    text = form.get("text", "").strip()

    if not text:
        return None, "flash_question_text_required"

    try:
        points = int(form.get("points", 1) or 1)
    except ValueError:
        points = 1

    image_url = save_question_image(files.get("image"))

    fields = {
        "question_type": q_type, "text": text, "points": points, "image_url": image_url,
        "option_a": None, "option_b": None, "option_c": None, "option_d": None,
        "correct_option": None, "matching_data": None, "ordering_data": None, "short_answer_key": None,
    }

    if q_type == "mcq":
        fields["option_a"] = form.get("option_a", "").strip()
        fields["option_b"] = form.get("option_b", "").strip()
        fields["option_c"] = form.get("option_c", "").strip() or None
        fields["option_d"] = form.get("option_d", "").strip() or None
        fields["correct_option"] = form.get("correct_option")
        if not fields["option_a"] or not fields["option_b"] or not fields["correct_option"]:
            return None, "flash_mcq_incomplete"

    elif q_type == "true_false":
        correct = form.get("true_false_correct", "t")
        fields["correct_option"] = "t" if correct == "t" else "f"

    elif q_type == "matching":
        terms = form.getlist("term[]")
        definitions = form.getlist("definition[]")
        pairs = [
            {"term": tm.strip(), "definition": d.strip()}
            for tm, d in zip(terms, definitions) if tm.strip() and d.strip()
        ]
        if len(pairs) < 2:
            return None, "flash_matching_incomplete"
        fields["matching_data"] = json.dumps(pairs, ensure_ascii=False)

    elif q_type == "ordering":
        items = [i.strip() for i in form.getlist("order_item[]") if i.strip()]
        if len(items) < 2:
            return None, "flash_ordering_incomplete"
        fields["ordering_data"] = json.dumps(items, ensure_ascii=False)

    elif q_type == "short_answer":
        key = form.get("short_answer_key", "").strip()
        if not key:
            return None, "flash_short_answer_incomplete"
        fields["short_answer_key"] = key

    elif q_type == "essay":
        pass  # لا حقول إضافية

    else:
        return None, "flash_unknown_type"

    return fields, None


# ---------------------------------------------------------------------------
# سجل التدقيق (Audit Log) والإشعارات
# ---------------------------------------------------------------------------
def log_action(action, details=""):
    """يسجّل حدثاً في سجل التدقيق. لا يوقف تنفيذ الطلب أبداً حتى لو فشل التسجيل."""
    try:
        entry = AuditLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            user_name=current_user.full_name if current_user.is_authenticated else None,
            user_role=current_user.role if current_user.is_authenticated else None,
            action=action,
            details=details,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def notify_user(user_id, title, message, link=None):
    db.session.add(Notification(user_id=user_id, title=title, message=message, link=link))


def notify_all_students(title, message, link=None):
    student_ids = [u.id for u in User.query.filter_by(role="student", is_active_user=True).all()]
    for sid in student_ids:
        db.session.add(Notification(user_id=sid, title=title, message=message, link=link))


@app.context_processor
def inject_notifications():
    if current_user.is_authenticated:
        count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    else:
        count = 0
    return {"unread_notifications_count": count}


# ---------------------------------------------------------------------------
# الصفحة الرئيسية / تسجيل الدخول / تسجيل الخروج
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin_dashboard"))
        elif current_user.role == "teacher":
            return redirect(url_for("teacher_dashboard"))
        else:
            return redirect(url_for("student_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password) and user.is_active_user:
            login_user(user)
            user.last_login_at = datetime.now()
            db.session.commit()
            log_action("login")
            flash(t("welcome_msg", name=user.full_name), "success")
            return redirect(url_for("index"))
        else:
            flash(t("invalid_credentials"), "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    log_action("logout")
    logout_user()
    flash(t("logged_out"), "info")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """يسمح لأي مستخدم (مدير/أستاذ/طالب) بتغيير كلمة مروره الخاصة بنفسه."""
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash(t("flash_wrong_current_password"), "danger")
            return redirect(url_for("change_password"))

        if len(new_pw) < 4:
            flash(t("flash_password_too_short"), "danger")
            return redirect(url_for("change_password"))

        if new_pw != confirm_pw:
            flash(t("flash_passwords_dont_match"), "danger")
            return redirect(url_for("change_password"))

        current_user.set_password(new_pw)
        db.session.commit()
        log_action("change_password")
        flash(t("flash_password_changed"), "success")
        return redirect(url_for("index"))

    return render_template("change_password.html")


# ---------------------------------------------------------------------------
# لوحة تحكم المدير (Admin)
# ---------------------------------------------------------------------------
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    admins = User.query.filter_by(role="admin").order_by(User.created_at.desc()).all()
    teachers = User.query.filter_by(role="teacher").order_by(User.created_at.desc()).all()
    students = User.query.filter_by(role="student").order_by(User.created_at.desc()).all()
    exams = Exam.query.all()
    stats = {
        "teachers": len(teachers),
        "students": len(students),
        "exams": len(exams),
        "submissions": Submission.query.count(),
        "subjects": Subject.query.count(),
    }
    recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all()

    # --- بيانات الرسوم البيانية للوحة المدير ---
    finished_submissions = Submission.query.filter_by(is_finished=True).all()
    pass_count = sum(1 for s in finished_submissions if s.total_points and (s.score / s.total_points * 100) >= 50)
    fail_count = len(finished_submissions) - pass_count

    published_count = sum(1 for e in exams if e.is_active)
    draft_count = len(exams) - published_count

    daily_labels, daily_counts = [], []
    today = datetime.now().date()
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        day_start = datetime.combine(day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        count = Submission.query.filter(
            Submission.submitted_at >= day_start, Submission.submitted_at < day_end
        ).count()
        daily_labels.append(day.strftime("%m-%d"))
        daily_counts.append(count)

    chart_data = {
        "pass_count": pass_count, "fail_count": fail_count,
        "draft_count": draft_count, "published_count": published_count,
        "daily_labels": daily_labels, "daily_counts": daily_counts,
        "has_submissions_data": len(finished_submissions) > 0,
        "has_exams_data": len(exams) > 0,
        "has_activity_data": sum(daily_counts) > 0,
    }

    return render_template(
        "admin/dashboard.html", admins=admins, teachers=teachers, students=students,
        stats=stats, recent_logs=recent_logs, chart_data=chart_data,
    )


@app.route("/admin/users/create", methods=["GET", "POST"])
@role_required("admin")
def admin_create_user():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "student")

        if not full_name or not username or not password:
            flash(t("flash_fields_required"), "danger")
            return redirect(url_for("admin_create_user"))

        if User.query.filter_by(username=username).first():
            flash(t("flash_username_exists"), "danger")
            return redirect(url_for("admin_create_user"))

        user = User(full_name=full_name, username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_action("create_user", details=f"{username} ({role})")
        flash(t("flash_user_created"), "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin/create_user.html")


@app.route("/admin/users/generate", methods=["GET", "POST"])
@role_required("admin")
def admin_generate_users():
    """توليد عدة حسابات دفعة واحدة بأسماء مستخدمين وكلمات مرور عشوائية."""
    generated = None

    if request.method == "POST":
        role = request.form.get("role", "student")
        count = int(request.form.get("count", 1))
        name_prefix = request.form.get("name_prefix", "").strip() or ("طالب" if role == "student" else "أستاذ")
        count = max(1, min(count, 1000))  # حد أقصى 1000 حساب دفعة واحدة

        prefix_map = {"student": "std", "teacher": "tch", "admin": "adm"}
        username_prefix = prefix_map.get(role, "usr")

        existing_usernames = {u.username for u in User.query.with_entities(User.username).all()}

        generated = []
        for i in range(1, count + 1):
            username = generate_random_username(username_prefix, existing_usernames)
            existing_usernames.add(username)
            password = generate_random_password()
            full_name = f"{name_prefix} {i}"

            user = User(full_name=full_name, username=username, role=role)
            user.set_password(password)
            db.session.add(user)

            generated.append({"full_name": full_name, "username": username, "password": password})

        db.session.commit()
        log_action("bulk_generate_users", details=f"{count} x {role}")
        flash(t("flash_bulk_generated", count=count), "success")

    return render_template("admin/generate_users.html", generated=generated)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@role_required("admin")
def admin_toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != "admin":
        user.is_active_user = not user.is_active_user
        db.session.commit()
        log_action("toggle_user", details=f"{user.username} -> {'active' if user.is_active_user else 'suspended'}")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != "admin":
        username = user.username
        db.session.delete(user)
        db.session.commit()
        log_action("delete_user", details=username)
        flash(t("flash_user_deleted"), "info")
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------------
# إدارة المواد الدراسية (Subjects)
# ---------------------------------------------------------------------------
@app.route("/admin/subjects", methods=["GET", "POST"])
@role_required("admin")
def admin_subjects():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip() or None
        description = request.form.get("description", "").strip()
        teacher_id = request.form.get("teacher_id") or None

        if not name:
            flash(t("flash_subject_name_required"), "danger")
            return redirect(url_for("admin_subjects"))

        subject = Subject(
            name=name, code=code, description=description,
            teacher_id=int(teacher_id) if teacher_id else None,
        )
        db.session.add(subject)
        db.session.commit()
        log_action("create_subject", details=name)
        flash(t("flash_subject_created"), "success")
        return redirect(url_for("admin_subjects"))

    subjects = Subject.query.order_by(Subject.created_at.desc()).all()
    teachers = User.query.filter_by(role="teacher", is_active_user=True).all()
    return render_template("admin/subjects.html", subjects=subjects, teachers=teachers)


@app.route("/admin/subjects/<int:subject_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    name = subject.name
    # فك ارتباط أي امتحانات بهذه المادة قبل حذفها بدل تركها بمرجع معلّق
    Exam.query.filter_by(subject_id=subject.id).update({"subject_id": None})
    db.session.delete(subject)
    db.session.commit()
    log_action("delete_subject", details=name)
    flash(t("flash_subject_deleted"), "info")
    return redirect(url_for("admin_subjects"))


# ---------------------------------------------------------------------------
# سجل التدقيق (Audit Log)
# ---------------------------------------------------------------------------
@app.route("/admin/audit-log")
@role_required("admin")
def admin_audit_log():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    distinct_actions = sorted({log.action for log in logs})
    return render_template("admin/audit_log.html", logs=logs, distinct_actions=distinct_actions)


@app.route("/admin/audit-log/export")
@role_required("admin")
def admin_audit_log_export():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["datetime", "user_name", "role", "action", "details"])
    for log in logs:
        writer.writerow([log.created_at, log.user_name or "", log.user_role or "", log.action, log.details or ""])
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@app.route("/admin/backup")
@role_required("admin")
def admin_backup_database():
    """
    نسخة احتياطية حقيقية: تنزيل ملف قاعدة البيانات SQLite الفعلي كما هو، لا بيانات
    وهمية ولا تصدير جزئي — الملف نفسه الذي يعمل عليه النظام حالياً بكل جداوله وسجلاته.
    """
    db_path = os.path.join(BASE_DIR, "exam_system.db")
    if not os.path.exists(db_path):
        flash(t("flash_backup_not_found"), "danger")
        return redirect(url_for("admin_dashboard"))

    log_action("download_backup")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(db_path, "rb") as f:
        data = f.read()
    return Response(
        data, mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=exam_system_backup_{timestamp}.db"},
    )


# ---------------------------------------------------------------------------
# لوحة تحكم الأستاذ (Teacher)
# ---------------------------------------------------------------------------
@app.route("/teacher")
@role_required("teacher")
def teacher_dashboard():
    exams = Exam.query.filter_by(teacher_id=current_user.id).order_by(Exam.created_at.desc()).all()
    return render_template("teacher/dashboard.html", exams=exams)


@app.route("/teacher/exams/create", methods=["GET", "POST"])
@role_required("teacher")
def teacher_create_exam():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        duration = int(request.form.get("duration_minutes", 30))
        shuffle_q = bool(request.form.get("shuffle_questions"))
        shuffle_o = bool(request.form.get("shuffle_options"))
        subject_id = request.form.get("subject_id") or None

        start_time_raw = request.form.get("start_time")
        end_time_raw = request.form.get("end_time")
        start_time = datetime.fromisoformat(start_time_raw) if start_time_raw else None
        end_time = datetime.fromisoformat(end_time_raw) if end_time_raw else None

        exam = Exam(
            title=title,
            description=description,
            teacher_id=current_user.id,
            subject_id=int(subject_id) if subject_id else None,
            duration_minutes=duration,
            start_time=start_time,
            end_time=end_time,
            shuffle_questions=shuffle_q,
            shuffle_options=shuffle_o,
        )
        db.session.add(exam)
        db.session.commit()
        log_action("create_exam", details=exam.title)
        flash(t("flash_exam_created"), "success")
        return redirect(url_for("teacher_add_question", exam_id=exam.id))

    subjects = Subject.query.order_by(Subject.name).all()
    return render_template("teacher/create_exam.html", subjects=subjects)


@app.route("/teacher/exams/<int:exam_id>/questions", methods=["GET", "POST"])
@role_required("teacher")
def teacher_add_question(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    if request.method == "POST":
        fields, error_key = parse_question_form(request.form, request.files)
        if error_key:
            flash(t(error_key), "danger")
            return redirect(url_for("teacher_add_question", exam_id=exam.id))

        q = Question(exam_id=exam.id, **fields)
        db.session.add(q)
        db.session.commit()
        flash(t("flash_question_added"), "success")
        return redirect(url_for("teacher_add_question", exam_id=exam.id))

    return render_template("teacher/add_question.html", exam=exam)


@app.route("/teacher/exams/<int:exam_id>/preview")
@role_required("teacher")
def teacher_preview_exam(exam_id):
    """معاينة الامتحان بالضبط كما سيراه الطالب (بدون خلط عشوائي وبدون منع غش)،
    لمراجعة الشكل النهائي قبل الضغط على زر النشر."""
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    q_data = []
    for q in exam.questions:
        item = {"id": q.id, "text": q.text, "type": q.question_type, "image_url": q.image_url, "points": q.points}

        if q.question_type == "mcq":
            opts = [("a", q.option_a), ("b", q.option_b)]
            if q.option_c:
                opts.append(("c", q.option_c))
            if q.option_d:
                opts.append(("d", q.option_d))
            item["options"] = opts

        elif q.question_type == "matching":
            pairs = q.get_matching_pairs()
            item["terms"] = [{"index": i, "text": p["term"]} for i, p in enumerate(pairs)]
            item["definitions"] = [{"original_index": i, "text": p["definition"], "display_number": i + 1} for i, p in enumerate(pairs)]

        elif q.question_type == "ordering":
            ordering_items = q.get_ordering_items()
            item["ordering_items"] = [{"original_index": i, "text": txt} for i, txt in enumerate(ordering_items)]
            item["ordering_count"] = len(ordering_items)

        q_data.append(item)

    return render_template("teacher/preview_exam.html", exam=exam, questions=q_data)


@app.route("/teacher/questions/<int:question_id>/delete", methods=["POST"])
@role_required("teacher")
def teacher_delete_question(question_id):
    q = Question.query.get_or_404(question_id)
    if q.exam.teacher_id != current_user.id:
        abort(403)
    exam_id = q.exam_id
    db.session.delete(q)
    db.session.commit()
    return redirect(url_for("teacher_add_question", exam_id=exam_id))


# ---------------------------------------------------------------------------
# بنك الأسئلة المستقل لكل أستاذ
# ---------------------------------------------------------------------------
@app.route("/teacher/bank")
@role_required("teacher")
def teacher_bank():
    query = BankQuestion.query.filter_by(creator_id=current_user.id)

    subject_id = request.args.get("subject_id") or ""
    q_type = request.args.get("type") or ""
    difficulty = request.args.get("difficulty") or ""
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "active")

    if subject_id:
        query = query.filter_by(subject_id=int(subject_id))
    if q_type:
        query = query.filter_by(question_type=q_type)
    if difficulty:
        query = query.filter_by(difficulty=difficulty)
    if status in ("active", "archived"):
        query = query.filter_by(status=status)
    if search:
        query = query.filter(BankQuestion.text.ilike(f"%{search}%"))

    questions = query.order_by(BankQuestion.created_at.desc()).all()
    subjects = Subject.query.order_by(Subject.name).all()
    return render_template(
        "teacher/bank.html", questions=questions, subjects=subjects,
        filters={"subject_id": subject_id, "type": q_type, "difficulty": difficulty, "search": search, "status": status},
    )


@app.route("/teacher/bank/create", methods=["GET", "POST"])
@role_required("teacher")
def teacher_bank_create():
    if request.method == "POST":
        fields, error_key = parse_question_form(request.form, request.files)
        if error_key:
            flash(t(error_key), "danger")
            return redirect(url_for("teacher_bank_create"))

        subject_id = request.form.get("subject_id") or None
        difficulty = request.form.get("difficulty", "medium")
        keywords = request.form.get("keywords", "").strip() or None

        bq = BankQuestion(
            creator_id=current_user.id,
            subject_id=int(subject_id) if subject_id else None,
            difficulty=difficulty,
            keywords=keywords,
            **fields,
        )
        db.session.add(bq)
        db.session.commit()
        log_action("create_bank_question", details=bq.text[:60])
        flash(t("flash_bank_question_added"), "success")
        return redirect(url_for("teacher_bank"))

    subjects = Subject.query.order_by(Subject.name).all()
    return render_template("teacher/bank_create.html", subjects=subjects)


@app.route("/teacher/bank/<int:bq_id>/toggle-archive", methods=["POST"])
@role_required("teacher")
def teacher_bank_toggle_archive(bq_id):
    bq = BankQuestion.query.get_or_404(bq_id)
    if bq.creator_id != current_user.id:
        abort(403)
    bq.status = "archived" if bq.status == "active" else "active"
    db.session.commit()
    return redirect(url_for("teacher_bank"))


@app.route("/teacher/bank/<int:bq_id>/delete", methods=["POST"])
@role_required("teacher")
def teacher_bank_delete(bq_id):
    bq = BankQuestion.query.get_or_404(bq_id)
    if bq.creator_id != current_user.id:
        abort(403)
    db.session.delete(bq)
    db.session.commit()
    flash(t("flash_bank_question_deleted"), "info")
    return redirect(url_for("teacher_bank"))


@app.route("/teacher/exams/<int:exam_id>/add-from-bank", methods=["GET", "POST"])
@role_required("teacher")
def teacher_add_from_bank(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    if request.method == "POST":
        selected_ids = request.form.getlist("bank_ids[]")
        added = 0
        for raw_id in selected_ids:
            bq = BankQuestion.query.get(int(raw_id))
            if not bq or bq.creator_id != current_user.id:
                continue
            q = Question(
                exam_id=exam.id, question_type=bq.question_type, text=bq.text,
                image_url=bq.image_url, option_a=bq.option_a, option_b=bq.option_b,
                option_c=bq.option_c, option_d=bq.option_d, correct_option=bq.correct_option,
                matching_data=bq.matching_data, ordering_data=bq.ordering_data,
                short_answer_key=bq.short_answer_key, points=bq.points,
            )
            db.session.add(q)
            added += 1
        db.session.commit()
        flash(t("flash_bank_questions_added", count=added), "success")
        return redirect(url_for("teacher_add_question", exam_id=exam.id))

    query = BankQuestion.query.filter_by(creator_id=current_user.id, status="active")
    subject_id = request.args.get("subject_id") or ""
    difficulty = request.args.get("difficulty") or ""
    if subject_id:
        query = query.filter_by(subject_id=int(subject_id))
    if difficulty:
        query = query.filter_by(difficulty=difficulty)

    bank_questions = query.order_by(BankQuestion.created_at.desc()).all()
    subjects = Subject.query.order_by(Subject.name).all()
    return render_template(
        "teacher/add_from_bank.html", exam=exam, bank_questions=bank_questions,
        subjects=subjects, filters={"subject_id": subject_id, "difficulty": difficulty},
    )


@app.route("/teacher/bank/export")
@role_required("teacher")
def teacher_bank_export():
    questions = BankQuestion.query.filter_by(creator_id=current_user.id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "type", "text", "option_a", "option_b", "option_c", "option_d",
        "correct_option", "short_answer_key", "points", "difficulty", "keywords", "subject",
    ])
    for q in questions:
        writer.writerow([
            q.question_type, q.text, q.option_a or "", q.option_b or "", q.option_c or "", q.option_d or "",
            q.correct_option or "", q.short_answer_key or "", q.points, q.difficulty, q.keywords or "",
            q.subject.name if q.subject else "",
        ])
    log_action("export_bank_csv", details=f"{len(questions)} questions")
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=question_bank.csv"},
    )


@app.route("/teacher/bank/import", methods=["GET", "POST"])
@role_required("teacher")
def teacher_bank_import():
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash(t("flash_csv_file_required"), "danger")
            return redirect(url_for("teacher_bank_import"))

        try:
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            subject_cache = {s.name: s.id for s in Subject.query.all()}
            imported = 0

            for row in reader:
                q_type = (row.get("type") or "mcq").strip().lower()
                text = (row.get("text") or "").strip()
                # يدعم الاستيراد أنواعاً بسيطة فقط (mcq/true_false/short_answer/essay)؛
                # أسئلة التوصيل والترتيب تحتاج بنية أعقد من صف CSV مسطّح ولا تُستورد هنا.
                if not text or q_type not in ("mcq", "true_false", "essay", "short_answer"):
                    continue

                try:
                    points = int(row.get("points") or 1)
                except ValueError:
                    points = 1

                subject_id = subject_cache.get((row.get("subject") or "").strip())

                bq = BankQuestion(
                    creator_id=current_user.id, question_type=q_type, text=text, points=points,
                    difficulty=(row.get("difficulty") or "medium").strip().lower() or "medium",
                    keywords=(row.get("keywords") or "").strip() or None,
                    subject_id=subject_id,
                )

                if q_type == "mcq":
                    bq.option_a = (row.get("option_a") or "").strip()
                    bq.option_b = (row.get("option_b") or "").strip()
                    bq.option_c = (row.get("option_c") or "").strip() or None
                    bq.option_d = (row.get("option_d") or "").strip() or None
                    bq.correct_option = (row.get("correct_option") or "").strip().lower() or None
                    if not bq.option_a or not bq.option_b or bq.correct_option not in ("a", "b", "c", "d"):
                        continue
                elif q_type == "true_false":
                    raw_correct = (row.get("correct_option") or "").strip().lower()
                    bq.correct_option = "t" if raw_correct in ("t", "true", "1", "صح") else "f"
                elif q_type == "short_answer":
                    bq.short_answer_key = (row.get("short_answer_key") or "").strip()
                    if not bq.short_answer_key:
                        continue

                db.session.add(bq)
                imported += 1

            db.session.commit()
            log_action("import_bank_csv", details=f"{imported} questions")
            flash(t("flash_csv_imported", count=imported), "success")
        except Exception:
            db.session.rollback()
            flash(t("flash_csv_import_error"), "danger")

        return redirect(url_for("teacher_bank"))

    return render_template("teacher/bank_import.html")


@app.route("/teacher/exams/<int:exam_id>/toggle", methods=["POST"])
@role_required("teacher")
def teacher_toggle_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    was_active = exam.is_active

    # يمنع نشر امتحان بلا أسئلة (خطأ شائع يجعل الامتحان "منشوراً" لكن فارغاً)
    if not was_active and len(exam.questions) == 0:
        flash(t("flash_cannot_publish_empty"), "danger")
        return redirect(url_for("teacher_dashboard"))

    exam.is_active = not exam.is_active

    if exam.is_active and not was_active:
        notify_all_students(
            t("notify_exam_published_title"),
            t("notify_exam_published_message", title=exam.title),
            link=url_for("student_dashboard"),
        )
        flash(t("flash_exam_published"), "success")
    else:
        flash(t("flash_exam_unpublished"), "info")

    db.session.commit()
    log_action("publish_exam" if exam.is_active else "pause_exam", details=exam.title)
    return redirect(url_for("teacher_dashboard"))


@app.route("/teacher/exams/<int:exam_id>/duplicate", methods=["POST"])
@role_required("teacher")
def teacher_duplicate_exam(exam_id):
    """ينسخ امتحاناً موجوداً (بكل أسئلته) كمسودة جديدة منفصلة تماماً — مفيد لإعادة
    استخدام امتحان سابق كنقطة انطلاق دون التأثير على الأصل أو نتائجه."""
    original = Exam.query.get_or_404(exam_id)
    if original.teacher_id != current_user.id:
        abort(403)

    copy = Exam(
        title=t("duplicated_exam_title", title=original.title),
        description=original.description,
        teacher_id=current_user.id,
        subject_id=original.subject_id,
        duration_minutes=original.duration_minutes,
        start_time=None,  # يُترك للأستاذ تحديد مواعيد جديدة للنسخة
        end_time=None,
        shuffle_questions=original.shuffle_questions,
        shuffle_options=original.shuffle_options,
        is_active=False,  # تبدأ دائماً كمسودة
    )
    db.session.add(copy)
    db.session.flush()  # للحصول على copy.id قبل إنشاء الأسئلة المرتبطة به

    for q in original.questions:
        db.session.add(Question(
            exam_id=copy.id, question_type=q.question_type, text=q.text, image_url=q.image_url,
            option_a=q.option_a, option_b=q.option_b, option_c=q.option_c, option_d=q.option_d,
            correct_option=q.correct_option, matching_data=q.matching_data,
            ordering_data=q.ordering_data, short_answer_key=q.short_answer_key, points=q.points,
        ))

    db.session.commit()
    log_action("duplicate_exam", details=f"{original.title} -> {copy.title}")
    flash(t("flash_exam_duplicated"), "success")
    return redirect(url_for("teacher_add_question", exam_id=copy.id))


@app.route("/teacher/exams/<int:exam_id>/results")
@role_required("teacher")
def teacher_exam_results(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)
    submissions = Submission.query.filter_by(exam_id=exam.id, is_finished=True).order_by(Submission.submitted_at.desc()).all()
    return render_template("teacher/results.html", exam=exam, submissions=submissions)


@app.route("/teacher/exams/<int:exam_id>/results/export")
@role_required("teacher")
def teacher_exam_results_export(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    submissions = Submission.query.filter_by(exam_id=exam.id, is_finished=True).order_by(Submission.score.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_name", "score", "total_points", "percentage", "submitted_at", "violations", "auto_submitted"])
    for s in submissions:
        writer.writerow([
            s.student.full_name, s.score, s.total_points, s.percentage,
            s.submitted_at, s.violation_count, "yes" if s.auto_submitted else "no",
        ])

    log_action("export_results_csv", details=exam.title)
    safe_title = "".join(c if c.isalnum() else "_" for c in exam.title)[:50]
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=results_{safe_title}.csv"},
    )


@app.route("/teacher/exams/<int:exam_id>/analytics")
@role_required("teacher")
def teacher_exam_analytics(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    if exam.teacher_id != current_user.id:
        abort(403)

    submissions = Submission.query.filter_by(exam_id=exam.id, is_finished=True).all()

    data = {
        "submissions_count": len(submissions),
        "average_pct": 0, "highest_pct": 0, "lowest_pct": 0, "median_pct": 0,
        "pass_count": 0, "fail_count": 0,
        "avg_duration_minutes": 0,
        "avg_violations": 0,
        "auto_submitted_count": 0,
        "pending_grading_count": 0,
        "score_buckets": {"0-20%": 0, "20-40%": 0, "40-60%": 0, "60-80%": 0, "80-100%": 0},
        "questions": [],
        "student_rows": [],
    }

    if submissions:
        pcts = [s.percentage for s in submissions]
        data["average_pct"] = round(sum(pcts) / len(pcts), 1)
        data["highest_pct"] = max(pcts)
        data["lowest_pct"] = min(pcts)
        data["median_pct"] = round(statistics.median(pcts), 1)
        data["pass_count"] = sum(1 for p in pcts if p >= 50)
        data["fail_count"] = sum(1 for p in pcts if p < 50)
        data["auto_submitted_count"] = sum(1 for s in submissions if s.auto_submitted)
        data["pending_grading_count"] = sum(1 for s in submissions if s.needs_manual_grading and not s.manually_graded)
        data["avg_violations"] = round(sum(s.violation_count for s in submissions) / len(submissions), 1)

        durations = [s.duration_taken_seconds for s in submissions if s.duration_taken_seconds]
        if durations:
            data["avg_duration_minutes"] = round(sum(durations) / len(durations) / 60, 1)

        for p in pcts:
            if p < 20: data["score_buckets"]["0-20%"] += 1
            elif p < 40: data["score_buckets"]["20-40%"] += 1
            elif p < 60: data["score_buckets"]["40-60%"] += 1
            elif p < 80: data["score_buckets"]["60-80%"] += 1
            else: data["score_buckets"]["80-100%"] += 1

        data["student_rows"] = sorted(
            [{"name": s.student.full_name, "pct": s.percentage, "score": s.score,
              "total": s.total_points, "violations": s.violation_count,
              "auto": s.auto_submitted} for s in submissions],
            key=lambda r: r["pct"], reverse=True
        )

    # تحليل كل سؤال على حدة (نسبة الإجابة الصحيحة لكل سؤال قابل للتصحيح الآلي)
    for q in exam.questions:
        answers = Answer.query.filter(
            Answer.question_id == q.id,
            Answer.submission_id.in_([s.id for s in submissions])
        ).all() if submissions else []

        q_info = {"text": q.text, "type": q.question_type, "points": q.points, "answered": len(answers)}

        if q.question_type in ("mcq", "true_false", "short_answer"):
            correct = sum(1 for a in answers if a.is_correct)
            q_info["correct_pct"] = round((correct / len(answers)) * 100, 1) if answers else 0
        elif q.question_type == "matching":
            pairs_count = len(q.get_matching_pairs()) or 1
            avg_correct = sum((a.matching_correct_count or 0) for a in answers) / len(answers) if answers else 0
            q_info["correct_pct"] = round((avg_correct / pairs_count) * 100, 1) if answers else 0
        elif q.question_type == "ordering":
            items_count = len(q.get_ordering_items()) or 1
            avg_correct = sum((a.ordering_correct_count or 0) for a in answers) / len(answers) if answers else 0
            q_info["correct_pct"] = round((avg_correct / items_count) * 100, 1) if answers else 0
        else:  # essay
            graded = [a for a in answers if a.manual_points is not None]
            if graded:
                avg_pts = sum(a.manual_points for a in graded) / len(graded)
                q_info["correct_pct"] = round((avg_pts / q.points) * 100, 1) if q.points else 0
            else:
                q_info["correct_pct"] = None

        data["questions"].append(q_info)

    return render_template("teacher/analytics.html", exam=exam, data=data)


@app.route("/teacher/submissions/<int:submission_id>/grade", methods=["GET", "POST"])
@role_required("teacher")
def teacher_grade_submission(submission_id):
    submission = Submission.query.get_or_404(submission_id)
    if submission.exam.teacher_id != current_user.id:
        abort(403)

    essay_answers = [a for a in submission.answers if a.question.question_type == "essay"]

    if request.method == "POST":
        manual_total = 0
        for ans in essay_answers:
            raw = request.form.get(f"score_{ans.id}", "0")
            try:
                pts = float(raw)
            except ValueError:
                pts = 0
            pts = max(0, min(pts, ans.question.points))
            ans.manual_points = pts
            ans.points_earned = pts
            manual_total += pts

        submission.manual_score = manual_total
        submission.score = submission.auto_score + manual_total
        submission.manually_graded = True
        notify_user(
            submission.student_id,
            t("notify_grading_done_title"),
            t("notify_grading_done_message", exam=submission.exam.title),
            link=url_for("student_view_result", submission_id=submission.id),
        )
        db.session.commit()
        log_action("grade_essay", details=f"{submission.student.full_name} - {submission.exam.title}")
        flash(t("flash_grades_saved"), "success")
        return redirect(url_for("teacher_exam_results", exam_id=submission.exam_id))

    return render_template("teacher/grade_submission.html", submission=submission, essay_answers=essay_answers)


# ---------------------------------------------------------------------------
# لوحة تحكم الطالب (Student)
# ---------------------------------------------------------------------------
@app.route("/student")
@role_required("student")
def student_dashboard():
    now = datetime.now()
    exams = Exam.query.filter_by(is_active=True).all()

    my_submissions = {
        s.exam_id: s for s in Submission.query.filter_by(student_id=current_user.id).all()
    }

    available, upcoming = [], []
    for exam in exams:
        submission = my_submissions.get(exam.id)
        if submission and submission.is_finished:
            continue  # يظهر في صفحة الدرجات بدل هذه القائمة
        if exam.start_time and now < exam.start_time:
            upcoming.append((exam, submission))
        elif exam.end_time and now > exam.end_time:
            continue
        else:
            available.append((exam, submission))

    return render_template("student/dashboard.html", available=available, upcoming=upcoming, subjects=Subject.query.order_by(Subject.name).all())


@app.route("/student/grades")
@role_required("student")
def student_grades():
    submissions = Submission.query.filter_by(
        student_id=current_user.id, is_finished=True
    ).order_by(Submission.submitted_at.desc()).all()

    avg_pct = round(sum(s.percentage for s in submissions) / len(submissions), 1) if submissions else 0
    return render_template("student/grades.html", submissions=submissions, avg_pct=avg_pct)


@app.route("/student/exams/<int:exam_id>/start", methods=["POST"])
@role_required("student")
def student_start_exam(exam_id):
    exam = Exam.query.get_or_404(exam_id)
    now = datetime.now()

    if not exam.is_active:
        flash(t("flash_exam_not_available"), "danger")
        return redirect(url_for("student_dashboard"))
    if exam.start_time and now < exam.start_time:
        flash(t("flash_exam_not_started"), "warning")
        return redirect(url_for("student_dashboard"))
    if exam.end_time and now > exam.end_time:
        flash(t("flash_exam_ended"), "warning")
        return redirect(url_for("student_dashboard"))

    existing = Submission.query.filter_by(exam_id=exam.id, student_id=current_user.id).first()
    if existing:
        if existing.is_finished:
            flash(t("flash_already_submitted"), "info")
            return redirect(url_for("student_dashboard"))
        submission = existing
    else:
        submission = Submission(exam_id=exam.id, student_id=current_user.id, started_at=now)
        db.session.add(submission)
        db.session.commit()

    return redirect(url_for("student_take_exam", submission_id=submission.id))


@app.route("/student/submission/<int:submission_id>/take")
@role_required("student")
def student_take_exam(submission_id):
    submission = Submission.query.get_or_404(submission_id)
    if submission.student_id != current_user.id:
        abort(403)
    if submission.is_finished:
        return redirect(url_for("student_view_result", submission_id=submission.id))

    exam = submission.exam
    questions = list(exam.questions)
    if exam.shuffle_questions:
        random.shuffle(questions)

    # حساب الوقت المتبقي بالثواني
    elapsed = (datetime.now() - submission.started_at).total_seconds()
    remaining = max(0, exam.duration_minutes * 60 - elapsed)

    q_data = []
    for q in questions:
        item = {"id": q.id, "text": q.text, "type": q.question_type, "image_url": q.image_url, "points": q.points}

        if q.question_type == "mcq":
            opts = [("a", q.option_a), ("b", q.option_b)]
            if q.option_c:
                opts.append(("c", q.option_c))
            if q.option_d:
                opts.append(("d", q.option_d))
            if exam.shuffle_options:
                random.shuffle(opts)
            item["options"] = opts

        elif q.question_type == "matching":
            pairs = q.get_matching_pairs()
            terms = [{"index": i, "text": p["term"]} for i, p in enumerate(pairs)]
            defs = [{"original_index": i, "text": p["definition"]} for i, p in enumerate(pairs)]
            random.shuffle(defs)
            for n, d in enumerate(defs, start=1):
                d["display_number"] = n
            item["terms"] = terms
            item["definitions"] = defs

        elif q.question_type == "ordering":
            ordering_items = q.get_ordering_items()
            shuffled = [{"original_index": i, "text": txt} for i, txt in enumerate(ordering_items)]
            random.shuffle(shuffled)
            item["ordering_items"] = shuffled
            item["ordering_count"] = len(ordering_items)

        # essay و true_false و short_answer: لا حاجة لبيانات إضافية

        q_data.append(item)

    return render_template(
        "student/take_exam.html",
        exam=exam, submission=submission, questions=q_data, remaining=int(remaining)
    )


@app.route("/student/submission/<int:submission_id>/violation", methods=["POST"])
@role_required("student")
def student_report_violation(submission_id):
    """يستقبل تنبيهات نظام منع الغش (تبديل التبويب / الخروج من ملء الشاشة)."""
    submission = Submission.query.get_or_404(submission_id)
    if submission.student_id != current_user.id:
        abort(403)
    if submission.is_finished:
        return jsonify({"status": "finished"})

    submission.violation_count += 1
    db.session.commit()

    max_v = app.config["MAX_TAB_SWITCH_VIOLATIONS"]
    should_force_submit = submission.violation_count >= max_v
    return jsonify({
        "status": "ok",
        "violation_count": submission.violation_count,
        "max_violations": max_v,
        "force_submit": should_force_submit,
    })


@app.route("/student/submission/<int:submission_id>/submit", methods=["POST"])
@role_required("student")
def student_submit_exam(submission_id):
    submission = Submission.query.get_or_404(submission_id)
    if submission.student_id != current_user.id:
        abort(403)
    if submission.is_finished:
        return redirect(url_for("student_view_result", submission_id=submission.id))

    exam = submission.exam
    auto = request.form.get("auto_submitted") == "1"

    total_points = 0
    auto_score = 0
    has_essay = False

    for q in exam.questions:
        total_points += q.points
        ans = Answer(submission_id=submission.id, question_id=q.id)

        if q.question_type == "mcq":
            selected = request.form.get(f"question_{q.id}")
            ans.selected_option = selected
            is_correct = bool(selected) and selected == q.correct_option
            ans.is_correct = is_correct
            ans.points_earned = q.points if is_correct else 0
            auto_score += ans.points_earned

        elif q.question_type == "true_false":
            selected = request.form.get(f"question_{q.id}")
            ans.selected_option = selected
            is_correct = bool(selected) and selected == q.correct_option
            ans.is_correct = is_correct
            ans.points_earned = q.points if is_correct else 0
            auto_score += ans.points_earned

        elif q.question_type == "matching":
            pairs = q.get_matching_pairs()
            n_pairs = len(pairs) or 1
            selections = {}
            correct_count = 0
            for term_index in range(len(pairs)):
                raw = request.form.get(f"matching_{q.id}_{term_index}")
                selections[str(term_index)] = raw
                if raw is not None and raw != "" and int(raw) == term_index:
                    correct_count += 1
            ans.matching_answer = json.dumps(selections)
            ans.matching_correct_count = correct_count
            ans.points_earned = q.points * (correct_count / n_pairs)
            auto_score += ans.points_earned

        elif q.question_type == "ordering":
            items = q.get_ordering_items()
            n_items = len(items) or 1
            selections = {}
            correct_count = 0
            for idx in range(len(items)):
                raw = request.form.get(f"ordering_{q.id}_{idx}")
                selections[str(idx)] = raw
                if raw is not None and raw != "" and int(raw) == idx + 1:
                    correct_count += 1
            ans.ordering_answer = json.dumps(selections)
            ans.ordering_correct_count = correct_count
            ans.points_earned = q.points * (correct_count / n_items)
            auto_score += ans.points_earned

        elif q.question_type == "short_answer":
            given = request.form.get(f"shortanswer_{q.id}", "").strip()
            ans.text_answer = given
            correct_key = (q.short_answer_key or "").strip().lower()
            is_correct = bool(correct_key) and given.strip().lower() == correct_key
            ans.is_correct = is_correct
            ans.points_earned = q.points if is_correct else 0
            auto_score += ans.points_earned

        else:  # essay
            has_essay = True
            ans.text_answer = request.form.get(f"essay_{q.id}", "").strip()
            ans.manual_points = None
            ans.points_earned = 0

        db.session.add(ans)

    submission.auto_score = auto_score
    submission.manual_score = 0
    submission.score = auto_score
    submission.total_points = total_points
    submission.submitted_at = datetime.now()
    submission.auto_submitted = auto
    submission.is_finished = True
    submission.needs_manual_grading = has_essay
    submission.manually_graded = not has_essay
    db.session.commit()

    flash(t("flash_exam_submitted"), "success")
    return redirect(url_for("student_view_result", submission_id=submission.id))


@app.route("/student/submission/<int:submission_id>/result")
@role_required("student")
def student_view_result(submission_id):
    submission = Submission.query.get_or_404(submission_id)
    if submission.student_id != current_user.id:
        abort(403)
    if not submission.is_finished:
        return redirect(url_for("student_take_exam", submission_id=submission.id))
    return render_template("student/result.html", submission=submission)


# ---------------------------------------------------------------------------
# الإشعارات (لكل الأدوار)
# ---------------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications_list():
    notes = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(100).all()
    return render_template("notifications.html", notifications=notes)


@app.route("/notifications/<int:note_id>/read", methods=["POST"])
@login_required
def notification_mark_read(note_id):
    note = Notification.query.get_or_404(note_id)
    if note.user_id != current_user.id:
        abort(403)
    note.is_read = True
    db.session.commit()
    return redirect(note.link or url_for("notifications_list"))


@app.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def notifications_mark_all_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return redirect(url_for("notifications_list"))


# ---------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message=t("error_403")), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message=t("error_404")), 404


if __name__ == "__main__":
    # threaded=True مهم: بدون هذا الخيار سيتجمّد الخادم بالكامل لكل المستخدمين أثناء
    # أي عملية طويلة نسبياً مثل توليد مئات الحسابات دفعة واحدة (تجزئة كلمات المرور
    # الآمنة تستغرق وقتاً لكل حساب)، لأن خادم التطوير الافتراضي في Flask يعالج طلباً
    # واحداً في كل مرة ما لم يُفعَّل هذا الخيار.
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)
