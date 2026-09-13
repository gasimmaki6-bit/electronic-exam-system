import json
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # admin / teacher / student
    is_active_user = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    code = db.Column(db.String(30), nullable=True)
    description = db.Column(db.Text, default="")
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    teacher = db.relationship("User", backref="subjects")
    exams = db.relationship("Exam", backref="subject")


class Exam(db.Model):
    __tablename__ = "exams"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)
    duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    shuffle_questions = db.Column(db.Boolean, default=True)
    shuffle_options = db.Column(db.Boolean, default=True)
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    teacher = db.relationship("User", backref="exams")
    questions = db.relationship("Question", backref="exam", cascade="all, delete-orphan")
    submissions = db.relationship("Submission", backref="exam", cascade="all, delete-orphan")

    @property
    def total_points(self):
        return sum(q.points for q in self.questions) or 0

    @property
    def has_essay_questions(self):
        return any(q.question_type == "essay" for q in self.questions)


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)

    # نوع السؤال:
    # mcq (اختيار من متعدد) / true_false (صح أو خطأ) / essay (مقالي) /
    # matching (توصيل) / ordering (ترتيب) / short_answer (إجابة قصيرة)
    question_type = db.Column(db.String(20), nullable=False, default="mcq")

    text = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(300), nullable=True)  # صورة مرفقة بالسؤال (اختياري لأي نوع)

    # حقول خاصة بأسئلة الاختيار من متعدد
    option_a = db.Column(db.String(500), nullable=True)
    option_b = db.Column(db.String(500), nullable=True)
    option_c = db.Column(db.String(500), nullable=True)
    option_d = db.Column(db.String(500), nullable=True)
    correct_option = db.Column(db.String(1), nullable=True)  # a/b/c/d أو t/f

    # حقل خاص بأسئلة التوصيل: JSON لقائمة [{"term": "...", "definition": "..."}, ...]
    matching_data = db.Column(db.Text, nullable=True)

    # حقل خاص بأسئلة الترتيب: JSON لقائمة العناصر بترتيبها الصحيح ["عنصر1", "عنصر2", ...]
    ordering_data = db.Column(db.Text, nullable=True)

    # حقل خاص بأسئلة الإجابة القصيرة: الإجابة النموذجية المقبولة (تُقارن نصياً بعد تجاهل حالة الأحرف والمسافات)
    short_answer_key = db.Column(db.String(300), nullable=True)

    points = db.Column(db.Integer, default=1)

    def get_matching_pairs(self):
        if not self.matching_data:
            return []
        try:
            return json.loads(self.matching_data)
        except (ValueError, TypeError):
            return []

    def get_ordering_items(self):
        if not self.ordering_data:
            return []
        try:
            return json.loads(self.ordering_data)
        except (ValueError, TypeError):
            return []


class Submission(db.Model):
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.now)
    submitted_at = db.Column(db.DateTime, nullable=True)

    auto_score = db.Column(db.Float, default=0)     # الدرجة المصححة آلياً
    manual_score = db.Column(db.Float, default=0)    # الدرجة المصححة يدوياً (الأسئلة المقالية)
    score = db.Column(db.Float, default=0)           # المجموع النهائي = auto_score + manual_score
    total_points = db.Column(db.Float, default=0)

    violation_count = db.Column(db.Integer, default=0)
    auto_submitted = db.Column(db.Boolean, default=False)
    is_finished = db.Column(db.Boolean, default=False)

    needs_manual_grading = db.Column(db.Boolean, default=False)
    manually_graded = db.Column(db.Boolean, default=False)

    student = db.relationship("User", backref="submissions")
    answers = db.relationship("Answer", backref="submission", cascade="all, delete-orphan")

    @property
    def percentage(self):
        if not self.total_points:
            return 0
        return round((self.score / self.total_points) * 100, 1)

    @property
    def duration_taken_seconds(self):
        if self.submitted_at and self.started_at:
            return (self.submitted_at - self.started_at).total_seconds()
        return None


class Answer(db.Model):
    __tablename__ = "answers"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submissions.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)

    selected_option = db.Column(db.String(1), nullable=True)     # a/b/c/d أو t/f (اختيار متعدد / صح-خطأ)
    text_answer = db.Column(db.Text, nullable=True)               # نص إجابة السؤال المقالي أو القصير
    matching_answer = db.Column(db.Text, nullable=True)           # JSON: {"0": 2, "1": 0, ...}
    ordering_answer = db.Column(db.Text, nullable=True)           # JSON: {"0": 2, "1": 0, ...} (رقم الترتيب المختار لكل عنصر)

    is_correct = db.Column(db.Boolean, nullable=True)             # للأسئلة الثنائية (اختيار متعدد/صح-خطأ/إجابة قصيرة)
    matching_correct_count = db.Column(db.Integer, nullable=True) # عدد التوصيلات الصحيحة
    ordering_correct_count = db.Column(db.Integer, nullable=True) # عدد العناصر في الموضع الصحيح
    points_earned = db.Column(db.Float, default=0)                # الدرجة المكتسبة من هذا السؤال
    manual_points = db.Column(db.Float, nullable=True)             # درجة السؤال المقالي (يضعها الأستاذ)

    question = db.relationship("Question")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    user_name = db.Column(db.String(150), nullable=True)   # نسخة من الاسم وقت الحدث (تبقى حتى لو حُذف المستخدم)
    user_role = db.Column(db.String(20), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, default="")
    link = db.Column(db.String(300), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship("User", backref="notifications")


class BankQuestion(db.Model):
    """بنك أسئلة مستقل لكل أستاذ — قابل لإعادة الاستخدام عبر عدة امتحانات."""
    __tablename__ = "bank_questions"

    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=True)

    question_type = db.Column(db.String(20), nullable=False, default="mcq")
    text = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(300), nullable=True)

    option_a = db.Column(db.String(500), nullable=True)
    option_b = db.Column(db.String(500), nullable=True)
    option_c = db.Column(db.String(500), nullable=True)
    option_d = db.Column(db.String(500), nullable=True)
    correct_option = db.Column(db.String(1), nullable=True)

    matching_data = db.Column(db.Text, nullable=True)
    ordering_data = db.Column(db.Text, nullable=True)
    short_answer_key = db.Column(db.String(300), nullable=True)

    points = db.Column(db.Integer, default=1)
    difficulty = db.Column(db.String(10), default="medium")  # easy / medium / hard
    keywords = db.Column(db.String(300), nullable=True)
    status = db.Column(db.String(10), default="active")  # active / archived
    created_at = db.Column(db.DateTime, default=datetime.now)

    creator = db.relationship("User")
    subject = db.relationship("Subject")

    def get_matching_pairs(self):
        if not self.matching_data:
            return []
        try:
            return json.loads(self.matching_data)
        except (ValueError, TypeError):
            return []

    def get_ordering_items(self):
        if not self.ordering_data:
            return []
        try:
            return json.loads(self.ordering_data)
        except (ValueError, TypeError):
            return []
